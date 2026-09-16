"""日线装载（T-P4-04）：买入前 K 线过滤的数据侧（IO 与纯函数分离）。

数据源：QuantDB parquet（`qdb_daily_unadjusted` **不复权**——前复权价在除权日
跳变会污染跳空/乖离判定）；窗口取判断日前若干自然日（覆盖 ≥21 交易日）。
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

logger = logging.getLogger(__name__)

_COLUMNS = ["open", "high", "low", "close", "volume"]


def load_recent_daily_bars(
    symbols: list[str],
    *,
    end_date: str | None = None,
    calendar_days: int = 45,
) -> dict[str, list[dict[str, Any]]] | None:
    """批量装载最近日线（不复权）。

    返回 {symbol(后缀式): [按日期升序的 bar dict]}；**基础设施不可用返回 None**
    （调用方整步跳过并如实标注——fail-loud，不静默当"通过"）。
    """
    symbols = [str(s).strip() for s in (symbols or []) if str(s).strip()]
    if not symbols:
        return {}
    try:
        from backend.shared.stock_utils import StockCodeUtil
        from backend.services.engine.data_platform.quantdb_hub import (
            QuantDBDataHub,
        )

        hub = QuantDBDataHub.get_instance()
        if hub is None or not hub.available:
            logger.warning("[BuyFilters] QuantDB hub 不可用，跳过 K 线过滤")
            return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("[BuyFilters] QuantDB hub 初始化失败: %s", exc)
        return None

    end = date.fromisoformat(end_date) if end_date else date.today()
    start = end - timedelta(days=max(10, int(calendar_days)))
    start_dt = int(start.strftime("%Y%m%d"))
    end_dt = int(end.strftime("%Y%m%d"))

    out: dict[str, list[dict[str, Any]]] = {}
    for symbol in symbols:
        suffix = StockCodeUtil.to_suffix(symbol) or symbol
        try:
            df = hub.fetch_series(
                "qdb_daily_unadjusted",
                suffix,
                start_dt,
                end_dt,
                columns=_COLUMNS,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[BuyFilters] %s 日线读取失败: %s", symbol, exc)
            df = None
        if df is None or len(df) == 0:
            out[suffix] = []
            continue
        df = df.sort_values("dt")
        out[suffix] = [
            {
                "date": int(row["dt"]),
                "open": row.get("open"),
                "high": row.get("high"),
                "low": row.get("low"),
                "close": row.get("close"),
                "volume": row.get("volume"),
            }
            for _, row in df.iterrows()
        ]
    return out
