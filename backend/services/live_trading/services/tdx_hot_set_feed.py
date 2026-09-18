"""TDX 桥·热集行情轮询（2026-09-17 用户拍板启用）：桥(13:8550) → 标准键全热集覆盖。

**为什么**：TdxAiData 订阅推送盘中零数据帧（全天 26 秒）、QMT 备源待 Windows 重载——
而用户在 Windows 上的**通达信桥（tqcenter, .13:8550, L2 会员）**有真实实时行情。本任务把
热集（本地 `qm:hot_set:symbols`，527+）以**预算内轮转**方式经桥取快照写入标准键，恢复
`market:snapshot/series` 的落地率与新鲜度（T-P6-02 桥源席），从而解锁：覆盖率闸门→实时
推理、regime live、模拟撮合取价、副驾驶事件面。

**预算纪律**：桥 `rate_limit_per_minute=600` 且与持仓馈送（~40/min）/账户同步/推送共用。
本任务节奏 `TDX_HOTSET_PACING_S`（默认 0.18s/只 ≈ 333/min），留 >200/min 余量；撞
RATE_LIMITED → 指数退避（30s→300s 封顶），不当作故障。

**交易时段门**：与持仓馈送同源（`is_trading_time`，含集合竞价，午休停拉）。

**写侧**：复用 `tdx_quote_feed.map_snapshot` / `_write_snapshot`（同源契约：小写前缀
snapshot + 大写前缀 series，source=tdx_bridge）；本模块扩展五档映射（Buyp/Buyv/Sellp/Sellv
→ bid1-5/ask1-5，单位=手与 TDX 帧一致，F2 内核按 ×100 归一为股）。**L0.5 同源归档**：
写键成功的快照同步喂 `l05_store.SnapshotArchiver`（tag=bridge，行契约与订阅侧一致），
补齐订阅零帧期间空转的 T-P6-04 落盘与 T-P6-09 回放原料。
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime
from typing import Any

from backend.shared.stock_utils import StockCodeUtil
from backend.services.live_trading.services.tdx_push_service import tdx_pusher
from backend.services.live_trading.services.tdx_quote_feed import (
    OFF_HOURS_SLEEP,
    TZ,
    _now_sh,
    _write_snapshot,
    is_trading_time,
    map_snapshot,
)

logger = logging.getLogger(__name__)

PACING_S = max(0.05, float(os.getenv("TDX_HOTSET_PACING_S", "0.18")))
BATCH_PER_LOOP = max(1, int(os.getenv("TDX_HOTSET_BATCH", "60")))
BACKOFF_START_S = 30.0
BACKOFF_MAX_S = 300.0

# 运行状态（供 GET /tdx/quote-feed/status 的 hot_set 段读取）
hot_set_feed_status: dict = {
    "enabled": False,
    "universe": 0,
    "cursor": 0,
    "written": 0,
    "errors": 0,
    "rate_limited": False,
    "backoff_s": 0.0,
    "last_symbol": None,
    "last_cycle_s": None,
    "last_feed_at": None,
    "last_error": None,
    "bridge_ok": None,
    "l05": None,
    "latency": None,
}

# ── L0.5 归档（T-P6-04 原料）：桥源席写标准键的成功快照同步喂归档器 ──────────────
# 背景：归档器只内嵌在订阅写侧，而订阅盘中零数据帧 → L0.5 长期空转、回放验收无原料；
# 桥源席成为生产唯一供数席后，必须同源归档（行契约与订阅侧完全一致，读取工具零改动）。
L05_TAG = "bridge"  # 文件名分片 tag（与订阅侧 s{i} 并列，防同日多写侧同毫秒撞名）

_L05_PRICE_MAP = {
    "price": "Now",
    "pre_close": "PreClose",
    "open": "Open",
    "high": "High",
    "low": "Low",
    "volume": "Volume",
    "amount": "Amount",
}
# 五档 20 字段（与 l05_store._FLOAT_FIELDS 契约一致，名字相同直接透传）
_L05_BOOK_FIELDS = tuple(
    [f"bid{i}" for i in range(1, 6)]
    + [f"bid_vol{i}" for i in range(1, 6)]
    + [f"ask{i}" for i in range(1, 6)]
    + [f"ask_vol{i}" for i in range(1, 6)]
)


def snapshot_l05_record(suffix: str, snap: dict) -> dict:
    """标准键快照 → L0.5 归档行（symbol=后缀式、ts=epoch 秒，与订阅侧写侧同契约）。

    桥的 ``get_market_snapshot`` 不提供涨跌停/封单字段 → 对应列如实 None（不假填）。
    """
    ts = int(snap["timestamp"])
    record: dict[str, Any] = {
        "symbol": suffix,
        "ts": ts,
        "refresh_time": datetime.fromtimestamp(ts, tz=TZ).strftime("%H%M%S"),
        "source": "tdx_bridge",
        "limit_up": None,
        "limit_down": None,
        "seal_amount": None,
    }
    for dst, src in _L05_PRICE_MAP.items():
        record[dst] = snap.get(src)
    for field in _L05_BOOK_FIELDS:
        record[field] = snap.get(field)
    return record


_archiver: Any | None = None


def _ensure_archiver() -> Any:
    """L0.5 归档器懒建单例（env 与订阅侧同源：QM_L05_DIR/FLUSH_ROWS/FLUSH_S/KEEP_DAYS）。"""
    global _archiver
    if _archiver is None:
        from backend.shared.l05_store import DEFAULT_BASE_DIR, SnapshotArchiver

        _archiver = SnapshotArchiver(
            base_dir=os.getenv("QM_L05_DIR") or DEFAULT_BASE_DIR,
            flush_rows=int(os.getenv("QM_L05_FLUSH_ROWS", "50000")),
            flush_seconds=float(os.getenv("QM_L05_FLUSH_S", "30")),
            keep_days=int(os.getenv("QM_L05_KEEP_DAYS", "90")),
            tag=L05_TAG,
        )
        logger.info("[TdxHotSet] l05 归档开启 dir=%s tag=%s", _archiver.base_dir, L05_TAG)
    return _archiver


def _refresh_l05_status() -> None:
    if _archiver is not None:
        hot_set_feed_status["l05"] = {
            "base_dir": _archiver.base_dir,
            "pending_rows": _archiver.pending_rows,
            **_archiver.counters,
        }


def _flush_archiver_if_pending() -> None:
    if _archiver is not None and _archiver.pending_rows:
        _archiver.flush()
        _refresh_l05_status()


# ── 时延打点（T-P6-05）：桥调用+写入耗时（stage=market_snapshot_bridge）───────────
# 口径如实标注：桥的 get_market_snapshot 载荷无 tick 时间戳（3s 服务端缓存也不透传），
# 无法测「行情源→本机」时延；本档记录**取数+写键**的处理耗时（P95 参考线：调用实测
# p50≈11ms + Redis 写 <5ms）。
BRIDGE_LATENCY_STAGE = "market_snapshot_bridge"
_latency: Any | None = None


def _ensure_latency() -> Any:
    global _latency
    if _latency is None:
        from backend.shared.latency_metrics import LatencyRecorder

        _latency = LatencyRecorder(BRIDGE_LATENCY_STAGE)
        logger.info("[TdxHotSet] 时延打点开启 stage=%s", BRIDGE_LATENCY_STAGE)
    return _latency


def _latency_observe(elapsed_ms: float) -> None:
    """记录一次处理耗时（打点关闭时静默 no-op；失败不阻断实时链）。"""
    if str(os.getenv("QM_LATENCY_ENABLED", "true")).strip().lower() in {"0", "false", "no", "off"}:
        return
    try:
        rec = _ensure_latency()
        rec.observe(float(elapsed_ms))
        rec.maybe_flush()
    except Exception:  # noqa: BLE001 - 打点 best-effort
        pass


def load_hot_set_symbols() -> list[str]:
    """热集（本地 Redis，Qlib 后缀式）→ 排序列表；读失败返回空（下轮重试）。"""
    try:
        from backend.shared.hot_set_store import hot_set_key, make_hot_set_client

        client = make_hot_set_client()
        try:
            return sorted(client.smembers(hot_set_key()) or [])
        finally:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
    except Exception as exc:  # noqa: BLE001
        hot_set_feed_status["last_error"] = f"热集读取失败: {exc}"
        return []


def map_snapshot_with_book(result: dict) -> dict | None:
    """契约字段（复用持仓馈送映射）+ **五档扩展**（L2 桥 Buyp/Buyv/Sellp/Sellv，单位=手）。

    与 collector 写侧契约同构（bid1-5/ask1-5 价量 20 字段），F2 快照内核可直接消费。
    """
    snap = map_snapshot(result)
    if snap is None:
        return None
    for side, price_key, vol_key in (("bid", "Buyp", "Buyv"), ("ask", "Sellp", "Sellv")):
        prices = result.get(price_key) or []
        vols = result.get(vol_key) or []
        for i in range(5):
            try:
                price = float(prices[i]) if i < len(prices) else 0.0
                vol = float(vols[i]) if i < len(vols) else 0.0
            except (TypeError, ValueError):
                price, vol = 0.0, 0.0
            snap[f"{side}{i + 1}"] = price
            snap[f"{side}_vol{i + 1}"] = int(vol)
    return snap


async def run_tdx_hot_set_feed_task() -> None:
    """常驻轮转：热集逐只取快照 → 标准键；预算内节奏 + 限流退避；盘外低频探测。"""
    logger.info(
        "[TdxHotSet] 热集行情轮询启动: bridge=%s pacing=%.2fs batch=%d",
        tdx_pusher.bridge_url, PACING_S, BATCH_PER_LOOP,
    )
    hot_set_feed_status["enabled"] = True
    cursor = 0
    backoff = 0.0
    while True:
        try:
            # 调度心跳（T-P1-06）：每次循环写入（含盘外低频探测），体检 C07 依此判活
            try:
                from backend.shared.scheduler_registry import heartbeat as _sched_heartbeat

                _sched_heartbeat("tdx_hot_set_feed")
            except Exception:  # noqa: BLE001 - best-effort
                pass
            if not is_trading_time(_now_sh()):
                _flush_archiver_if_pending()  # 收市终刷（缓冲不跨场次滞留）
                await asyncio.sleep(OFF_HOURS_SLEEP)
                continue
            symbols = load_hot_set_symbols()
            hot_set_feed_status["universe"] = len(symbols)
            if not symbols:
                await asyncio.sleep(30.0)
                continue

            cycle_t0 = time.monotonic()
            rate_limited = False
            for _ in range(BATCH_PER_LOOP):
                symbol = symbols[cursor % len(symbols)]
                cursor += 1
                hot_set_feed_status["cursor"] = cursor
                # 桥调用用后缀式（600036.SH）；标准键写入用**前缀式**（SH600036，
                # 与持仓馈送 _write_snapshot 的入参口径一致——2026-09-17 实测键形状错）
                suffix = StockCodeUtil.to_suffix(symbol) or symbol
                prefix = StockCodeUtil.to_prefix(symbol) or symbol
                call_t0 = time.monotonic()
                try:
                    result = await tdx_pusher.tdx_call(
                        "get_market_snapshot", {"stock_code": suffix}
                    )
                except Exception as exc:  # noqa: BLE001
                    msg = str(exc)
                    if "RATE_LIMITED" in msg:
                        rate_limited = True
                        break
                    hot_set_feed_status["errors"] += 1
                    hot_set_feed_status["last_error"] = f"{symbol}: {msg[:120]}"
                    continue
                snap = map_snapshot_with_book(result if isinstance(result, dict) else {})
                if snap is None:
                    continue
                if await _write_snapshot(prefix, snap):
                    hot_set_feed_status["written"] += 1
                    hot_set_feed_status["last_symbol"] = symbol
                    hot_set_feed_status["last_feed_at"] = _now_sh().isoformat(timespec="seconds")
                    # L0.5 同源归档（写失败只计数不抛出，不阻断实时链）
                    _ensure_archiver().append(snapshot_l05_record(suffix, snap))
                    _latency_observe((time.monotonic() - call_t0) * 1000.0)
                else:
                    hot_set_feed_status["errors"] += 1
                await asyncio.sleep(PACING_S)

            hot_set_feed_status["last_cycle_s"] = round(time.monotonic() - cycle_t0, 2)
            _refresh_l05_status()
            if _latency is not None:
                hot_set_feed_status["latency"] = {
                    "stage": BRIDGE_LATENCY_STAGE,
                    **_latency.counters,
                }
            if rate_limited:
                backoff = BACKOFF_START_S if backoff <= 0 else min(BACKOFF_MAX_S, backoff * 2)
                hot_set_feed_status["rate_limited"] = True
                hot_set_feed_status["backoff_s"] = backoff
                hot_set_feed_status["bridge_ok"] = False
                logger.warning("[TdxHotSet] 桥限流，退避 %.0fs", backoff)
                await asyncio.sleep(backoff)
                continue
            backoff = 0.0
            hot_set_feed_status["rate_limited"] = False
            hot_set_feed_status["backoff_s"] = 0.0
            hot_set_feed_status["bridge_ok"] = True
        except asyncio.CancelledError:
            _flush_archiver_if_pending()  # 停机终刷（与订阅侧归档器同纪律）
            raise
        except Exception as exc:  # noqa: BLE001 - 循环永续
            hot_set_feed_status["last_error"] = str(exc)[:200]
            logger.warning("[TdxHotSet] 循环异常: %s", exc)
            await asyncio.sleep(10.0)
