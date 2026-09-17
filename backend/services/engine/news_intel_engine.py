"""新闻情报服务（T-P6-12）：enrichment → 去重/时效 → 分级 → 总线事件 + 风险 veto 直连。

**数据流**（常驻循环，门控 ``qm:engine:news_intel:config`` 默认关）::

    news_article_enrichment（PG，enricher 已算好实体/标签/情绪）
      → 增量游标 + title_hash 去重 + 时效过滤（max_age_min）
      → classify_news（风险/利空/利好，见 news_intel.py 纯函数）
      → 总线事件（intel:events, type=news, source=news_intel）
      → **风险事件（critical）**：写标级 veto 标记（news_veto.mark_news_veto）——
        策略配置 risk.veto.news_event=true 的买单在模拟引擎落地前被拦（留痕）
      → **情绪突变**：窗口内同标的负面文章数超阈 → 追加 sentiment_spike 事件（冷却）

误判修正路径：enrichment 侧词库/模型版本化（``target_version``/``is_row_outdated`` +
``run_full_rebuild``）——修正后行被重算，本服务按增量自然接上新结果，无需改代码。
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from backend.services.engine.news_intel import (
    KIND_SENTIMENT_SPIKE,
    LEVEL_WARN,
    NewsVerdict,
    batch_cursor_id,
    build_news_events,
    classify_news,
    is_fresh,
)

logger = logging.getLogger(__name__)
_SH_TZ = ZoneInfo("Asia/Shanghai")

CONFIG_KEY = "qm:engine:news_intel:config"
STATUS_KEY = "qm:news:intel:status"
CURSOR_KEY = "qm:news:intel:cursor"
SEEN_KEY = "qm:news:intel:seen"
SPIKE_ZSET_PREFIX = "qm:news:intel:neg:"       # per-symbol 负面分 ZSET（score=ts）
SPIKE_COOLDOWN_PREFIX = "qm:news:intel:spike:"  # per-symbol 突变冷却
SOURCE = "news_intel"

DEFAULT_CADENCE_S = 60.0
_TRUTHY = {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class NewsIntelConfig:
    enabled: bool = False
    cadence_s: float = DEFAULT_CADENCE_S
    max_age_min: float = 120.0
    min_abs_score: float = 0.6
    batch: int = 200
    veto_enabled: bool = True
    spike_min_articles: int = 2
    spike_window_s: float = 1800.0
    spike_score_max: float = -0.5   # 计入"负面"的文章情绪分上限
    spike_cooldown_s: float = 3600.0

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> NewsIntelConfig:
        m = dict(raw or {})

        def _b(key: str, default: bool) -> bool:
            v = m.get(key)
            if v is None or str(v).strip() == "":
                return default
            return str(v).strip().lower() in _TRUTHY

        def _f(key: str, default: float) -> float:
            try:
                return float(m.get(key))
            except (TypeError, ValueError):
                return default

        def _i(key: str, default: int) -> int:
            try:
                return int(float(m.get(key)))
            except (TypeError, ValueError):
                return default

        return cls(
            enabled=_b("enabled", False),
            cadence_s=max(10.0, _f("cadence_s", DEFAULT_CADENCE_S)),
            max_age_min=max(1.0, _f("max_age_min", 120.0)),
            min_abs_score=min(max(_f("min_abs_score", 0.6), 0.0), 1.0),
            batch=min(max(_i("batch", 200), 1), 1000),
            veto_enabled=_b("veto_enabled", True),
            spike_min_articles=max(2, _i("spike_min_articles", 2)),
            spike_window_s=max(60.0, _f("spike_window_s", 1800.0)),
            spike_score_max=min(_f("spike_score_max", -0.5), 0.0),
            spike_cooldown_s=max(60.0, _f("spike_cooldown_s", 3600.0)),
        )


def _main_redis():
    import os

    import redis as _redis

    return _redis.Redis(
        host=os.getenv("REDIS_HOST") or "redis",
        port=int(os.getenv("REDIS_PORT", "6379")),
        password=os.getenv("REDIS_PASSWORD") or None,
        db=int(os.getenv("REDIS_DB_GENERAL", "0")),
        decode_responses=True,
        socket_connect_timeout=3,
        socket_timeout=5,
    )


def _load_config_sync() -> NewsIntelConfig:
    try:
        client = _main_redis()
        raw = client.hgetall(CONFIG_KEY) or {}
        client.close()
        return NewsIntelConfig.from_mapping(raw)
    except Exception:  # noqa: BLE001
        return NewsIntelConfig()


class NewsIntelEngine:
    """新闻情报常驻服务（依赖全注入，测试可整体桩化）。"""

    def __init__(
        self,
        *,
        config_loader: Callable[[], NewsIntelConfig] | None = None,
        fetch_fn: Callable[[NewsIntelConfig, str], tuple[list[dict[str, Any]], str]] | None = None,
        publisher: Callable[[dict[str, Any]], None] | None = None,
        veto_marker: Callable[[Sequence[str]], int] | None = None,
        status_writer: Callable[[dict[str, Any]], None] | None = None,
        now_fn: Callable[[], float] = time.time,
    ) -> None:
        self._config_loader = config_loader or _load_config_sync
        self._fetch_fn = fetch_fn or self._default_fetch
        self._publisher = publisher or self._default_publish
        self._veto_marker = veto_marker or self._default_veto_marker
        self._status_writer = status_writer or self._default_status_write
        self._now = now_fn
        self._lock = threading.Lock()
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self.counters: dict[str, Any] = {
            "cycles": 0,
            "scanned": 0,
            "published": 0,
            "risk_events": 0,
            "veto_marked": 0,
            "spikes": 0,
            "skipped_dup": 0,
            "skipped_stale": 0,
            "skipped_neutral": 0,
            "errors": 0,
            "last_error": None,
            "last_build_at": None,
            "last_cursor": None,
        }

    # ── 动作 ────────────────────────────────────────────────────────

    def _default_publish(self, event: dict[str, Any]) -> None:
        from backend.shared.intel_events import publish_event

        client = _main_redis()
        try:
            publish_event(client, event)
        finally:
            client.close()

    def _default_veto_marker(self, symbols: Sequence[str]) -> int:
        from backend.services.live_trading.services.news_veto import mark_news_veto
        from backend.services.trade_shared.redis_client import redis_client as trade_redis

        if trade_redis.client is None:
            trade_redis.connect()
        return mark_news_veto(trade_redis, symbols)

    def _default_status_write(self, payload: dict[str, Any]) -> None:
        client = _main_redis()
        try:
            client.hset(
                STATUS_KEY,
                mapping={
                    "last_build_at": str(payload.get("at") or ""),
                    "counters": json.dumps(payload.get("counters") or {}, ensure_ascii=False, default=str),
                },
            )
            client.expire(STATUS_KEY, 86400)
        finally:
            client.close()

    # ── 主循环 ─────────────────────────────────────────────────────

    def build_once(self) -> dict[str, Any]:
        cfg = self._config_loader()
        if not cfg.enabled:
            return {"enabled": False}
        client = _main_redis()
        try:
            cursor = str(client.get(CURSOR_KEY) or "")
            rows, new_cursor = self._fetch_fn(cfg, cursor)
            now = datetime.now(timezone.utc)
            published = 0
            scanned = len(rows or [])
            for row in rows or []:
                title_hash = str(row.get("title_hash") or "")
                if title_hash:
                    try:
                        is_new = bool(client.sadd(SEEN_KEY, title_hash))
                        client.expire(SEEN_KEY, 172800)
                    except Exception:  # noqa: BLE001
                        is_new = True
                    if not is_new:
                        self._bump("skipped_dup")
                        continue
                if not is_fresh(row.get("enriched_at"), now=now, max_age_min=cfg.max_age_min):
                    self._bump("skipped_stale")
                    continue
                verdict = classify_news(row, min_abs_score=cfg.min_abs_score)
                if verdict is None:
                    self._bump("skipped_neutral")
                    continue
                self._publish_verdict(row, verdict, cfg, client)
                published += 1
                self._maybe_spike(row, verdict, cfg, client)
            if new_cursor:
                client.set(CURSOR_KEY, new_cursor)
            with self._lock:
                self.counters["cycles"] += 1
                self.counters["scanned"] += scanned
                self.counters["published"] += published
                self.counters["last_build_at"] = datetime.now(_SH_TZ).isoformat()
                self.counters["last_cursor"] = new_cursor or cursor
                snapshot = dict(self.counters)
            try:
                self._status_writer({"at": snapshot["last_build_at"], "counters": snapshot})
            except Exception as exc:  # noqa: BLE001
                self._note_error(f"status write: {exc}")
            return {"enabled": True, "scanned": scanned, "published": published,
                    "counters": snapshot}
        finally:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass

    def _publish_verdict(
        self, row: Mapping[str, Any], verdict: NewsVerdict, cfg: NewsIntelConfig, client: Any
    ) -> None:
        for event in build_news_events(row, verdict, ts=self._now()):
            try:
                self._publisher(event)
            except Exception as exc:  # noqa: BLE001
                self._note_error(f"publish: {exc}")
        if verdict.kind == "risk_event":
            self._bump("risk_events")
            if cfg.veto_enabled:
                try:
                    marked = int(self._veto_marker(list(verdict.targets)) or 0)
                    self._bump("veto_marked", marked)
                except Exception as exc:  # noqa: BLE001
                    self._note_error(f"veto marker: {exc}")

    def _maybe_spike(
        self, row: Mapping[str, Any], verdict: NewsVerdict, cfg: NewsIntelConfig, client: Any
    ) -> None:
        """情绪突变：窗口内同标的负面文章 ≥ N → 追加事件（per-symbol 冷却）。"""
        if verdict.sentiment_score > cfg.spike_score_max:
            return
        now = self._now()
        for symbol in verdict.targets:
            zkey = f"{SPIKE_ZSET_PREFIX}{symbol}"
            ckey = f"{SPIKE_COOLDOWN_PREFIX}{symbol}"
            try:
                pipe = client.pipeline(transaction=False)
                pipe.zadd(zkey, {f"{row.get('huntly_page_id') or now}": now})
                pipe.zremrangebyscore(zkey, 0, now - cfg.spike_window_s)
                pipe.expire(zkey, int(cfg.spike_window_s) * 2)
                pipe.execute()
                count = int(client.zcard(zkey) or 0)
                if count < cfg.spike_min_articles:
                    continue
                if not client.set(ckey, "1", nx=True, ex=int(cfg.spike_cooldown_s)):
                    continue
            except Exception as exc:  # noqa: BLE001
                self._note_error(f"spike: {exc}")
                continue
            for spike_event in build_news_events(
                row, NewsVerdict(verdict.level, verdict.kind, (symbol,),
                                 verdict.event_tags, verdict.sentiment_score, verdict.sentiment_label),
                ts=now,
            ):
                spike_event["payload"]["kind"] = KIND_SENTIMENT_SPIKE
                spike_event["payload"]["negative_articles"] = count
                spike_event["payload"]["window_s"] = cfg.spike_window_s
                spike_event["level"] = LEVEL_WARN
                spike_event["actions_hint"] = ["risk_watch"]
                try:
                    self._publisher(spike_event)
                    self._bump("spikes")
                except Exception as exc:  # noqa: BLE001
                    self._note_error(f"spike publish: {exc}")

    def _bump(self, key: str, delta: int = 1) -> None:
        with self._lock:
            self.counters[key] = int(self.counters.get(key) or 0) + int(delta)

    def _note_error(self, message: str) -> None:
        with self._lock:
            self.counters["errors"] += 1
            self.counters["last_error"] = str(message)[:200]
        logger.warning("[news_intel] %s", message)

    async def run_forever(self) -> None:
        logger.info("[news_intel] 新闻情报循环启动")
        while not self._stop.is_set():
            cfg = self._config_loader()
            try:
                if cfg.enabled:
                    await asyncio.to_thread(self.build_once)
                await asyncio.wait_for(self._stop.wait(), timeout=cfg.cadence_s)
            except asyncio.TimeoutError:
                continue
            except Exception as exc:  # noqa: BLE001
                self._note_error(f"cycle: {exc}")
                await asyncio.sleep(5)

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.get_running_loop().create_task(
                self.run_forever(), name="news-intel-engine"
            )

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except (asyncio.TimeoutError, Exception):  # noqa: BLE001
                pass

    def status(self) -> dict[str, Any]:
        cfg = self._config_loader()
        with self._lock:
            counters = dict(self.counters)
        return {
            "enabled": cfg.enabled,
            "cadence_s": cfg.cadence_s,
            "max_age_min": cfg.max_age_min,
            "min_abs_score": cfg.min_abs_score,
            "veto_enabled": cfg.veto_enabled,
            "counters": counters,
        }

    # ── 默认取数（生产接线） ────────────────────────────────────────

    def _default_fetch(
        self, cfg: NewsIntelConfig, cursor: str
    ) -> tuple[list[dict[str, Any]], str]:
        """增量读 enrichment（enriched_at > cursor；无游标时自 now-batch 窗口起）。"""
        from sqlalchemy import text

        from backend.shared.sync_db import sync_session

        where = "enriched_at > :cursor" if cursor else "enriched_at > NOW() - INTERVAL '10 minutes'"
        params: dict[str, Any] = {"limit": cfg.batch}
        if cursor:
            params["cursor"] = cursor
        sql = (
            "SELECT huntly_page_id, tickers, industries, event_tags, sentiment_score, "
            "       sentiment_label, title, title_hash, enriched_at "
            f"FROM news_article_enrichment WHERE {where} "
            "ORDER BY enriched_at ASC, huntly_page_id ASC LIMIT :limit"
        )
        with sync_session() as session:
            rows = [
                {
                    "huntly_page_id": r[0], "tickers": list(r[1] or []), "industries": list(r[2] or []),
                    "event_tags": list(r[3] or []), "sentiment_score": r[4], "sentiment_label": r[5],
                    "title": r[6], "title_hash": r[7], "enriched_at": r[8],
                }
                for r in session.execute(text(sql), params).fetchall()
            ]
        return rows, batch_cursor_id(rows)


_service_singleton: NewsIntelEngine | None = None


def default_service() -> NewsIntelEngine:
    global _service_singleton
    if _service_singleton is None:
        _service_singleton = NewsIntelEngine()
    return _service_singleton
