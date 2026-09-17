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
→ bid1-5/ask1-5，单位=手与 TDX 帧一致，F2 内核按 ×100 归一为股）。
"""

from __future__ import annotations

import asyncio
import logging
import os
import time

from backend.shared.stock_utils import StockCodeUtil
from backend.services.live_trading.services.tdx_push_service import tdx_pusher
from backend.services.live_trading.services.tdx_quote_feed import (
    OFF_HOURS_SLEEP,
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
}


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
            if not is_trading_time(_now_sh()):
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
                else:
                    hot_set_feed_status["errors"] += 1
                await asyncio.sleep(PACING_S)

            hot_set_feed_status["last_cycle_s"] = round(time.monotonic() - cycle_t0, 2)
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
            raise
        except Exception as exc:  # noqa: BLE001 - 循环永续
            hot_set_feed_status["last_error"] = str(exc)[:200]
            logger.warning("[TdxHotSet] 循环异常: %s", exc)
            await asyncio.sleep(10.0)
