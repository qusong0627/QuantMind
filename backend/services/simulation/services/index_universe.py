"""指数成分股查询（模拟盘动态股票池）。

读取 Qlib `instruments/<market>.txt`（`<symbol>\\t<start>\\t<end>`），
按交易日判断某只股票是否属于指数成分。带 mtime 缓存，避免每轮调仓重复读盘。

只依赖 `backend.shared.qlib_paths` 与 `StockCodeUtil`，不导入 qlib，
避免模拟盘进程被 qlib 初始化拖慢。
"""

from __future__ import annotations

import logging
import threading
from datetime import date
from pathlib import Path

from backend.shared.qlib_paths import resolve_qlib_provider_uri
from backend.shared.stock_utils import StockCodeUtil

logger = logging.getLogger(__name__)

# (market, provider_uri) -> (file_mtime, {symbol: [(start, end), ...]})
_CACHE: dict[tuple[str, str], tuple[float, dict[str, list[tuple[date, date]]]]] = {}
_LOCK = threading.Lock()


def _to_date(value: object) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def load_index_intervals(
    market: str,
    *,
    provider_uri: str | None = None,
    refresh: bool = False,
) -> dict[str, list[tuple[date, date]]]:
    """读取指数成分区间；文件不存在或解析失败时返回空字典（调用方回退全市场）。"""
    market = str(market or "").strip()
    if not market:
        return {}
    provider = str(provider_uri or resolve_qlib_provider_uri())
    path = Path(provider) / "instruments" / f"{market}.txt"
    if not path.is_file():
        logger.warning("index_universe: 成分文件不存在 market=%s path=%s", market, path)
        return {}

    try:
        mtime = path.stat().st_mtime
    except OSError as exc:
        logger.warning("index_universe: 读取成分文件失败 market=%s error=%s", market, exc)
        return {}

    key = (market, provider)
    with _LOCK:
        cached = _CACHE.get(key)
        if cached and not refresh and cached[0] == mtime:
            return cached[1]

    intervals: dict[str, list[tuple[date, date]]] = {}
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                parts = line.strip().split()
                if len(parts) < 3:
                    continue
                symbol = StockCodeUtil.normalize(parts[0])
                start = _to_date(parts[1])
                end = _to_date(parts[2])
                if not symbol or start is None or end is None:
                    continue
                intervals.setdefault(symbol, []).append((start, end))
    except OSError as exc:
        logger.warning("index_universe: 解析成分文件失败 market=%s error=%s", market, exc)
        return {}

    with _LOCK:
        _CACHE[key] = (mtime, intervals)
    return intervals


def index_members_on(
    market: str,
    day: object,
    *,
    provider_uri: str | None = None,
) -> set[str]:
    """返回指定交易日属于该指数的股票集合（统一为 `600036.SH` 后缀格式）。"""
    trade_date = _to_date(day)
    if trade_date is None:
        return set()
    intervals = load_index_intervals(market, provider_uri=provider_uri)
    return {
        symbol
        for symbol, ranges in intervals.items()
        if any(start <= trade_date <= end for start, end in ranges)
    }


def clear_cache() -> None:
    """测试/热更新用：清空成分缓存。"""
    with _LOCK:
        _CACHE.clear()
