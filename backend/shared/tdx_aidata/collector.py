"""订阅帧 → Redis 标准键（T-P6-02）：纯映射 + 订阅引擎。

**键与字段契约（消费方零改动的前提，与 tdx_quote_feed/stream 完全一致）**：
- ``market:series:{SH600036}``（ZSET，score=epoch，成员 JSON，TTL 2 天、6000 点）
- ``market:snapshot:{sh600036}``（Hash，TTL 300s）**必写消费方契约字段**：
  ``Now/Open/PreClose/High/Low/Volume/timestamp``（见 stream ``RemoteRedisDataSource``），
  另附原始推送字段（pre_close/bid1..ask5/limit_up 等五档全景，供 F2/前端）。
- 写入目标 = 远端行情 Redis（``shared.remote_quote_config`` 唯一事实源，默认 db3），
  与消费方读取同一实例。

**订阅引擎（worker 内运行）**：热集符号集来自 Redis 集合（``qm:hot_set:symbols``，T-P6-06 维护；
测试可用 env 隔离键）→ 差分增量调整订阅（native subscribe 为全量替换语义：变更时
unsub-all + sub-new，受预算闸门约束）→ 帧经回调入队 → 异步批量写 Redis；
静默 > 阈值判定断流 → 强制重订阅（带最小间隔防抖动）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import queue
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from backend.shared.tdx_aidata import config, protocol

logger = logging.getLogger("tdx_aidata.collector")

CST = timezone(timedelta(hours=8))

SNAPSHOT_TTL = 300
SERIES_TTL = 172800
SERIES_MAX_POINTS = 6000
DEFAULT_HOT_SET_KEY = config.DEFAULT_HOT_SET_KEY
DEFAULT_CAP = 1000
DEFAULT_SILENCE_S = 120.0

_BID_ASK_FIELDS = tuple(
    [f"bid{i}" for i in range(1, 6)]
    + [f"bid_vol{i}" for i in range(1, 6)]
    + [f"ask{i}" for i in range(1, 6)]
    + [f"ask_vol{i}" for i in range(1, 6)]
)


# ── 纯映射 ──────────────────────────────────────────────────────────


def refresh_time_to_epoch(hhmmss: str, now: datetime) -> int | None:
    """帧的 refresh_time(HHMMSS) → epoch 秒（按 now 所在 CST 日期）。

    跨日守卫：解析结果晚于 now 超过 5 分钟 → 回退一天。非法输入 → None。
    """
    text = str(hhmmss or "").strip()
    if len(text) != 6 or not text.isdigit():
        return None
    local_now = now.astimezone(CST)
    try:
        dt = local_now.replace(
            hour=int(text[:2]), minute=int(text[2:4]), second=int(text[4:]),
            microsecond=0,
        )
    except ValueError:
        return None
    if (dt - local_now).total_seconds() > 300:
        dt -= timedelta(days=1)
    return int(dt.timestamp())


def normalize_subscription_symbol(symbol: object) -> tuple[str, str] | None:
    """任意形态代码 → (原生订阅码[后缀式], 标准键前缀[大写])；非法返回 None。"""
    try:
        from backend.shared.stock_utils import StockCodeUtil

        prefix = StockCodeUtil.to_prefix(str(symbol or "").strip())
    except Exception:  # noqa: BLE001
        return None
    prefix = str(prefix or "").strip().upper()
    if len(prefix) != 8 or prefix[:2] not in {"SH", "SZ", "BJ"} or not prefix[2:].isdigit():
        return None
    return f"{prefix[2:]}.{prefix[:2]}", prefix


def record_ts(record: dict, now: datetime | None = None) -> int:
    """记录时间戳（唯一实现）：显式 ts 优先，否则由 refresh_time(HHMMSS) 按 CST 换算。"""
    raw_ts = record.get("ts")
    if raw_ts is not None:
        try:
            return int(raw_ts)
        except (TypeError, ValueError):
            pass
    now = now or datetime.now(CST)
    ts = refresh_time_to_epoch(str(record.get("refresh_time") or ""), now)
    return int(ts if ts is not None else now.timestamp())


def frame_to_redis(record: dict, now: datetime) -> dict[str, Any] | None:
    """推送记录 → Redis 写入三件套；符号非法/关键价缺失 → None（调用方计数跳过）。"""
    norm = normalize_subscription_symbol(record.get("symbol"))
    if norm is None:
        return None
    _native_code, prefix = norm
    price = record.get("price")
    pre_close = record.get("pre_close")
    open_price = record.get("open")
    if not price or not pre_close:
        return None

    ts = record_ts(record, now)

    snapshot_fields: dict[str, str] = {
        # 消费方契约字段（stream RemoteRedisDataSource 必读）
        "Now": str(price),
        "Open": str(open_price if open_price is not None else price),
        "PreClose": str(pre_close),
        "timestamp": str(ts),
        "source": "tdx_aidata_sub",
        "symbol": prefix,
    }
    for src, dst in (
        ("high", "High"), ("low", "Low"), ("volume", "Volume"),
        ("limit_up", "LimitUp"), ("limit_down", "LimitDown"),
        ("seal_amount", "SealAmount"), ("open", "PreOpenRaw"),
    ):
        if record.get(src) is not None:
            snapshot_fields[dst] = str(record[src])
    for field in ("price", "pre_close") + _BID_ASK_FIELDS:
        if record.get(field) is not None:
            snapshot_fields[field] = str(record[field])

    series_payload: dict[str, Any] = {
        "symbol": prefix,
        "normalized_symbol": prefix,
        "timestamp": ts,
        "datetime": datetime.fromtimestamp(ts, tz=CST).isoformat(),
        "price": price,
        "open": snapshot_fields["Open"],
        "high": record.get("high"),
        "low": record.get("low"),
        "volume": record.get("volume"),
        "amount": record.get("amount"),
        "is_stale": False,
        "source": "tdx_aidata_sub",
        "limit_up": record.get("limit_up"),
        "limit_down": record.get("limit_down"),
    }
    for field in _BID_ASK_FIELDS:
        series_payload[field] = record.get(field)

    return {
        "prefix": prefix,
        "series_key": f"market:series:{prefix}",
        "snapshot_key": f"market:snapshot:{prefix.lower()}",
        "snapshot_fields": snapshot_fields,
        "series_payload": series_payload,
        "ts": ts,
    }


def hot_set_diff(
    current: set[str], desired: set[str], cap: int = DEFAULT_CAP
) -> tuple[set[str], set[str]]:
    """热集差分：desired 超上限按字典序确定性截断；返回 (add, remove)。"""
    desired_trimmed = set(sorted(desired)[: max(0, int(cap))])
    to_add = desired_trimmed - current
    to_remove = current - desired_trimmed
    return to_add, to_remove


def is_silent(last_frame_ts: float | None, now: float, silence_s: float) -> bool:
    """断流判定：从未收帧（None）不判静默（建立期宽限由调用方节奏控制）。"""
    if last_frame_ts is None:
        return False
    return (now - last_frame_ts) > silence_s


# ── 订阅引擎（worker 内运行）────────────────────────────────────────

# SDK 单次 subscribe 上限（实测 2026-09-17：100 只整批接受、101 只起整批拒绝——
# 错误码 2“股票代码错误”仅打印不抛、零帧到达；修复=热集分片多 worker，每片 ≤ 此值）
SDK_SUBSCRIBE_MAX = 100

# L0.5 归档时效闸门：只留「实时到达」的帧（age ≤ 此值）。夜间/停牌陈旧重放帧
# （2026-09-17 实测：收盘后服务器仍以 ~10s 推前日 15:30 快照，age 数小时）不落盘——
# 否则会污染当日分区为「迟到的一天」（如 09-16 分区只有 65 只收盘重放行）。
ARCHIVE_MAX_AGE_S = 300.0


def shard_symbols(
    symbols: set[str] | list[str], shard_id: int, shard_count: int
) -> list[str]:
    """确定性分片：crc32(symbol) % shard_count == shard_id（热集增删不重排，稳定）。"""
    ordered = sorted(symbols)
    if shard_count <= 1:
        return ordered
    import zlib

    n = int(shard_count)
    sid = int(shard_id) % n
    return [s for s in ordered if zlib.crc32(s.encode("utf-8")) % n == sid]


class SubscriptionEngine:
    """热集订阅 → 帧落 Redis 的常驻引擎。

    - ``sdk_subscribe``/``sdk_unsubscribe``：注入的原生调用（worker 持有 tqs；测试可注入桩）；
    - ``budget_gate``：worker 的配额闸门（订阅/退订调用同样计费——保守且如实）；
    - ``redis_factory``：同步 redis 客户端工厂（写侧，失败不阻断订阅）；
    - 全部计数在 ``snapshot()`` 可观测（frames/writes/resubscribes/skipped/silence…）。
    """

    def __init__(
        self,
        *,
        sdk_subscribe: Callable[[list[str], Callable[[str], int]], Any],
        sdk_unsubscribe: Callable[[], Any],
        budget_gate,
        redis_factory: Callable[[], Any] | None,
        archiver: Any | None = None,
        latency: Any | None = None,
        hot_set_key: str = DEFAULT_HOT_SET_KEY,
        cap: int = DEFAULT_CAP,
        silence_s: float = DEFAULT_SILENCE_S,
        sync_interval_s: float = 15.0,
        resubscribe_min_gap_s: float = 60.0,
        shard_id: int = 0,
        shard_count: int = 1,
    ) -> None:
        self._sdk_subscribe = sdk_subscribe
        self._sdk_unsubscribe = sdk_unsubscribe
        self._gate = budget_gate
        self._redis_factory = redis_factory
        self._archiver = archiver  # l05_store.SnapshotArchiver | None（L0.5 落盘 sink）
        self._latency = latency  # latency_metrics.LatencyRecorder | None（T-P6-05 时延打点）
        self._hot_set_key = hot_set_key
        self.cap = int(cap)
        self.silence_s = float(silence_s)
        self.sync_interval_s = float(sync_interval_s)
        self.resubscribe_min_gap_s = float(resubscribe_min_gap_s)
        self.shard_count = max(1, int(shard_count))
        self.shard_id = int(shard_id) % self.shard_count

        self._queue: queue.SimpleQueue = queue.SimpleQueue()
        self._current: set[str] = set()  # 原生码（后缀式）
        self.last_frame_ts: float | None = None
        self._last_resubscribe_ts = 0.0
        self._stop = asyncio.Event()
        self.counters: dict[str, Any] = {
            "frames": 0,
            "records": 0,
            "written": 0,
            "archived": 0,
            "skipped_symbols": 0,
            "parse_errors": 0,
            "redis_errors": 0,
            "resubscribes": 0,
            "resubscribe_deferred_gap": 0,  # 热集差分因最小间隔推迟（下轮收敛）
            "set_syncs": 0,
            "hot_set_size": 0,  # 全量热集规模（分片前）
            "over_cap": False,  # 本片超 SDK 上限被截断（显式降级，绝不静默）
            "archived_stale_skipped": 0,  # 陈旧重放帧未落盘计数（ARCHIVE_MAX_AGE_S 闸门）
            "last_error": None,
        }

    # 回调（SDK 线程）——只做解析入队，绝不阻塞原生线程
    def on_push(self, raw: str) -> int:
        try:
            records = protocol.parse_push_payload(raw)
        except Exception as exc:  # noqa: BLE001
            self.counters["parse_errors"] += 1
            self.counters["last_error"] = f"push parse: {exc}"
            self.counters["last_frame_preview"] = str(raw)[:120]
            return 1
        self.counters["frames"] += 1
        self.counters["last_frame_preview"] = str(raw)[:120]
        self.counters["last_frame_records"] = len(records)
        if records:
            self.counters["frames_data"] = self.counters.get("frames_data", 0) + 1
        else:
            # 元帧（ColDes 全列、Content 空行——订阅 ACK/心跳型），无数据内容
            self.counters["frames_meta"] = self.counters.get("frames_meta", 0) + 1
        self.last_frame_ts = time.time()
        for record in records:
            self._queue.put(record)
        return 1

    # ── redis 写侧 ──────────────────────────────────────────────────

    def _drain_and_write(self) -> int:
        """队列→(Redis 写 | L0.5 归档 | 时延打点) 批量消费。

        三路互相独立：Redis 写失败（或 redis_factory=None）不阻断归档与打点；
        归档/打点失败只计数（各自内部吞错），不阻断实时链。
        """
        batch: list[dict] = []
        while len(batch) < 500:
            try:
                batch.append(self._queue.get_nowait())
            except queue.Empty:
                break
        if not batch:
            return 0

        now = datetime.now(CST)
        now_epoch = now.timestamp()
        mapped_records: list[tuple[dict, dict]] = []
        for record in batch:
            mapped = frame_to_redis(record, now)
            self.counters["records"] += 1
            if mapped is None:
                self.counters["skipped_symbols"] += 1
                continue
            mapped_records.append((record, mapped))
        if not mapped_records:
            return 0

        if self._latency is not None:
            # 时延打点：源时间戳（水位）→ 本批可消费（写标准键/归档）时刻
            for _record, mapped in mapped_records:
                self._latency.observe((now_epoch - mapped["ts"]) * 1000.0)
            self._latency.maybe_flush()

        archived = [{**record, "ts": mapped["ts"]} for record, mapped in mapped_records]
        written = len(mapped_records)

        if self._redis_factory is not None:
            client = None
            try:
                client = self._redis_factory()
                pipe = client.pipeline(transaction=False)
                for _record, mapped in mapped_records:
                    pipe.hset(mapped["snapshot_key"], mapping=mapped["snapshot_fields"])
                    pipe.expire(mapped["snapshot_key"], SNAPSHOT_TTL)
                    pipe.zadd(
                        mapped["series_key"],
                        {json.dumps(mapped["series_payload"], ensure_ascii=False): mapped["ts"]},
                    )
                    pipe.zremrangebyrank(mapped["series_key"], 0, -(SERIES_MAX_POINTS + 1))
                    pipe.expire(mapped["series_key"], SERIES_TTL)
                pipe.execute()
                self.counters["written"] += written
            except Exception as exc:  # noqa: BLE001
                self.counters["redis_errors"] += 1
                self.counters["last_error"] = f"redis write: {exc}"
                logger.warning("订阅写 Redis 失败: %s", exc)
            finally:
                if client is not None:
                    try:
                        client.close()
                    except Exception:  # noqa: BLE001
                        pass

        if self._archiver is not None and archived:
            fresh_records = []
            for record in archived:
                age = now_epoch - record["ts"]
                if 0.0 <= age <= ARCHIVE_MAX_AGE_S:
                    fresh_records.append(record)
                else:
                    self.counters["archived_stale_skipped"] += 1
            for record in fresh_records:
                self._archiver.append(record)
            self.counters["archived"] += len(fresh_records)
        return written

    # ── 热集同步 ────────────────────────────────────────────────────

    def _read_hot_set(self) -> set[str] | None:
        if self._redis_factory is None:
            return None
        client = None
        try:
            client = self._redis_factory()
            members = client.smembers(self._hot_set_key) or set()
            out = set()
            for m in members:
                norm = normalize_subscription_symbol(m)
                if norm is not None:
                    out.add(norm[0])
            return out
        except Exception as exc:  # noqa: BLE001
            self.counters["last_error"] = f"hot_set read: {exc}"
            return None
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:  # noqa: BLE001
                    pass

    def resubscribe(self, symbols: set[str]) -> bool:
        """native 全量替换：unsub-all + sub-new。

        **不占请求预算（2026-09-17 盘中事故修正）**：订阅通道实测**零请求配额**
        （T-P6-01 探针：订阅期间请求仍受限流但订阅本身不耗窗口；盘中多分片反复重订
        亦无 Token Insufficient）。旧版把订阅计入「3 次/窗口」请求闸门——热集每 60s
        漂移时预算瞬耗 → 重订被 defer 饿死 → 全片静默（叠加 tqs 累积 bug 后无法自愈）。
        订阅节流改由两道自有约束：热集差分最小间隔（sync_hot_set_once）+ 静默重订
        最小间隔（resubscribe_min_gap_s，run 循环）。
        """
        try:
            if self._current:
                try:
                    self._sdk_unsubscribe()
                except Exception:  # noqa: BLE001 - 空集/首订时 unsub 报错容忍
                    pass
            if symbols:
                self._sdk_subscribe(sorted(symbols), self.on_push)
        except Exception as exc:  # noqa: BLE001
            code, msg = protocol.map_sdk_error(exc)
            self.counters["last_error"] = f"resubscribe: {msg}"
            if code == "rate_limited":
                self._gate.note_rate_limited()
            return False
        self._current = set(symbols)
        self._last_resubscribe_ts = time.time()
        self.counters["resubscribes"] += 1
        return True

    def _shard_desired(self, full: set[str]) -> set[str]:
        """全量热集 → 本片应订集合（哈希分片 + SDK 上限硬截断，超限显式计数）。"""
        self.counters["hot_set_size"] = len(full)
        desired = shard_symbols(full, self.shard_id, self.shard_count)
        if len(desired) > SDK_SUBSCRIBE_MAX:
            self.counters["over_cap"] = True
            logger.error(
                "分片 %d/%d 应订 %d 只超 SDK 上限 %d：截断（请上调 QM_SUB_SHARDS）",
                self.shard_id, self.shard_count, len(desired), SDK_SUBSCRIBE_MAX,
            )
            desired = desired[:SDK_SUBSCRIBE_MAX]
        else:
            self.counters["over_cap"] = False
        return set(desired)

    def sync_hot_set_once(self) -> dict[str, Any]:
        """一次热集差分同步（供 run 循环与手动触发复用；含本片分片过滤）。"""
        full = self._read_hot_set()
        if full is None:
            return {"changed": False, "reason": "hot_set_unreadable"}
        desired = self._shard_desired(full)
        self.counters["set_syncs"] += 1
        if desired == self._current:
            return {"changed": False, "current": len(self._current)}
        to_add, to_remove = hot_set_diff(self._current, desired, cap=SDK_SUBSCRIBE_MAX)
        if not to_add and not to_remove:
            return {"changed": False, "current": len(self._current)}
        # 热集差分重订最小间隔（防 churn 抖动；订阅不占请求预算后的自有节流，见 resubscribe）
        now = time.time()
        if self._last_resubscribe_ts and now - self._last_resubscribe_ts < self.resubscribe_min_gap_s:
            self.counters["resubscribe_deferred_gap"] += 1
            return {"changed": False, "reason": "min_gap", "current": len(self._current)}
        ok = self.resubscribe(desired)
        return {
            "changed": ok,
            "current": len(self._current),
            "add": len(to_add),
            "remove": len(to_remove),
        }

    # ── 主循环 ──────────────────────────────────────────────────────

    async def run(self) -> None:
        logger.info(
            "subscription engine started hot_set=%s shard=%d/%d silence=%.0fs",
            self._hot_set_key, self.shard_id, self.shard_count, self.silence_s,
        )
        while not self._stop.is_set():
            try:
                await asyncio.to_thread(self._drain_and_write)
                await asyncio.to_thread(self.sync_hot_set_once)
                # 静默断流 → 强制重订阅（带最小间隔防抖动）
                now = time.time()
                if (
                    self._current
                    and is_silent(self.last_frame_ts, now, self.silence_s)
                    and now - self._last_resubscribe_ts >= self.resubscribe_min_gap_s
                ):
                    logger.warning(
                        "订阅静默 %.0fs，强制重订阅 %d 只",
                        now - (self.last_frame_ts or now), len(self._current),
                    )
                    await asyncio.to_thread(self.resubscribe, set(self._current))
            except Exception as exc:  # noqa: BLE001 - 循环永不退出
                self.counters["last_error"] = f"engine loop: {exc}"
                logger.error("订阅引擎循环异常: %s", exc, exc_info=True)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.sync_interval_s)
            except asyncio.TimeoutError:
                pass

    def stop(self) -> None:
        self._stop.set()

    def snapshot(self) -> dict[str, Any]:
        now = time.time()
        out: dict[str, Any] = {
            "enabled": True,
            "hot_set_key": self._hot_set_key,
            "shard": {"id": self.shard_id, "count": self.shard_count},
            "subscribed": len(self._current),
            "subscribed_sample": sorted(self._current)[:10],
            "last_frame_age_s": (
                round(now - self.last_frame_ts, 1) if self.last_frame_ts else None
            ),
            "silent": is_silent(self.last_frame_ts, now, self.silence_s),
            "counters": dict(self.counters),
        }
        if self._archiver is not None:
            out["archiver"] = {
                "base_dir": self._archiver.base_dir,
                "pending_rows": self._archiver.pending_rows,
                **self._archiver.counters,
            }
        if self._latency is not None:
            out["latency"] = self._latency.snapshot()
        return out
