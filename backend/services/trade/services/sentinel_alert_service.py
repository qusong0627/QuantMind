"""哨兵告警服务（T-P6-15）：情报总线 → 留痕表 → 分级推送（管理员通知）+ 节流。

**数据流**（trade 服务常驻 worker，消费组 ``sentinel``，门控 ``qm:sentinel:config`` 默认关）::

    intel:events（Redis Stream，与 WS 推送器各自消费组）
      → 留痕（sentinel_alerts 表；dedupe_key 含 stream msg_id —— 重投递幂等）：
         全量落表（含 info 级），方向/标题/载荷全存
      → 分级推送（severity ≥ push_level_min；复用 notification_publisher → PG+WS 通知卡）：
         cooldown（同类型同标的）+ 小时总配额 双闸门，逐条记 push_reason
      → 管理员 fanout（users.is_admin，与 DataAlertService 同口径）
    T+1 回填与误报率报表见 sentinel_backfill.py / api/routers/sentinel.py。

**毒丸纪律**：坏 JSON/schema 拒绝 → ack 跳过 + 计数（绝不卡组，同 intel_pusher 约定）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
_SH_TZ = ZoneInfo("Asia/Shanghai")

CONFIG_KEY = "qm:sentinel:config"
STATUS_KEY = "qm:sentinel:status"
COOLDOWN_PREFIX = "qm:sentinel:pushcd:"
RATE_KEY_PREFIX = "qm:sentinel:pushh:"
CONSUMER_GROUP = "sentinel"
CONSUMER_NAME = "sentinel-1"
SOURCE = "sentinel"

_SEVERITY_RANK = {"info": 0, "warn": 1, "critical": 2}
_LEVEL_TO_NOTIFY = {"critical": "error", "warn": "warning", "info": "info"}
_TRUTHY = {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class SentinelConfig:
    enabled: bool = False
    push_level_min: str = "warn"  # 推送下限（info 只留痕不推）
    cooldown_s: float = 1800.0  # 同 (alert_type, symbol) 推送冷却
    hourly_cap: int = 20  # 全局小时推送上限（防过载）
    push_max_age_s: float = 900.0  # 事件过旧只留痕不推送（回放/积压防打扰）

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> SentinelConfig:
        m = dict(raw or {})

        def _b(key: str, default: bool) -> bool:
            v = m.get(key)
            if v is None or str(v).strip() == "":
                return default
            return str(v).strip().lower() in _TRUTHY

        level = str(m.get("push_level_min") or "warn").strip().lower()
        if level not in _SEVERITY_RANK:
            level = "warn"
        try:
            cooldown = float(m.get("cooldown_s"))
        except (TypeError, ValueError):
            cooldown = 1800.0
        try:
            cap = int(float(m.get("hourly_cap")))
        except (TypeError, ValueError):
            cap = 20
        try:
            max_age = float(m.get("push_max_age_s"))
        except (TypeError, ValueError):
            max_age = 900.0
        return cls(
            enabled=_b("enabled", False),
            push_level_min=level,
            cooldown_s=max(60.0, cooldown),
            hourly_cap=max(1, cap),
            push_max_age_s=max(0.0, max_age),
        )


def _main_redis():
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


def _load_config_sync() -> SentinelConfig:
    try:
        client = _main_redis()
        raw = client.hgetall(CONFIG_KEY) or {}
        client.close()
        return SentinelConfig.from_mapping(raw)
    except Exception:  # noqa: BLE001
        return SentinelConfig()


class SentinelAlertService:
    """总线 → 留痕 → 分级推送（依赖全注入，测试可整体桩化）。"""

    def __init__(
        self,
        *,
        config_loader: Callable[[], SentinelConfig] | None = None,
        redis_factory: Callable[[], Any] | None = None,
        notifier: Callable[..., bool] | None = None,
        record_fn: Callable[[dict[str, Any]], bool] | None = None,
        now_fn: Callable[[], float] = time.time,
    ) -> None:
        self._config_loader = config_loader or _load_config_sync
        self._redis_factory = redis_factory or _main_redis
        self._notifier = notifier or self._default_notify
        self._record_fn = record_fn or self._default_record
        self._now = now_fn
        self._lock = threading.Lock()
        self.counters: dict[str, Any] = {
            "cycles": 0,
            "scanned": 0,
            "recorded": 0,
            "duplicates": 0,
            "pushed": 0,
            "throttled_cooldown": 0,
            "throttled_rate": 0,
            "below_level": 0,
            "stale": 0,
            "malformed": 0,
            "record_errors": 0,
            "errors": 0,
            "last_error": None,
            "last_build_at": None,
        }

    # ── 留痕（PG）──────────────────────────────────────────────────

    def _default_record(self, row: dict[str, Any]) -> bool:
        from sqlalchemy import text

        from backend.shared.sentinel_alert_contract import ensure_sentinel_alerts_table
        from backend.shared.sync_db import sync_session

        if not ensure_sentinel_alerts_table():
            return False
        with sync_session() as session:
            result = session.execute(
                text(
                    "INSERT INTO sentinel_alerts "
                    "(dedupe_key, tenant_id, ts, trade_date, market, symbol, targets, "
                    " alert_type, severity, source, title, detail, direction) "
                    "VALUES (:dk, :t, to_timestamp(:ts), :d, :m, :s, CAST(:tg AS JSONB), "
                    "        :at, :sev, :src, :ti, CAST(:dt AS JSONB), :dir) "
                    "ON CONFLICT (dedupe_key) DO NOTHING"
                ),
                {
                    "dk": row["dedupe_key"],
                    "t": row["tenant_id"],
                    "ts": row["ts"],
                    "d": row["trade_date"],
                    "m": row["market"],
                    "s": row["symbol"],
                    "tg": json.dumps(row["targets"], ensure_ascii=False),
                    "at": row["alert_type"],
                    "sev": row["severity"],
                    "src": row["source"],
                    "ti": row["title"],
                    "dt": json.dumps(row["detail"], ensure_ascii=False, default=str),
                    "dir": row["direction"],
                },
            )
            session.commit()
        return bool(result.rowcount)

    def _mark_pushed(self, dedupe_key: str, reason: str) -> None:
        from sqlalchemy import text

        from backend.shared.sync_db import sync_session

        try:
            with sync_session() as session:
                session.execute(
                    text(
                        "UPDATE sentinel_alerts SET pushed = :p, push_reason = :r "
                        "WHERE dedupe_key = :dk"
                    ),
                    {"p": reason == "pushed", "r": reason, "dk": dedupe_key},
                )
                session.commit()
        except Exception as exc:  # noqa: BLE001
            self._note_error(f"mark pushed: {exc}")

    # ── 推送（管理员 fanout）────────────────────────────────────────

    def _default_notify(self, *, title: str, content: str, level: str) -> bool:
        # 管理员 fanout 口径与其它运维告警共用（notification_publisher），
        # 不再各自写 SQL（受众规则一改就得改多处，必漂移）。
        from backend.shared.notification_publisher import (
            publish_notification_to_admins,
        )

        try:
            delivered, audience = publish_notification_to_admins(
                title=title,
                content=content,
                type="sentinel",
                level=level,
            )
        except Exception as exc:  # noqa: BLE001 - 查询失败按无受众处理并留痕
            logger.warning("[sentinel] 推送失败: %s", exc)
            return False
        if audience == 0:
            logger.warning("[sentinel] 无管理员用户可推送: %s", title)
            return False
        return delivered > 0

    # ── 主循环 ─────────────────────────────────────────────────────

    def run_once(self) -> dict[str, Any]:
        cfg = self._config_loader()
        if not cfg.enabled:
            return {"enabled": False}
        from backend.shared.intel_events import (
            STREAM_KEY,
            ack_event,
            ensure_group,
            read_events,
        )

        client = self._redis_factory()
        scanned = recorded = pushed = 0
        try:
            # 新消费者从 "$" 起：只消费增量，不回放留存事件（防历史告警风暴）
            ensure_group(client, group=CONSUMER_GROUP, start_id="$")
            events = read_events(
                client,
                group=CONSUMER_GROUP,
                consumer=CONSUMER_NAME,
                count=100,
                block_ms=1500,
            )
            scanned = len(events)
            for msg_id, event in events:
                try:
                    if event.get("_malformed") is not None:
                        self._bump("malformed")
                        continue
                    row = self._build_row(msg_id, event)
                    if self._record_fn(row):
                        recorded += 1
                    else:
                        self._bump("duplicates")
                    reason = self._decide_push(cfg, client, row)
                    if reason == "pushed":
                        pushed += 1
                    self._mark_pushed(row["dedupe_key"], reason)
                except Exception as exc:  # noqa: BLE001 - 单条失败不卡组
                    self._note_error(f"event {msg_id}: {exc}")
                finally:
                    ack_event(client, msg_id, group=CONSUMER_GROUP)
            with self._lock:
                self.counters["cycles"] += 1
                self.counters["scanned"] += scanned
                self.counters["recorded"] += recorded
                self.counters["pushed"] += pushed
                self.counters["last_build_at"] = datetime.now(_SH_TZ).isoformat()
                snapshot = dict(self.counters)
            self._write_status(client, snapshot)
            return {
                "enabled": True,
                "scanned": scanned,
                "recorded": recorded,
                "pushed": pushed,
                "counters": snapshot,
            }
        finally:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass

    def _build_row(self, msg_id: str, event: Mapping[str, Any]) -> dict[str, Any]:
        payload = event.get("payload") or {}
        targets = [str(t) for t in (event.get("targets") or [])]
        symbol = targets[0] if targets else "*"
        etype = str(event.get("type") or "")
        kind = str(payload.get("kind") or "")
        alert_type = f"{etype}:{kind}" if kind else etype
        ts = float(event.get("ts") or self._now())
        trade_date = datetime.fromtimestamp(ts, tz=_SH_TZ).date().isoformat()
        title = str(
            payload.get("title")
            or payload.get("description")
            or f"{alert_type} {symbol}"
        ).strip()[:256]
        title_hash = hashlib.sha1(title.encode("utf-8")).hexdigest()[:16]
        from backend.shared.sentinel_alert_contract import (
            alert_direction,
            make_dedupe_key,
        )

        source = str(event.get("source") or "unknown")[:64]
        return {
            "dedupe_key": make_dedupe_key(
                source=source,
                alert_type=alert_type,
                symbol=symbol,
                trade_date=trade_date,
                title_hash=f"{title_hash}:{msg_id}",
            ),
            "tenant_id": "default",
            "ts": ts,
            "trade_date": trade_date,
            "market": str(event.get("market") or "CN"),
            "symbol": symbol,
            "targets": targets,
            "alert_type": alert_type,
            "severity": str(event.get("level") or "info"),
            "source": source,
            "title": title,
            "detail": {
                "payload": payload,
                "actions_hint": event.get("actions_hint") or [],
                "msg_id": msg_id,
            },
            "direction": alert_direction(alert_type, payload),
        }

    def _decide_push(
        self, cfg: SentinelConfig, client: Any, row: dict[str, Any]
    ) -> str:
        """推送裁决（纯逻辑+Redis 双闸门）：返回 push_reason。"""
        if (
            cfg.push_max_age_s > 0
            and (self._now() - float(row.get("ts") or 0)) > cfg.push_max_age_s
        ):
            self._bump("stale")
            return "stale"
        if _SEVERITY_RANK.get(row["severity"], 0) < _SEVERITY_RANK.get(
            cfg.push_level_min, 1
        ):
            self._bump("below_level")
            return "below_level"
        cd_key = f"{COOLDOWN_PREFIX}{row['alert_type']}:{row['symbol']}"
        try:
            if not client.set(cd_key, "1", nx=True, ex=int(cfg.cooldown_s)):
                self._bump("throttled_cooldown")
                return "throttled_cooldown"
        except Exception as exc:  # noqa: BLE001
            self._note_error(f"cooldown: {exc}")
        rate_key = f"{RATE_KEY_PREFIX}{datetime.now(_SH_TZ).strftime('%Y%m%d%H')}"
        try:
            used = int(client.incr(rate_key))
            client.expire(rate_key, 3700)
            if used > cfg.hourly_cap:
                self._bump("throttled_rate")
                return "throttled_rate"
        except Exception as exc:  # noqa: BLE001
            self._note_error(f"rate: {exc}")
        content = json.dumps(
            {
                "alert_type": row["alert_type"],
                "market": row["market"],
                "targets": row["targets"][:10],
                "detail": row["detail"].get("payload", {}),
            },
            ensure_ascii=False,
            default=str,
        )
        ok = False
        try:
            ok = bool(
                self._notifier(
                    title=f"[{row['severity']}] {row['title']}",
                    content=content,
                    level=_LEVEL_TO_NOTIFY.get(row["severity"], "info"),
                )
            )
        except Exception as exc:  # noqa: BLE001
            self._note_error(f"notify: {exc}")
        if not ok:
            return "no_audience"
        return "pushed"

    def _bump(self, key: str, delta: int = 1) -> None:
        with self._lock:
            self.counters[key] = int(self.counters.get(key) or 0) + int(delta)

    def _note_error(self, message: str) -> None:
        with self._lock:
            self.counters["errors"] += 1
            self.counters["last_error"] = str(message)[:200]
        logger.warning("[sentinel] %s", message)

    def _write_status(self, client: Any, counters: dict[str, Any]) -> None:
        try:
            client.hset(
                STATUS_KEY,
                mapping={
                    "last_build_at": str(counters.get("last_build_at") or ""),
                    "counters": json.dumps(counters, ensure_ascii=False, default=str),
                },
            )
            client.expire(STATUS_KEY, 86400)
        except Exception:  # noqa: BLE001
            pass


async def run_sentinel_alert_worker() -> None:
    """常驻消费循环（trade 服务注册；间隔 5s，Redis 门控热读）。"""
    service = SentinelAlertService()
    logger.info("[sentinel] 哨兵告警消费循环启动")
    while True:
        try:
            from backend.shared.scheduler_registry import heartbeat as _sched_heartbeat

            _sched_heartbeat("sentinel_push")
        except Exception:  # noqa: BLE001
            pass
        try:
            await asyncio.to_thread(service.run_once)
        except Exception as exc:  # noqa: BLE001 - 循环永不退出
            logger.warning("[sentinel] cycle failed: %s", exc)
        await asyncio.sleep(5)
