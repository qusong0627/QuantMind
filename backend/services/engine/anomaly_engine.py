"""识别引擎 v1（T-P6-14）：四类检测 → 三类动作（告警/否决/降仓）常驻服务。

**数据流**（每轮 build_once，engine 服务内常驻循环，门控默认关）::

    市场量价（热集抽样快照 + volume_ma_3 基线）
    账户（simulation 账户持仓 + 当日 sim_orders）
    数据（热集抽样日线最新两根 vs 交易日历）
    模型（ready 模型的 model_ic_monitor 滚动 IC）
        → 检测（anomaly_detectors 纯函数）
        → 动作① 告警：intel 总线事件（type=anomaly，WS 实时）+ qm_market_anomalies 落表
        → 动作② 否决：severity=critical 时按标的/账户写 risk lock（fail-closed；
          模拟撮合买单即刻受阻，带 risk_events 审计行）
        → 动作③ 降仓：默认关（config.reduce_enabled）；开启后仅记审计建议，
          实际减仓执行由风控链（risk_trigger_service flatten）承接——本服务不直接下单

**门控** ``qm:engine:anomaly:config``（enabled/cadence_s/各类阈值/deny_enabled/reduce_enabled，
热读，默认关）。状态面 ``qm:anomaly:status``（计数）+ ``qm:anomaly:recent_symbols``
（近 1h 异动标的集合——热集构建器 T-P6-06 的"异动源"由此供数）。

**诚实边界**：取数失败/样本不足一律 `skipped` 计数，绝不用默认值编造检测；
指数/全市场级异动不在 v1（热集抽样 + 持仓账户为界）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, time as dtime
from collections.abc import Callable, Mapping, Sequence
from typing import Any
from zoneinfo import ZoneInfo

from backend.services.engine.anomaly_detectors import (
    Detection,
    detect_account_anomaly,
    detect_data_anomaly,
    detect_model_anomaly,
    detect_volume_price,
)

logger = logging.getLogger(__name__)

_SH_TZ = ZoneInfo("Asia/Shanghai")
CONFIG_KEY = "qm:engine:anomaly:config"
STATUS_KEY = "qm:anomaly:status"
RECENT_SYMBOLS_TTL = 3600
SOURCE = "anomaly_engine"

DEFAULT_CADENCE_S = 60.0
DEFAULT_DATA_EVERY_S = 1800.0
DEFAULT_MODEL_EVERY_S = 3600.0

_TRUTHY = {"1", "true", "yes", "on"}

# A 股连续竞价时段（分钟数），用于量比的时间校正
_SESSIONS = ((dtime(9, 30), dtime(11, 30)), (dtime(13, 0), dtime(15, 0)))


def trading_elapsed_fraction(now: datetime | None = None) -> float:
    """已过连续竞价时长占全天 240 分钟的比例（0.05~1.0；盘前/午休取已过部分）。"""
    current = now or datetime.now(_SH_TZ)
    minutes = current.hour * 60 + current.minute
    elapsed = 0.0
    for start, end in _SESSIONS:
        s = start.hour * 60 + start.minute
        e = end.hour * 60 + end.minute
        if minutes >= e:
            elapsed += e - s
        elif minutes > s:
            elapsed += minutes - s
    return min(max(elapsed / 240.0, 0.05), 1.0)


def _main_redis():
    import redis as _redis
    import os

    return _redis.Redis(
        host=os.getenv("REDIS_HOST") or "redis",
        port=int(os.getenv("REDIS_PORT", "6379")),
        password=os.getenv("REDIS_PASSWORD") or None,
        db=int(os.getenv("REDIS_DB_GENERAL", "0")),
        decode_responses=True,
        socket_connect_timeout=3,
        socket_timeout=5,
    )


def _remote_redis():
    from backend.shared.remote_quote_config import make_sync_client

    return make_sync_client()


@dataclass(frozen=True)
class AnomalyConfig:
    enabled: bool = False
    cadence_s: float = DEFAULT_CADENCE_S
    data_every_s: float = DEFAULT_DATA_EVERY_S
    model_every_s: float = DEFAULT_MODEL_EVERY_S
    market_sample: int = 80
    account_max: int = 50
    data_sample: int = 30
    model_max: int = 3
    volume_ratio_min: float = 3.0
    price_pct_min: float = 0.05
    cancel_ratio_min: float = 0.6
    min_orders: int = 10
    concentration_max: float = 0.5
    jump_pct_max: float = 0.11
    ic_short_min: float = 0.0
    ic_drop_ratio_max: float = 0.5
    deny_enabled: bool = True
    reduce_enabled: bool = False
    cooldown_s: float = 1800.0  # 同 (kind,subject,severity) 重复异动抑制窗（防总线/落表刷屏）
    min_position_value: float = 50_000.0  # 集中度判定的最小持仓市值（小额账户不报噪声）

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> AnomalyConfig:
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
            cadence_s=max(5.0, _f("cadence_s", DEFAULT_CADENCE_S)),
            data_every_s=max(60.0, _f("data_every_s", DEFAULT_DATA_EVERY_S)),
            model_every_s=max(300.0, _f("model_every_s", DEFAULT_MODEL_EVERY_S)),
            market_sample=max(1, _i("market_sample", 80)),
            account_max=max(1, _i("account_max", 50)),
            data_sample=max(1, _i("data_sample", 30)),
            model_max=max(1, _i("model_max", 3)),
            volume_ratio_min=max(1.0, _f("volume_ratio_min", 3.0)),
            price_pct_min=max(0.005, _f("price_pct_min", 0.05)),
            cancel_ratio_min=min(max(_f("cancel_ratio_min", 0.6), 0.05), 1.0),
            min_orders=max(1, _i("min_orders", 10)),
            concentration_max=min(max(_f("concentration_max", 0.5), 0.05), 1.0),
            jump_pct_max=max(0.02, _f("jump_pct_max", 0.11)),
            ic_short_min=_f("ic_short_min", 0.0),
            ic_drop_ratio_max=min(max(_f("ic_drop_ratio_max", 0.5), 0.05), 1.0),
            deny_enabled=_b("deny_enabled", True),
            reduce_enabled=_b("reduce_enabled", False),
            cooldown_s=max(60.0, _f("cooldown_s", 1800.0)),
            min_position_value=max(0.0, _f("min_position_value", 50_000.0)),
        )


def _load_config_sync() -> AnomalyConfig:
    try:
        client = _main_redis()
        raw = client.hgetall(CONFIG_KEY) or {}
        client.close()
        return AnomalyConfig.from_mapping(raw)
    except Exception:  # noqa: BLE001
        return AnomalyConfig()


class AnomalyEngine:
    """识别引擎常驻服务（依赖全注入，测试可整体桩化）。"""

    def __init__(
        self,
        *,
        config_loader: Callable[[], AnomalyConfig] | None = None,
        market_fetcher: Callable[[AnomalyConfig], Mapping[str, Mapping[str, Any]]] | None = None,
        account_fetcher: Callable[[AnomalyConfig], Sequence[Mapping[str, Any]]] | None = None,
        data_fetcher: Callable[[AnomalyConfig], Sequence[Mapping[str, Any]]] | None = None,
        model_fetcher: Callable[[AnomalyConfig], Sequence[Mapping[str, Any]]] | None = None,
        publisher: Callable[[Detection], None] | None = None,
        recorder: Callable[[Detection], Any] | None = None,
        denier: Callable[[Detection], dict[str, Any]] | None = None,
        reducer: Callable[[Detection], dict[str, Any]] | None = None,
        recent_marker: Callable[[Sequence[Detection]], None] | None = None,
        deduper: Callable[[list[Detection], AnomalyConfig], tuple[list[Detection], int]] | None = None,
        status_writer: Callable[[dict[str, Any]], None] | None = None,
        now_fn: Callable[[], float] = time.time,
    ) -> None:
        self._config_loader = config_loader or _load_config_sync
        self._market_fetcher = market_fetcher or self._default_market_inputs
        self._account_fetcher = account_fetcher or self._default_account_inputs
        self._data_fetcher = data_fetcher or self._default_data_inputs
        self._model_fetcher = model_fetcher or self._default_model_inputs
        self.publisher = publisher or self._default_publish
        self.recorder = recorder or self._default_record
        self.denier = denier or self._default_deny
        self.reducer = reducer or self._default_reduce
        self._recent_marker = recent_marker or self._mark_recent
        self._deduper = deduper or self._dedup
        self._status_writer = status_writer or self._default_status_write
        self._now = now_fn

        self._last_data_ts: float | None = None
        self._last_model_ts: float | None = None
        self._cursor = 0
        self._lock = threading.Lock()
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self.counters: dict[str, Any] = {
            "cycles": 0,
            "detections": 0,
            "deduped": 0,
            "alerts": 0,
            "denied": 0,
            "reduce_suggested": 0,
            "skipped_market": 0,
            "skipped_account": 0,
            "skipped_data": 0,
            "skipped_model": 0,
            "errors": 0,
            "last_error": None,
            "last_build_at": None,
            "last_detection_at": None,
        }

    # ── 动作（默认真实接线；测试注入桩） ────────────────────────────

    def _default_publish(self, detection: Detection) -> None:
        """动作① 告警：intel 总线（type=anomaly）。"""
        from backend.shared.intel_events import publish_event

        client = _main_redis()
        try:
            publish_event(
                client,
                {
                    "ts": self._now(),
                    "type": "anomaly",
                    "market": detection.market,
                    "targets": list(detection.targets)[:64],
                    "level": detection.severity,
                    "payload": {
                        "kind": detection.kind,
                        "title": detection.title,
                        "description": detection.description,
                        "metrics": detection.metrics,
                    },
                    "actions_hint": list(detection.actions_hint)[:8],
                    "source": SOURCE,
                },
            )
        finally:
            client.close()

    def _record_sync(self, detection: Detection) -> None:
        """动作① 附：落表 qm_market_anomalies（PG，同步引擎——本服务在 worker 线程运行）。"""
        from sqlalchemy import text

        from backend.shared.sync_db import sync_session

        with sync_session() as session:
            session.execute(
                text(
                    "INSERT INTO qm_market_anomalies "
                    "(anomaly_id, trade_date, anomaly_type, sector_id, instrument, severity, "
                    " title, description, details, created_at) "
                    "VALUES (gen_random_uuid()::text, :d, :t, NULL, :ins, :sev, :title, :desc, "
                    "        CAST(:details AS JSONB), NOW())"
                ),
                {
                    "d": datetime.now(_SH_TZ).date(),
                    "t": detection.kind,
                    "ins": None if detection.kind.startswith("account_")
                    else (detection.subject or None),
                    "sev": "critical" if detection.severity == "critical" else (
                        "warning" if detection.severity == "warn" else "info"),
                    "title": detection.title[:256],
                    "desc": detection.description[:2000],
                    "details": json.dumps(
                        {"metrics": detection.metrics, "source": SOURCE,
                         "targets": list(detection.targets)},
                        ensure_ascii=False, default=str,
                    ),
                },
            )
            session.commit()

    def _default_record(self, detection: Detection) -> None:
        self._record_sync(detection)

    def _default_deny(self, detection: Detection) -> dict[str, Any]:
        """动作② 否决：写 risk lock（fail-closed）+ risk_events 审计。仅 critical 触发。"""
        from backend.services.live_trading.services.risk_lock import (
            write_account_lock,
            write_symbol_lock,
        )
        from backend.services.trade_shared.redis_client import redis_client as trade_redis

        if trade_redis.client is None:
            trade_redis.connect()
        trade_date = datetime.now(_SH_TZ).date()
        tenant = str((detection.metrics or {}).get("tenant_id") or "default")
        locked_users: list[str] = []
        if detection.kind.startswith("account_"):
            user = str(detection.subject or "").strip()
            if user:
                write_account_lock(trade_redis, tenant, user, trade_date)
                locked_users.append(user)
        else:
            holders = self._symbol_holders(detection.subject)
            for tenant_id, user_id in holders:
                write_symbol_lock(trade_redis, tenant_id, user_id, trade_date, detection.subject)
                locked_users.append(f"{tenant_id}:{user_id}")
        self._audit(detection, action="deny", status="applied" if locked_users else "no_targets",
                    message=f"locked={len(locked_users)}")
        return {"locked": locked_users}

    def _symbol_holders(self, symbol: str) -> list[tuple[str, str]]:
        """该标的的模拟账户持有人（tenant, user）——与热集构建同源的账户扫描。"""
        out: list[tuple[str, str]] = []
        try:
            suffix = str(symbol or "")
            from backend.services.trade_shared.redis_client import redis_client as trade_redis

            if trade_redis.client is None:
                trade_redis.connect()
            for raw_key in trade_redis.client.scan_iter(match="simulation:account:*", count=500):
                parts = str(raw_key).split(":")
                if len(parts) < 4:
                    continue
                try:
                    payload = json.loads(trade_redis.client.get(raw_key) or "{}")
                except (TypeError, ValueError):
                    continue
                for sym, pos in (payload.get("positions") or {}).items():
                    if _same_symbol(str(sym), suffix):
                        try:
                            if float((pos or {}).get("volume") or 0) > 0:
                                out.append((parts[2] or "default", parts[3]))
                        except (TypeError, ValueError):
                            pass
                        break
        except Exception as exc:  # noqa: BLE001
            logger.warning("[anomaly] 持有人扫描失败 %s: %s", symbol, exc)
        return out

    def _audit(self, detection: Detection, *, action: str, status: str, message: str = "") -> None:
        """risk_events 审计行（动作留痕；best-effort，失败只记日志）。"""
        from sqlalchemy import text

        from backend.shared.sync_db import sync_session

        try:
            with sync_session() as session:
                session.execute(
                    text(
                        "INSERT INTO risk_events (rule_id, rule_type, tenant_id, user_id, trade_date, "
                        "symbol, action, status, message, created_at) "
                        "VALUES (NULL, :rt, :t, 0, :d, :s, :a, :st, :msg, NOW())"
                    ),
                    {
                        "rt": f"{SOURCE}:{detection.kind}"[:50],
                        "t": str((detection.metrics or {}).get("tenant_id") or "default")[:64],
                        "d": datetime.now(_SH_TZ).date(),
                        "s": str(detection.subject or "*")[:32],
                        "a": action[:32],
                        "st": status[:32],
                        "msg": message[:500],
                    },
                )
                session.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[anomaly] 审计写入失败: %s", exc)

    def _default_reduce(self, detection: Detection) -> dict[str, Any]:
        """动作③ 降仓：v1 只记审计建议（不直接下单；实际减仓由风控链承接）。"""
        self._audit(detection, action="reduce_suggested", status="pending",
                    message=detection.title)
        with self._lock:
            self.counters["reduce_suggested"] += 1
        return {"reduced": False, "suggested": True}

    def _default_status_write(self, payload: dict[str, Any]) -> None:
        client = _main_redis()
        try:
            client.hset(STATUS_KEY, mapping={
                "last_build_at": str(payload.get("at") or ""),
                "counters": json.dumps(payload.get("counters") or {}, ensure_ascii=False, default=str),
            })
            client.expire(STATUS_KEY, 86400)
        finally:
            client.close()

    def _mark_recent(self, detections: Sequence[Detection]) -> None:
        from backend.shared.anomaly_contract import RECENT_SYMBOLS_KEY

        symbols = []
        for d in detections:
            if d.kind in {"volume_surge", "price_surge", "price_limit_up", "price_limit_down"}:
                symbols.append(str(d.subject))
        if not symbols:
            return
        client = _main_redis()
        try:
            client.sadd(RECENT_SYMBOLS_KEY, *symbols[:200])
            client.expire(RECENT_SYMBOLS_KEY, RECENT_SYMBOLS_TTL)
        finally:
            client.close()

    # ── 主循环 ─────────────────────────────────────────────────────

    def build_once(self) -> dict[str, Any]:
        cfg = self._config_loader()
        if not cfg.enabled:
            return {"enabled": False}
        now = self._now()
        detections: list[Detection] = []

        # 市场（量价）
        try:
            quotes = dict(self._market_fetcher(cfg) or {})
            frac = trading_elapsed_fraction()
            detections += detect_volume_price(
                quotes,
                volume_ratio_min=cfg.volume_ratio_min,
                price_pct_min=cfg.price_pct_min,
                elapsed_fraction=frac,
            )
            with self._lock:
                self.counters["skipped_market"] += max(0, len(quotes) - len(
                    [q for q in quotes.values() if isinstance(q, Mapping) and q.get("price")]
                ))
        except Exception as exc:  # noqa: BLE001
            self._note_error(f"market fetch: {exc}")

        # 账户（撤单率/集中度）
        try:
            for account in self._account_fetcher(cfg) or []:
                detections += detect_account_anomaly(
                    list(account.get("orders") or []),
                    list(account.get("positions") or []),
                    cancel_ratio_min=cfg.cancel_ratio_min,
                    min_orders=cfg.min_orders,
                    concentration_max=cfg.concentration_max,
                    min_position_value=cfg.min_position_value,
                    subject=str(account.get("user_id") or ""),
                )
        except Exception as exc:  # noqa: BLE001
            self._note_error(f"account fetch: {exc}")

        # 数据（跳变/缺口）——低频（首轮必跑）
        if self._last_data_ts is None or now - self._last_data_ts >= cfg.data_every_s:
            self._last_data_ts = now
            try:
                for row in self._data_fetcher(cfg) or []:
                    detections += detect_data_anomaly(
                        row.get("latest") or {},
                        row.get("prev"),
                        jump_pct_max=cfg.jump_pct_max,
                        expected_prev_date=row.get("expected_prev_date"),
                        subject=str(row.get("symbol") or ""),
                    )
            except Exception as exc:  # noqa: BLE001
                self._note_error(f"data fetch: {exc}")

        # 模型（IC）——低频（首轮必跑）
        if self._last_model_ts is None or now - self._last_model_ts >= cfg.model_every_s:
            self._last_model_ts = now
            try:
                for row in self._model_fetcher(cfg) or []:
                    detections += detect_model_anomaly(
                        str(row.get("model_id") or ""),
                        row.get("ic_stats") or {},
                        short_min=cfg.ic_short_min,
                        drop_ratio_max=cfg.ic_drop_ratio_max,
                    )
            except Exception as exc:  # noqa: BLE001
                self._note_error(f"model fetch: {exc}")

        # 去重冷却：同 (kind, subject, severity) 在窗口内只触发一次（升级告警可再触发）
        detections, deduped = self._deduper(detections, cfg)

        # 动作（告警必达；否决/降仓按门控与级别）
        for d in detections:
            try:
                self.publisher(d)
                with self._lock:
                    self.counters["alerts"] += 1
            except Exception as exc:  # noqa: BLE001
                self._note_error(f"publish: {exc}")
            try:
                self.recorder(d)
            except Exception as exc:  # noqa: BLE001
                self._note_error(f"record: {exc}")
            if cfg.deny_enabled and d.severity == "critical":
                try:
                    result = self.denier(d) or {}
                    with self._lock:
                        self.counters["denied"] += 1
                    if not result.get("locked"):
                        self._note_error(f"deny no_targets: {d.kind}:{d.subject}")
                except Exception as exc:  # noqa: BLE001
                    self._note_error(f"deny: {exc}")
            if cfg.reduce_enabled and d.severity == "critical":
                try:
                    self.reducer(d)
                except Exception as exc:  # noqa: BLE001
                    self._note_error(f"reduce: {exc}")

        if detections:
            self._recent_marker(detections)
            with self._lock:
                self.counters["last_detection_at"] = datetime.now(_SH_TZ).isoformat()

        with self._lock:
            self.counters["cycles"] += 1
            self.counters["detections"] += len(detections)
            self.counters["deduped"] += deduped
            self.counters["last_build_at"] = datetime.now(_SH_TZ).isoformat()
            snapshot = dict(self.counters)
        try:
            self._status_writer({"at": snapshot["last_build_at"], "counters": snapshot})
        except Exception as exc:  # noqa: BLE001
            self._note_error(f"status write: {exc}")
        return {"enabled": True, "detections": len(detections), "counters": snapshot}

    def _dedup(self, detections: list[Detection], cfg: AnomalyConfig) -> tuple[list[Detection], int]:
        """去重冷却（Redis SET NX EX，按 kind:subject:severity）；Redis 不可用时放行（宁可多报）。"""
        if not detections:
            return detections, 0
        key_prefix = "qm:anomaly:last_fired:"
        client = None
        try:
            client = _main_redis()
        except Exception:  # noqa: BLE001
            return detections, 0
        kept: list[Detection] = []
        deduped = 0
        try:
            for d in detections:
                key = f"{key_prefix}{d.kind}:{d.subject}:{d.severity}"
                try:
                    first = client.set(key, str(int(self._now())), nx=True, ex=int(cfg.cooldown_s))
                except Exception:  # noqa: BLE001
                    first = True
                if first:
                    kept.append(d)
                else:
                    deduped += 1
        finally:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
        return kept, deduped

    def _note_error(self, message: str) -> None:
        with self._lock:
            self.counters["errors"] += 1
            self.counters["last_error"] = str(message)[:200]
        logger.warning("[anomaly] %s", message)

    async def run_forever(self) -> None:
        logger.info("[anomaly] 识别引擎循环启动")
        while not self._stop.is_set():
            try:
                cfg = self._config_loader()
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
                self.run_forever(), name="anomaly-engine"
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
        recent: list[str] = []
        try:
            from backend.shared.anomaly_contract import RECENT_SYMBOLS_KEY

            client = _main_redis()
            recent = sorted(client.smembers(RECENT_SYMBOLS_KEY) or [])[:50]
            client.close()
        except Exception:  # noqa: BLE001
            pass
        return {
            "enabled": cfg.enabled,
            "cadence_s": cfg.cadence_s,
            "deny_enabled": cfg.deny_enabled,
            "reduce_enabled": cfg.reduce_enabled,
            "counters": counters,
            "recent_symbols": recent,
        }

    # ── 默认取数（生产接线；全部失败安全） ──────────────────────────

    def _default_market_inputs(self, cfg: AnomalyConfig) -> Mapping[str, Mapping[str, Any]]:
        """热集抽样 → 远端快照 + volume_ma_3 基线（快照缺失/基线缺失的标的不产出检测）。"""
        from backend.shared.hot_set_store import make_hot_set_client, hot_set_key

        hc = make_hot_set_client()
        try:
            symbols = sorted(hc.smembers(hot_set_key()) or [])
        finally:
            hc.close()
        if not symbols:
            return {}
        n = min(len(symbols), max(1, cfg.market_sample))
        start = self._cursor % max(1, len(symbols))
        self._cursor = (start + n) % max(1, len(symbols))
        picked = [symbols[(start + i) % len(symbols)] for i in range(n)]

        rc = _remote_redis()
        if rc is None:
            return {}
        try:
            pipe = rc.pipeline(transaction=False)
            for sym in picked:
                code, mk = sym.split(".")
                pipe.hgetall(f"market:snapshot:{mk.lower()}{code}")
            snaps = pipe.execute()
        finally:
            try:
                rc.close()
            except Exception:  # noqa: BLE001
                pass

        baselines = self._volume_baselines(picked)
        out: dict[str, Mapping[str, Any]] = {}
        for sym, snap in zip(picked, snaps, strict=True):
            if not snap:
                continue
            try:
                price = float(snap.get("Now") or 0)
                pre = float(snap.get("PreClose") or 0)
            except (TypeError, ValueError):
                continue
            if price <= 0 or pre <= 0:
                continue
            now_vol = None
            try:
                now_vol = float(snap.get("Volume") or 0) or None
            except (TypeError, ValueError):
                pass
            out[sym] = {
                "price": price,
                "pct_chg": price / pre - 1.0,
                "now_volume": now_vol,
                "avg_daily_volume": baselines.get(sym),
                "limit_up": _safe_float(snap.get("LimitUp")),
                "limit_down": _safe_float(snap.get("LimitDown")),
                "is_suspended": False,
            }
        return out

    def _volume_baselines(self, symbols: list[str]) -> dict[str, float]:
        """volume_ma_3（features_daily 3 日均量）——单分区列裁剪；失败返回空（宁缺毋假）。"""
        cache_key = f"qm:anomaly:volbase:{datetime.now(_SH_TZ).date().isoformat()}"
        client = None
        try:
            client = _main_redis()
            cached = client.hgetall(cache_key) or {}
        except Exception:  # noqa: BLE001
            cached = {}
        out: dict[str, float] = {}
        missing: list[str] = []
        for sym in symbols:
            v = cached.get(sym)
            if v:
                fv = _safe_float(v)
                if fv:
                    out[sym] = fv
                    continue
            missing.append(sym)
        if missing:
            try:
                from backend.services.engine.data_platform.quantdb_hub import QuantDBDataHub

                hub = QuantDBDataHub.get_instance()
                df = hub.fetch_latest_rows(
                    "qdb_features_daily", missing[:200], columns=["volume_ma_3"]
                )
                if not df.empty:
                    for _, row in df.iterrows():
                        fv = _safe_float(row.get("volume_ma_3"))
                        sym = str(row.get("symbol") or "")
                        if fv and sym:
                            out[sym] = fv
            except Exception as exc:  # noqa: BLE001
                logger.debug("[anomaly] volume baseline 读取失败: %s", exc)
        if out and client is not None:
            try:
                client.hset(cache_key, mapping={k: str(v) for k, v in out.items()})
                client.expire(cache_key, 3600 * 12)
            except Exception:  # noqa: BLE001
                pass
        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
        return out

    def _default_data_inputs(self, cfg: AnomalyConfig) -> Sequence[Mapping[str, Any]]:
        """数据异常取数：热集抽样日线最新两根（qdb_daily_forward 分区读）+ 日历前一日。

        口径：只对"最新 bar == 全市场最新交易分区"的标的做缺口判定（停牌旧 bar 不误报）；
        跳变判定始终可用。取数失败如实抛（上层计数），不编造。
        """
        from backend.shared.hot_set_store import make_hot_set_client, hot_set_key

        hc = make_hot_set_client()
        try:
            symbols = sorted(hc.smembers(hot_set_key()) or [])
        finally:
            hc.close()
        if not symbols:
            return []
        n = min(len(symbols), max(1, cfg.data_sample))
        picked = symbols[:n]

        from backend.services.engine.data_platform.quantdb_hub import QuantDBDataHub

        hub = QuantDBDataHub.get_instance()
        latest_df = hub.fetch_latest_rows(
            "qdb_daily_forward", picked, columns=["close", "volume"]
        )
        if latest_df is None or latest_df.empty:
            return []
        latest_by_symbol: dict[str, dict[str, Any]] = {}
        for _, row in latest_df.iterrows():
            sym = str(row.get("symbol") or "")
            if not sym:
                continue
            latest_by_symbol[sym] = {
                "date": _dt_to_iso(row.get("dt")),
                "close": row.get("close"),
                "volume": row.get("volume"),
            }
        if not latest_by_symbol:
            return []
        market_latest = max(v["date"] for v in latest_by_symbol.values() if v["date"])
        prev_df = hub.fetch_latest_rows(
            "qdb_daily_forward",
            list(latest_by_symbol.keys()),
            dt=int(market_latest.replace("-", "")) - 1,
            columns=["close"],
        )
        prev_by_symbol: dict[str, dict[str, Any]] = {}
        if prev_df is not None and not prev_df.empty:
            for _, row in prev_df.iterrows():
                sym = str(row.get("symbol") or "")
                if sym:
                    prev_by_symbol[sym] = {"date": _dt_to_iso(row.get("dt")), "close": row.get("close")}
        expected_prev = _prev_trading_day(market_latest)
        out: list[Mapping[str, Any]] = []
        for sym, latest in latest_by_symbol.items():
            out.append(
                {
                    "symbol": sym,
                    "latest": latest,
                    "prev": prev_by_symbol.get(sym),
                    # 仅当日有 bar 的标的做缺口判定（停牌/新股不误报）
                    "expected_prev_date": expected_prev if latest["date"] == market_latest else None,
                }
            )
        return out

    def _default_model_inputs(self, cfg: AnomalyConfig) -> Sequence[Mapping[str, Any]]:
        """模型异常取数：最近有 pred.parquet 的 N 个模型 → model_ic_monitor 滚动 IC。

        注意：monitor() 失败路径用 SystemExit（脚本语义）——此处必须捕 BaseException，
        否则会杀死服务循环；单模型失败只计入 errors 不影响其余模型。
        """
        import os
        from pathlib import Path

        root = Path(os.getenv("QM_MODELS_DIR") or "/app/models")
        candidates: list[Path] = []
        try:
            for pred in root.glob("**/pred.parquet"):
                candidates.append(pred.parent)
        except Exception:  # noqa: BLE001
            return []
        if not candidates:
            return []
        candidates.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
        picked = candidates[: max(1, cfg.model_max)]

        from backend.scripts.model_ic_monitor import monitor as ic_monitor

        out: list[Mapping[str, Any]] = []
        for model_dir in picked:
            model_id = model_dir.name
            try:
                result = ic_monitor(model_id, 90, [5, 20])
            except BaseException as exc:  # noqa: BLE001 - SystemExit 一并兜住
                self._note_error(f"ic monitor {model_id}: {exc}")
                continue
            windows = result.get("windows") or {}
            short = windows.get("last_5") or {}
            long = windows.get("last_20") or {}
            out.append(
                {
                    "model_id": model_id,
                    "ic_stats": {
                        "ic_5": short.get("mean_ic"),
                        "ic_20": long.get("mean_ic"),
                        "n_5": short.get("days"),
                        "n_20": long.get("days"),
                        "latest_ic_date": result.get("latest_ic_date"),
                    },
                }
            )
        return out

    def _default_account_inputs(self, cfg: AnomalyConfig) -> Sequence[Mapping[str, Any]]:
        """模拟账户：持仓（集中度）+ 当日委托（撤单率，sim_orders 真库）。"""
        from backend.services.trade_shared.redis_client import redis_client as trade_redis

        if trade_redis.client is None:
            trade_redis.connect()
        accounts: dict[tuple[str, str], dict[str, Any]] = {}
        try:
            for raw_key in trade_redis.client.scan_iter(match="simulation:account:*", count=500):
                parts = str(raw_key).split(":")
                if len(parts) < 4:
                    continue
                try:
                    payload = json.loads(trade_redis.client.get(raw_key) or "{}")
                except (TypeError, ValueError):
                    continue
                positions = []
                for sym, pos in (payload.get("positions") or {}).items():
                    try:
                        volume = float((pos or {}).get("volume") or 0)
                        price = float((pos or {}).get("price") or 0)
                    except (TypeError, ValueError):
                        continue
                    if volume <= 0:
                        continue
                    positions.append({"symbol": str(sym),
                                      "market_value": volume * price if price > 0 else 0.0,
                                      "volume": volume})
                if positions:
                    accounts[(parts[2] or "default", parts[3])] = {
                        "tenant_id": parts[2] or "default",
                        "user_id": parts[3],
                        "positions": positions,
                        "orders": [],
                    }
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"账户扫描失败: {exc}") from exc
        # 当日委托（真库）
        try:
            self._fill_orders(accounts, cfg)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[anomaly] 委托读取失败: %s", exc)
        return list(accounts.values())[: max(1, cfg.account_max)]

    def _fill_orders(self, accounts: dict, cfg: AnomalyConfig) -> None:
        from sqlalchemy import text

        from backend.shared.sync_db import sync_session

        today = datetime.now(_SH_TZ).date()
        with sync_session() as session:
            rows = session.execute(
                text(
                    "SELECT tenant_id, user_id, status::text AS status FROM sim_orders "
                    "WHERE created_at >= :start AND created_at < :end"
                ),
                {"start": datetime.combine(today, dtime.min, tzinfo=_SH_TZ),
                 "end": datetime.combine(today, dtime.max, tzinfo=_SH_TZ)},
            ).fetchall()
        for tenant_id, user_id, status in rows:
            key = (str(tenant_id or "default"), str(user_id))
            acct = accounts.get(key)
            if acct is not None:
                acct["orders"].append({"status": status})


def _same_symbol(a: str, b: str) -> bool:
    """两形态（前缀/后缀/裸码）是否同一标的——StockCodeUtil 归一后比对。"""
    try:
        from backend.shared.stock_utils import StockCodeUtil

        na = str(StockCodeUtil.to_suffix(str(a or "").strip()) or "").strip().upper()
        nb = str(StockCodeUtil.to_suffix(str(b or "").strip()) or "").strip().upper()
        if na and nb:
            return na == nb
    except Exception:  # noqa: BLE001
        pass
    return str(a or "").strip().upper() == str(b or "").strip().upper()


def _dt_to_iso(value: Any) -> str:
    """分区 dt（int/str YYYYMMDD）→ ISO 日期串；非法返回空串。"""
    raw = str(value or "").strip()
    if len(raw) == 8 and raw.isdigit():
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
    return raw[:10]


_calendar_cache: dict[str, Any] = {"loaded_at": 0.0, "days": []}


def _prev_trading_day(date_iso: str) -> str | None:
    """日历前一日（qlib calendars/day.txt，进程内缓存 12h；读取失败返回 None）。"""
    import time as _time

    cache = _calendar_cache
    if not cache["days"] or (_time.time() - cache["loaded_at"]) > 43_200:
        try:
            from backend.shared.qlib_paths import resolve_qlib_calendar_path

            path = resolve_qlib_calendar_path("CN")
            days = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
            cache["days"] = days
            cache["loaded_at"] = _time.time()
        except Exception as exc:  # noqa: BLE001 - 日历不可得 → 缺口判定跳过
            logger.debug("[anomaly] 交易日历读取失败: %s", exc)
            return None
    days = cache["days"]
    target = str(date_iso or "")
    if not days or target > days[-1]:
        # 日历落后于数据（qlib 缓存未跟随同步）→ 无法判定缺口，宁缺毋假
        return None
    prev = None
    for day in days:
        if day >= target:
            break
        prev = day
    return prev


def _safe_float(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out:
        return None
    return out


_service_singleton: AnomalyEngine | None = None


def default_service() -> AnomalyEngine:
    global _service_singleton
    if _service_singleton is None:
        _service_singleton = AnomalyEngine()
    return _service_singleton
