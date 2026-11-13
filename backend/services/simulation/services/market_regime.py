"""模拟盘市场状态辅助：沪深300指数急跌过滤。

QuantDB 的 `index_daily` 里有指数日线，直接读 `000300.SH` 收盘价，
不依赖本地全市场日线（本地日线不含指数）。
"""

from __future__ import annotations

import logging
import math
import threading
from datetime import date, timedelta

from backend.services.simulation.services.local_market_data import LocalMarketData

logger = logging.getLogger(__name__)

_INDEX_SYMBOL = "000300.SH"
_CACHE: dict[tuple[int, date], list[float]] = {}
_LOCK = threading.Lock()


def _index_closes(
    market_data: LocalMarketData,
    end_date: date | None,
) -> list[float]:
    """取最近 11 个交易日的沪深300收盘价（带进程内缓存）。"""
    end = end_date or date.today()
    key = (id(market_data), end)
    with _LOCK:
        cached = _CACHE.get(key)
    if cached is not None:
        return cached

    hub = getattr(market_data, "_hub", None)
    fetch = getattr(hub, "fetch_index_kline", None)
    if fetch is None:
        return []

    try:
        frame = fetch(_INDEX_SYMBOL, end - timedelta(days=45), end)
    except Exception:  # noqa: BLE001
        logger.exception("csi300_crash_active: 读取指数日线失败")
        return []
    if frame is None or frame.empty or "close" not in frame.columns:
        return []
    closes = [
        float(value)
        for value in frame["close"].dropna().tolist()
        if value is not None and float(value) > 0
    ][-11:]
    with _LOCK:
        _CACHE[key] = closes
    return closes


def csi300_crash_active(
    market_data: LocalMarketData,
    end_date: date | None = None,
    *,
    ret10_threshold: float = -0.06,
    ret5_threshold: float = -0.04,
) -> bool:
    """返回是否触发急跌过滤（沪深300 10 日 <= 阈值 或 5 日 <= 阈值）。"""
    try:
        closes = _index_closes(market_data, end_date)
        if len(closes) < 11:
            return False
        ret5 = closes[-1] / closes[-6] - 1.0
        ret10 = closes[-1] / closes[-11] - 1.0
        triggered = ret10 <= ret10_threshold or ret5 <= ret5_threshold
        if triggered:
            logger.info(
                "csi300_crash_active: ret10=%.2f%% ret5=%.2f%% threshold=(%.2f%%, %.2f%%)",
                ret10 * 100,
                ret5 * 100,
                ret10_threshold * 100,
                ret5_threshold * 100,
            )
        return triggered
    except Exception:  # noqa: BLE001
        logger.exception("csi300_crash_active: 计算急跌信号失败，按未触发处理")
        return False
