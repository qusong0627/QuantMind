"""前复权（qfq）标签取价面板：训练/回测标签的唯一取价口径。

为什么需要本模块（2026-11-19 审计）：
    A 股 ``daily_backward`` 在 2016 年前是**不复权**（治理方案已冻结
    「禁止跨 2016 使用 backward」），而默认训练源 ``l1_factors`` 的 OHLCV
    直通列逐值复制 ``daily_backward``。训练标签若从因子源自带的 close 构造，
    2010-2015 会把分红/送转当成亏损：实测 5 日标签 4.41% 与 qfq 口径差 >1%、
    1.18% 差 >10%，且 2016 前后标签定义不一致。

本模块只做两件事：
    1. 从 ``1_kline_data/daily_forward``（前复权，全历史同一 vintage）读
       ``open``/``close``，按 (symbol, trade_date) 供标签构造；
    2. 该表**只含当日有 bar 的行**——调用方据此剔除停牌僵尸行
       （停牌日无 bar，绝不按最后收盘填充）。

口径纪律：
    - 只读 daily_forward，不回退 daily_backward / daily_unadjusted；
    - 不写任何数据；
    - 符号统一转前缀式（``SH600000``），与因子源保持一致。
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from backend.shared.stock_utils import StockCodeUtil

PANEL_COLUMNS = ("symbol", "trade_date", "_qfq_open", "_qfq_close")


def _empty_panel() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "symbol": pd.Series(dtype="object"),
            "trade_date": pd.Series(dtype="datetime64[ns]"),
            "_qfq_open": pd.Series(dtype="float64"),
            "_qfq_close": pd.Series(dtype="float64"),
        }
    )


def _to_prefix(series: pd.Series) -> pd.Series:
    """后缀式代码 → 前缀式（去重后映射，避免逐行 Python 调用）。"""
    uniq = pd.unique(series.astype(str))
    mapping = {s: StockCodeUtil.to_prefix(s) for s in uniq}
    return series.astype(str).map(mapping)


def load_qfq_label_panel(
    quantdb_dir: str | Path | None,
    start,
    end,
    *,
    market: str = "CN",
) -> pd.DataFrame:
    """读取 ``daily_forward`` 的 qfq open/close（仅当日有 bar 的行）。

    Args:
        quantdb_dir: QuantDB 数据根（含 ``1_kline_data/daily_forward``）。
        start/end: 日期边界（date/str/Timestamp 均可）。
        market: 仅 CN 生效；其他市场返回空表（本模块不改变其口径）。

    Returns:
        columns = symbol(前缀式), trade_date(datetime64), _qfq_open, _qfq_close。
        目录不存在/区间为空时返回空表（调用方须判断 empty）。
    """
    if str(market or "CN").upper() != "CN" or quantdb_dir is None:
        return _empty_panel()
    root = Path(quantdb_dir) / "1_kline_data" / "daily_forward"
    if not root.is_dir():
        return _empty_panel()

    start_s = pd.Timestamp(start).strftime("%Y-%m-%d")
    end_s = pd.Timestamp(end).strftime("%Y-%m-%d")
    glob = str(root / "dt=*" / "data.parquet")

    import duckdb

    con = duckdb.connect(config={"memory_limit": "2GB", "threads": "2"})
    try:
        frame = con.execute(
            "SELECT symbol, CAST(time AS DATE) AS trade_date, open, close "
            f"FROM read_parquet('{glob}') "
            "WHERE CAST(time AS DATE) BETWEEN CAST(? AS DATE) AND CAST(? AS DATE)",
            [start_s, end_s],
        ).fetchdf()
    finally:
        con.close()

    if frame.empty:
        return _empty_panel()
    frame = frame.rename(columns={"open": "_qfq_open", "close": "_qfq_close"})
    frame["symbol"] = _to_prefix(frame["symbol"])
    frame["trade_date"] = pd.to_datetime(frame["trade_date"])
    frame["_qfq_open"] = pd.to_numeric(frame["_qfq_open"], errors="coerce")
    frame["_qfq_close"] = pd.to_numeric(frame["_qfq_close"], errors="coerce")
    return frame.loc[:, list(PANEL_COLUMNS)]


def forward_return_labels(
    panel: pd.DataFrame,
    *,
    horizon: int,
    signal_lag_days: int = 1,
) -> pd.DataFrame:
    """由 qfq 面板构造 ``close(T+lag+H)/close(T+lag)-1`` 标签。

    与训练侧同口径（``_EXECUTION_LAG_DAYS=1``，T+1 执行）；返回
    ``[symbol, trade_date, label]``，序列末尾自然为 NaN。
    """
    if panel.empty:
        return pd.DataFrame(
            {
                "symbol": pd.Series(dtype="object"),
                "trade_date": pd.Series(dtype="datetime64[ns]"),
                "label": pd.Series(dtype="float64"),
            }
        )
    lag = max(0, int(signal_lag_days))
    h = max(1, int(horizon))
    data = panel[["symbol", "trade_date", "_qfq_close"]].copy()
    data["_qfq_close"] = pd.to_numeric(data["_qfq_close"], errors="coerce")
    data = data[data["_qfq_close"] > 0].sort_values(["symbol", "trade_date"])
    execution_close = data.groupby("symbol")["_qfq_close"].shift(-lag)
    future_close = data.groupby("symbol")["_qfq_close"].shift(-(lag + h))
    data["label"] = future_close / execution_close - 1.0
    return data[["symbol", "trade_date", "label"]]
