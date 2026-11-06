"""Bounded QuantDB price input for private factor materialization.

``adjust="qfq"`` (default) reads forward-adjusted daily bars, matching the
RD-Agent research panel. ``adjust="hfq"`` reads backward-adjusted bars and is
the only track used to append dates after a frozen statistical panel.
Volume and amount come from the selected adjusted partition. ``$factor`` is
adjusted close divided by the unadjusted close.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

_ADJUST_PATHS = {
    "qfq": "1_kline_data/daily_forward",
    "hfq": "1_kline_data/daily_backward",
}


def load_factor_price_panel(
    symbols,
    start: str,
    end: str,
    *,
    warmup_bars: int = 0,
    hub=None,
    adjust: str = "qfq",
):
    import numpy as np
    import pandas as pd

    from backend.shared.stock_utils import StockCodeUtil

    if adjust not in _ADJUST_PATHS:
        raise ValueError("不支持的复权口径")
    if not symbols or not 0 <= warmup_bars <= 1000:
        raise ValueError("Factor price input requires a bounded universe and warmup")
    if hub is None:
        from backend.services.engine.data_platform.quantdb_hub import QuantDBDataHub
        from backend.services.engine.rd_agent.rd_loop_wrapper import RDLoopWrapper

        root = RDLoopWrapper._resolve_quantdb_dir()
        if not root:
            raise ValueError("QuantDB price input is unavailable")
        hub = QuantDBDataHub(root)
    start_date, end_date = date.fromisoformat(start), date.fromisoformat(end)
    if start_date > end_date:
        raise ValueError("Factor price input has an invalid date range")
    calendar_frame = hub.fetch_calendar()
    if calendar_frame is None or calendar_frame.empty:
        raise ValueError("QuantDB trading calendar is unavailable")
    date_column = next(
        (
            name
            for name in ("TradingDate", "trade_date", "date", "cal_date")
            if name in calendar_frame
        ),
        None,
    )
    if not date_column:
        raise ValueError("QuantDB trading calendar has no date column")
    if "IsTradingDay" in calendar_frame:
        trading_flag = pd.to_numeric(calendar_frame["IsTradingDay"], errors="coerce")
        if trading_flag.isna().any() or not trading_flag.isin([0, 1]).all():
            raise ValueError("QuantDB trading calendar has invalid trading flags")
        calendar_frame = calendar_frame.loc[trading_flag == 1]
    raw_dates = (
        calendar_frame[date_column].astype(str).str.replace(r"\.0$", "", regex=True)
    )
    parsed_dates = pd.to_datetime(raw_dates, format="mixed", errors="coerce")
    compact = raw_dates.str.fullmatch(r"\d{8}")
    parsed_dates.loc[compact] = pd.to_datetime(
        raw_dates.loc[compact], format="%Y%m%d", errors="coerce"
    )
    if parsed_dates.isna().any():
        raise ValueError("QuantDB trading calendar has invalid dates")
    calendar = pd.DatetimeIndex(parsed_dates.unique()).sort_values()
    position = calendar.searchsorted(pd.Timestamp(start_date), side="left")
    if (
        len(calendar) == 0
        or position >= len(calendar)
        or position < warmup_bars
        or pd.Timestamp(start_date) < calendar[0]
        or pd.Timestamp(end_date) > calendar[-1]
    ):
        raise ValueError("QuantDB price input lacks the requested range or warmup")
    normalized = sorted({StockCodeUtil.to_suffix(str(value)) for value in symbols})
    if not normalized or len(normalized) > 20000 or any(not s for s in normalized):
        raise ValueError("Invalid factor universe")
    selected_calendar = calendar[
        position - warmup_bars : calendar.searchsorted(
            pd.Timestamp(end_date), side="right"
        )
    ]
    selected_dates = selected_calendar.strftime("%Y%m%d").tolist()
    adjusted_path = _ADJUST_PATHS[adjust]
    price_root = Path(hub.data_dir) / adjusted_path
    existing = {
        p.name[3:]
        for p in price_root.glob("dt=*")
        if len(p.name) == 11 and p.name[3:].isdigit()
    }
    missing = set(selected_dates) - existing
    if missing:
        raise ValueError(
            f"QuantDB price partitions are missing: {', '.join(sorted(missing)[:5])}"
        )
    placeholders = ", ".join("?" for _ in normalized)

    def read_prices(relative_path):
        return hub._normalize_kline(
            hub._read_partitioned(
                relative_path,
                selected_dates,
                where_sql=f"symbol IN ({placeholders})",
                bind=normalized,
                order_by="symbol, dt",
            )
        )

    adjusted = read_prices(adjusted_path)
    raw = read_prices("1_kline_data/daily_unadjusted")
    required = {
        "symbol",
        "trade_date",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "amount",
    }
    if adjusted is None or adjusted.empty or not required.issubset(adjusted.columns):
        raise ValueError("QuantDB adjusted price columns are unavailable")
    if (
        raw is None
        or raw.empty
        or not {"symbol", "trade_date", "close"}.issubset(raw.columns)
    ):
        raise ValueError("QuantDB unadjusted close is unavailable")
    keys = ["symbol", "trade_date"]
    if adjusted.duplicated(keys).any() or raw.duplicated(keys).any():
        raise ValueError("QuantDB price input has duplicate symbol/date keys")
    frame = adjusted.merge(
        raw[keys + ["close"]].rename(columns={"close": "raw_close"}),
        on=keys,
        how="left",
        validate="one_to_one",
    )
    close = pd.to_numeric(frame["close"], errors="coerce")
    raw_close = pd.to_numeric(frame["raw_close"], errors="coerce")
    if ((close <= 0) | (raw_close <= 0) | close.isna() | raw_close.isna()).any():
        raise ValueError("QuantDB adjustment factor cannot be verified")
    frame["factor"] = close / raw_close
    frame["instrument"] = frame["symbol"].map(StockCodeUtil.to_qlib)
    frame["datetime"] = pd.to_datetime(frame["trade_date"], errors="raise")
    observed_dates = set(frame["datetime"].dt.normalize())
    expected_dates = set(selected_calendar)
    if not expected_dates.issubset(observed_dates):
        raise ValueError("QuantDB price input is missing trading-date observations")
    frame = frame.set_index(["instrument", "datetime"]).sort_index()
    columns = ["open", "high", "low", "close", "volume", "amount", "factor"]
    values = (
        frame[columns]
        .apply(pd.to_numeric, errors="coerce")
        .replace([np.inf, -np.inf], np.nan)
    )
    if values.empty or values.index.has_duplicates:
        raise ValueError("QuantDB price input is empty or ambiguous")
    if values[["open", "high", "low", "close", "volume", "amount"]].isna().any().any():
        raise ValueError("QuantDB price input contains missing price or volume values")
    result = values.rename(columns={name: "$" + name for name in columns})
    result.attrs["trading_dates"] = [
        value.strftime("%Y-%m-%d")
        for value in selected_calendar
        if value >= pd.Timestamp(start_date)
    ]
    return result
