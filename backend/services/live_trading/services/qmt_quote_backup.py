"""大 QMT 备源行情（T-P6-02 备源席）：全推订阅 → 标准键**旁路写**。

**定位与席位语义（2026-09-17 用户拍板实施）**：
- 主源 = TdxAiData 订阅（写 ``market:snapshot:``/``market:series:``，source=tdx_aidata_sub）；
- 本模块 = **备源**：静默订阅大 QMT 全推（``ContextInfo.subscribe_whole_quote``，服务端
  订阅管理器随 RPC runtime **默认常开**——Windows 侧零改动），**仅当标准键缺失或主源陈旧
  超过 ``stale_after_s``（默认 150s > 桥轮转一圈 ~102s，保证单键同一时刻只有一个写席）时才写**
  （standby 席位：避免双源交错抖动），写入 source=``qmt_big``，
  消费方契约字段（Now/Open/PreClose/High/Low/Volume/Amount/timestamp + 五档）与主源同构。
- 桥离线（Windows 未开机/QMT 未登录）→ 如实记 ``last_error`` + 指数退避重试，**绝不假装有数据**。

**实现要点**：
- RPC 与推送都走桥的 Redis（``broker:config:qmt_exec`` 的 redis_*，现网 db5）；全推为**增量回调**
  （仅变化标的），订阅时会另有一发全量 prime（big-convert 会话自带）；
- 心跳/断线重放由 big-convert ``WholeQuoteClientSession`` 自管；本模块只管：**热集差分订阅维护**
  + tick→标准键映射 + 席位写 + 时延打点（独立 stage ``market_snapshot_qmt``，不混主源口径）
  + 状态面（``qm:qmt:quote:backup:status``，含桥在线态）。
- 量纲说明：QMT volume/amount 为原样透传（volume=手口径、amount=元），备源定位=可用性兜底，
  与主源逐值对齐不在本席范围（消费方按 ``source`` 字段可区分）。

**测试**：U（映射/席位判定矩阵）＋ I（**双半真链路**：kit 真服务端管理器 + 真客户端会话，
经真 Redis pub/sub 推送 → 席位写断言；主源新鲜跳过/陈旧接管/桥离线降级）。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

CST = timezone(timedelta(hours=8))
CONFIG_KEY = "qm:qmt:quote:backup:config"
STATUS_KEY = "qm:qmt:quote:backup:status"
BACKUP_SOURCE = "qmt_big"

#: 桥热集一整圈的耗时参考：529 只 × 0.18s/只 ≈ 95s，实测 ~102s（桥限流下会略拉长）。
#: **接管阈值必须大于它**——否则桥刚写完的键下一拍就被判「陈旧」而被本备源接管，
#: 同一 key 两个写席轮流易主，消费侧表现为来源标签与现价来回跳
#:（2026-09-20 实况：阈值 30s < 轮转 ~100s；docs/P6实时轨_实施细案.md 已记为待办「上调 ≥150s」）。
BRIDGE_ROTATION_REFERENCE_S = 102.0
#: 默认接管阈值：> 轮转一圈并留约 50% 余量（容忍桥限流拉长/漏一圈）；
#: 仍 < freshness 的 300s 可用线，故桥真离线时接管前的旧值只标 stale，不会显示为不可用。
DEFAULT_STALE_AFTER_S = 150.0
BACKUP_LATENCY_STAGE = "market_snapshot_qmt"
SNAPSHOT_TTL = 300
SERIES_TTL = 172800
SERIES_MAX_POINTS = 6000

_BID_ASK_PAIRS = (
    ("bid1", "bidPrice", 0),
    ("bid2", "bidPrice", 1),
    ("bid3", "bidPrice", 2),
    ("bid4", "bidPrice", 3),
    ("bid5", "bidPrice", 4),
    ("ask1", "askPrice", 0),
    ("ask2", "askPrice", 1),
    ("ask3", "askPrice", 2),
    ("ask4", "askPrice", 3),
    ("ask5", "askPrice", 4),
    ("bid_vol1", "bidVol", 0),
    ("bid_vol2", "bidVol", 1),
    ("bid_vol3", "bidVol", 2),
    ("bid_vol4", "bidVol", 3),
    ("bid_vol5", "bidVol", 4),
    ("ask_vol1", "askVol", 0),
    ("ask_vol2", "askVol", 1),
    ("ask_vol3", "askVol", 2),
    ("ask_vol4", "askVol", 3),
    ("ask_vol5", "askVol", 4),
)


def _f(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out or out in (float("inf"), float("-inf")):
        return None
    return out


def _arr(tick: dict[str, Any], name: str, idx: int) -> float | None:
    seq = tick.get(name)
    if isinstance(seq, (list, tuple)) and idx < len(seq):
        return _f(seq[idx])
    return None


# ── 纯函数：tick → record / 席位判定 / 载荷构建 ─────────────────────────


def tick_ts(tick: dict[str, Any], now: float) -> float:
    """tick 时间（QMT ``time`` 毫秒）→ epoch 秒；缺失/离谱（±2 天外）→ now。"""
    raw = _f(tick.get("time"))
    if raw is None or raw <= 0:
        return now
    seconds = raw / 1000.0 if raw > 1e11 else raw  # 毫秒/秒自适应
    if abs(now - seconds) > 2 * 86400:
        return now
    return seconds


def qmt_tick_to_record(
    code: str, tick: dict[str, Any], now: float
) -> dict[str, Any] | None:
    """单个 QMT full_tick → 内部 record（消费方契约口径）；无效 → None。

    必需：lastPrice>0 且 lastClose>0；五档由 bidPrice/askPrice 数组展开（缺档位即缺字段）。
    """
    from backend.shared.stock_utils import StockCodeUtil

    if not isinstance(tick, dict):
        return None
    price = _f(tick.get("lastPrice"))
    pre_close = _f(tick.get("lastClose"))
    if not price or price <= 0 or not pre_close or pre_close <= 0:
        return None
    try:
        prefix = (
            str(StockCodeUtil.to_prefix(str(code or "").strip()) or "").strip().upper()
        )
    except Exception:  # noqa: BLE001
        return None
    if (
        len(prefix) != 8
        or prefix[:2] not in {"SH", "SZ", "BJ"}
        or not prefix[2:].isdigit()
    ):
        return None
    record: dict[str, Any] = {
        "symbol": prefix,
        "price": price,
        "pre_close": pre_close,
        "open": _f(tick.get("open")),
        "high": _f(tick.get("high")),
        "low": _f(tick.get("low")),
        "volume": _f(tick.get("volume")),
        "amount": _f(tick.get("amount")),
        "ts": tick_ts(tick, now),
        "source": BACKUP_SOURCE,
    }
    for field, arr_name, idx in _BID_ASK_PAIRS:
        value = _arr(tick, arr_name, idx)
        if value is not None:
            record[field] = value
    return record


def standby_decision(
    existing: dict[str, Any] | None, *, now: float, stale_after_s: float
) -> tuple[bool, str]:
    """席位判定（纯函数）：(写? , 原因)。

    - 键不存在 → 写（absent）；
    - 现值为**本备源自己**所写 → 续写（backup_owns；否则自家写的会把自己挡在门外）；
    - 现值 source=主源且 age ≤ stale_after_s → 不写（primary_fresh）；
    - 其余（陈旧/无 ts/他源）→ 写（primary_stale）。
    """
    if not existing:
        return True, "absent"
    source = str(existing.get("source") or "")
    ts = _f(existing.get("timestamp"))
    if source == BACKUP_SOURCE:
        return True, "backup_owns"
    age = (now - ts) if ts and ts > 0 else float("inf")
    if age <= stale_after_s:
        return False, "primary_fresh"
    return True, "primary_stale"


def build_snapshot_fields(record: dict[str, Any]) -> dict[str, str]:
    """内部 record → 标准键 Hash 字段（与主源同构 + 五档缺口如实缺省）。"""
    fields: dict[str, str] = {
        "Now": str(record["price"]),
        "Open": str(
            record.get("open") if record.get("open") is not None else record["price"]
        ),
        "PreClose": str(record["pre_close"]),
        "timestamp": str(int(record["ts"])),
        "source": BACKUP_SOURCE,
        "symbol": record["symbol"],
    }
    for src, dst in (
        ("high", "High"),
        ("low", "Low"),
        ("volume", "Volume"),
        ("amount", "Amount"),
    ):
        if record.get(src) is not None:
            fields[dst] = str(record[src])
    for field, _arr_name, _idx in _BID_ASK_PAIRS:
        if record.get(field) is not None:
            fields[field] = str(record[field])
    return fields


def build_series_payload(record: dict[str, Any]) -> dict[str, Any]:
    ts = int(record["ts"])
    payload: dict[str, Any] = {
        "symbol": record["symbol"],
        "normalized_symbol": record["symbol"],
        "timestamp": ts,
        "datetime": datetime.fromtimestamp(ts, tz=CST).isoformat(),
        "price": record["price"],
        "open": record.get("open"),
        "high": record.get("high"),
        "low": record.get("low"),
        "volume": record.get("volume"),
        "amount": record.get("amount"),
        "is_stale": False,
        "source": BACKUP_SOURCE,
    }
    for field, _arr_name, _idx in _BID_ASK_PAIRS:
        payload[field] = record.get(field)
    return payload


@dataclass
class BackupConfig:
    enabled: bool = False
    #: 主源陈旧判定阈值（秒）：默认 120 > 桥轮转一圈 95s，保证单键同一时刻只有一个写席
    stale_after_s: float = DEFAULT_STALE_AFTER_S
    symbols_refresh_s: float = 300.0
    poll_interval_s: float = 30.0

    @classmethod
    def from_mapping(cls, raw: dict[str, Any] | None) -> BackupConfig:
        raw = raw or {}

        def _num(key: str, default: float) -> float:
            try:
                return float(raw.get(key) or default)
            except (TypeError, ValueError):
                return default

        return cls(
            enabled=str(raw.get("enabled") or "").strip().lower()
            in {"1", "true", "yes", "on"},
            stale_after_s=max(5.0, _num("stale_after_s", DEFAULT_STALE_AFTER_S)),
            symbols_refresh_s=max(30.0, _num("symbols_refresh_s", 300.0)),
            poll_interval_s=max(5.0, _num("poll_interval_s", 30.0)),
        )


def _main_redis():
    import os

    import redis as redis_lib

    return redis_lib.Redis(
        host=os.getenv("REDIS_HOST") or "redis",
        port=int(os.getenv("REDIS_PORT", "6379")),
        db=int(os.getenv("REDIS_DB", "0")),
        password=os.getenv("REDIS_PASSWORD") or None,
        decode_responses=True,
        socket_connect_timeout=3,
        socket_timeout=5,
    )


def _load_config_sync() -> BackupConfig:
    try:
        client = _main_redis()
        raw = client.hgetall(CONFIG_KEY) or {}
        client.close()
        cfg = BackupConfig.from_mapping(raw)
        if cfg.enabled and cfg.stale_after_s < BRIDGE_ROTATION_REFERENCE_S:
            logger.warning(
                "[QmtQuoteBackup] stale_after_s=%.0fs < 桥轮转一圈 %.0fs —— 备源会与桥轮流"
                "接管同一批键（来源/现价来回跳）；建议 ≥ %.0fs",
                cfg.stale_after_s,
                BRIDGE_ROTATION_REFERENCE_S,
                DEFAULT_STALE_AFTER_S,
            )
        return cfg
    except Exception:  # noqa: BLE001
        return BackupConfig()


class QmtQuoteBackupService:
    """备源席位常驻服务（trade 服务内任务；注入式依赖便于双半真链路测试）。"""

    def __init__(
        self,
        *,
        config_loader: Callable[[], BackupConfig] | None = None,
        hot_set_fetcher: Callable[[], list[str]] | None = None,
        subscribe_fn: Callable[[list[str], Callable], Any] | None = None,
        unsubscribe_fn: Callable[[Any], Any] | None = None,
        writer_client_factory: Callable[[], Any] | None = None,
        latency_recorder: Any | None = None,
    ) -> None:
        self._config_loader = config_loader or _load_config_sync
        self._hot_set_fetcher = hot_set_fetcher or self._default_hot_set
        self._subscribe_fn = subscribe_fn
        self._unsubscribe_fn = unsubscribe_fn
        self._writer_factory = writer_client_factory
        self._latency = latency_recorder
        self._lock = threading.Lock()
        self._sub_id: Any = None
        self._subscribed_codes: list[str] = []
        self._last_symbols_refresh = 0.0
        self._bridge_ok = False
        self._backoff_s = 0.0
        self.counters: dict[str, Any] = {
            "batches": 0,
            "records_mapped": 0,
            "records_invalid": 0,
            "written": 0,
            "skipped_fresh": 0,
            "write_errors": 0,
            "last_push_ts": None,
            "last_error": None,
            "last_error_ts": None,
        }
        self._writer: Any = None
        self._latency_started = False

    # ── 默认依赖 ────────────────────────────────────────────────────

    def _default_hot_set(self) -> list[str]:
        from backend.shared.hot_set_store import make_hot_set_client, hot_set_key

        client = make_hot_set_client()
        try:
            return sorted(client.smembers(hot_set_key()) or [])
        finally:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass

    def _default_writer(self):
        from backend.shared.remote_quote_config import make_sync_client

        return make_sync_client()

    def _ensure_latency(self) -> None:
        if self._latency is not None or self._latency_started:
            return
        try:
            from backend.shared.latency_metrics import LatencyRecorder

            self._latency = LatencyRecorder(BACKUP_LATENCY_STAGE)
            self._latency_started = True
        except Exception as exc:  # noqa: BLE001
            logger.warning("[QmtQuoteBackup] 时延打点初始化失败: %s", exc)
            self._latency_started = True  # 不重试风暴

    def _client(self):
        if self._writer is None:
            self._writer = (self._writer_factory or self._default_writer)()
        return self._writer

    def _default_configure(self) -> None:
        """按页面配置 configure big-convert 全局客户端（与执行端同源设定）。"""
        from bigqmt_signal_trader import xtquant_compat as compat

        from backend.services.live_trading.services.qmt_exec_client import (
            load_broker_settings,
        )

        s = load_broker_settings()
        redis_config: dict[str, Any] = {}
        if s.get("redis_host"):
            redis_config = {
                "host": str(s.get("redis_host")),
                "port": int(s.get("redis_port") or 6379),
                "db": int(s.get("redis_db") or 0),
            }
            if s.get("redis_password"):
                redis_config["password"] = str(s.get("redis_password"))
        compat.configure(
            account_id=str(s.get("account_id") or ""),
            redis_config=redis_config or None,
            timeout_seconds=float(s.get("timeout") or 25.0),
        )

    def _default_subscribe(self, codes: list[str], callback: Callable) -> Any:
        from bigqmt_signal_trader import xtquant_compat as compat

        self._default_configure()
        return compat.xtdata.subscribe_whole_quote(codes, callback)

    def _default_unsubscribe(self, sub_id: Any) -> None:
        from bigqmt_signal_trader import xtquant_compat as compat

        compat.xtdata.unsubscribe_quote(sub_id)

    # ── 回调（会话订阅线程驱动，绝不让异常穿出）────────────────────────

    def on_ticks(self, ticks: Any) -> None:
        """全推批次（prime 全量 + 增量同形）：{code: tick} → 席位写标准键。"""
        try:
            self._handle_ticks(ticks)
        except Exception as exc:  # noqa: BLE001 - 回调线程必须存活
            with self._lock:
                self.counters["write_errors"] += 1
                self.counters["last_error"] = f"on_ticks: {type(exc).__name__}: {exc}"
            logger.warning("[QmtQuoteBackup] 批次处理异常: %s", exc, exc_info=True)

    def _handle_ticks(self, ticks: Any) -> None:
        if not isinstance(ticks, dict) or not ticks:
            return
        now = time.time()
        cfg = self._config_loader()
        client = self._client()
        if client is None:
            with self._lock:
                self.counters["last_error"] = "远端行情 Redis 不可用"
            return

        mapped: list[dict[str, Any]] = []
        invalid = 0
        for code, tick in ticks.items():
            record = qmt_tick_to_record(str(code), tick or {}, now)
            if record is None:
                invalid += 1
                continue
            mapped.append(record)
        if not mapped:
            with self._lock:
                self.counters["batches"] += 1
                self.counters["records_invalid"] += invalid
                self.counters["last_push_ts"] = now
            return

        # 席位判定：读取现值（同批 pipeline）
        snap_keys = [f"market:snapshot:{r['symbol'].lower()}" for r in mapped]
        pipe = client.pipeline(transaction=False)
        for key in snap_keys:
            pipe.hgetall(key)
        existing_rows = pipe.execute() or []

        write_plan: list[dict[str, Any]] = []
        skipped = 0
        for record, _key, existing in zip(
            mapped, snap_keys, existing_rows, strict=False
        ):
            ok, _reason = standby_decision(
                existing, now=now, stale_after_s=cfg.stale_after_s
            )
            if ok:
                write_plan.append(record)
            else:
                skipped += 1
        if write_plan:
            pipe = client.pipeline(transaction=False)
            for record in write_plan:
                prefix = record["symbol"]
                snap_key = f"market:snapshot:{prefix.lower()}"
                series_key = f"market:series:{prefix}"
                pipe.hset(snap_key, mapping=build_snapshot_fields(record))
                pipe.expire(snap_key, SNAPSHOT_TTL)
                pipe.zadd(
                    series_key,
                    {
                        json.dumps(
                            build_series_payload(record), ensure_ascii=False
                        ): int(record["ts"])
                    },
                )
                pipe.zremrangebyrank(series_key, 0, -(SERIES_MAX_POINTS + 1))
                pipe.expire(series_key, SERIES_TTL)
            pipe.execute()
        if self._latency is None:
            self._ensure_latency()
        if self._latency is not None:
            for record in write_plan:
                self._latency.observe((now - float(record["ts"])) * 1000.0)
            self._latency.maybe_flush()
        with self._lock:
            self.counters["batches"] += 1
            self.counters["records_mapped"] += len(mapped)
            self.counters["records_invalid"] += invalid
            self.counters["written"] += len(write_plan)
            self.counters["skipped_fresh"] += skipped
            self.counters["last_push_ts"] = now

    # ── 订阅维护 ────────────────────────────────────────────────────

    def sync_subscription(self) -> None:
        """热集差分 → 订阅集维护（变更才重订；桥离线 → 指数退避）。"""
        now = time.monotonic()
        cfg = self._config_loader()
        if (
            now - self._last_symbols_refresh < cfg.symbols_refresh_s
            and self._sub_id is not None
        ):
            return
        self._last_symbols_refresh = now
        codes = self._hot_set_fetcher()
        if not codes:
            with self._lock:
                self.counters["last_error"] = "热集为空（远端 Redis 不可读?）"
            return
        if codes == self._subscribed_codes and self._sub_id is not None:
            return
        if now < self._backoff_s:
            return
        subscribe = self._subscribe_fn or self._default_subscribe
        try:
            old_id = self._sub_id
            sub_id = subscribe(codes, self.on_ticks)
            if self._unsubscribe_fn is not None:
                unsubscribe = self._unsubscribe_fn
            else:
                unsubscribe = self._default_unsubscribe
            if old_id is not None:
                try:
                    unsubscribe(old_id)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[QmtQuoteBackup] 旧订阅退订失败（忽略）: %s", exc)
            with self._lock:
                self._sub_id = sub_id
                self._subscribed_codes = list(codes)
                self._bridge_ok = True
                self._backoff_s = 0.0
                self.counters["last_error"] = None
            logger.info(
                "[QmtQuoteBackup] 备源订阅建立 codes=%d sub_id=%s", len(codes), sub_id
            )
        except Exception as exc:  # noqa: BLE001 - 桥离线属常态
            wait = min(300.0, max(60.0, self._backoff_s * 2 or 60.0))
            self._backoff_s = time.monotonic() + wait
            with self._lock:
                self._bridge_ok = False
                self.counters["last_error"] = f"subscribe: {type(exc).__name__}: {exc}"
                self.counters["last_error_ts"] = time.time()
            logger.warning("[QmtQuoteBackup] 桥不可达，%ss 后重试: %s", int(wait), exc)

    def status(self) -> dict[str, Any]:
        cfg = self._config_loader()
        now = time.time()
        with self._lock:
            last_push = self.counters.get("last_push_ts")
            return {
                "enabled": cfg.enabled,
                "stale_after_s": cfg.stale_after_s,
                "bridge_ok": self._bridge_ok,
                "subscribed": len(self._subscribed_codes),
                "last_push_age_s": (
                    round(now - float(last_push), 1) if last_push else None
                ),
                **dict(self.counters),
            }

    def publish_status(self) -> None:
        try:
            client = _main_redis()
            status = self.status()
            client.hset(
                STATUS_KEY,
                mapping={
                    k: json.dumps(v, ensure_ascii=False)
                    if isinstance(v, (dict, list))
                    else str(v)
                    for k, v in status.items()
                },
            )
            client.expire(STATUS_KEY, 86400)
            client.close()
        except Exception:  # noqa: BLE001
            pass

    # ── 常驻循环 ────────────────────────────────────────────────────

    async def run_forever(self, stop_event: Any | None = None) -> None:
        import asyncio

        if stop_event is None:
            stop_event = asyncio.Event()
        logger.info("[QmtQuoteBackup] 备源席位循环启动")
        while not stop_event.is_set():
            cfg = self._config_loader()
            try:
                if cfg.enabled:
                    await asyncio.to_thread(self.sync_subscription)
                    self.publish_status()
            except Exception as exc:  # noqa: BLE001 - 循环永续
                with self._lock:
                    self.counters["last_error"] = f"loop: {type(exc).__name__}: {exc}"
                logger.warning("[QmtQuoteBackup] 循环异常: %s", exc)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=cfg.poll_interval_s)
            except asyncio.TimeoutError:
                pass
        logger.info("[QmtQuoteBackup] 备源席位循环退出")


def get_backup_service() -> QmtQuoteBackupService:
    """进程内单例（trade 服务任务使用）。"""
    global _singleton
    if _singleton is None:
        _singleton = QmtQuoteBackupService()
    return _singleton


_singleton: QmtQuoteBackupService | None = None
