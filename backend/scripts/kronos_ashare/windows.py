"""Kronos A股零样本测试：窗口构造纯函数。

口径约定（与 QuantMind 研究侧一致）：
- 信号日 T 收盘出分，T+entry_lag 收盘成交（默认 T+1）；
- 模型预测窗口 = T+1 .. T+pred_len 的交易日；
- 模型 IC 用 ``close[T+pred_len] / close[T] - 1``（模型目标）；
- 可执行收益用 ``close[T+pred_len] / close[T+entry_lag] - 1``（扣掉入场滞后）。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

OHLCVA = ("open", "high", "low", "close", "volume", "amount")


@dataclass(frozen=True)
class Window:
    symbol: str
    signal_date: pd.Timestamp
    hist_dates: pd.DatetimeIndex
    future_dates: pd.DatetimeIndex
    x_df: pd.DataFrame
    last_close: float
    entry_date: pd.Timestamp
    entry_price: float
    entry_change: float
    fwd_ret_model: float
    fwd_ret_exec: float


def select_signal_dates(
    calendar: pd.DatetimeIndex,
    start: str | pd.Timestamp,
    end: str | pd.Timestamp,
    every: int = 20,
    max_dates: int | None = None,
) -> pd.DatetimeIndex:
    """在 [start, end] 内每隔 ``every`` 个交易日取一个信号日。"""
    if every < 1:
        raise ValueError("every must be >= 1")
    mask = (calendar >= pd.Timestamp(start)) & (calendar <= pd.Timestamp(end))
    positions = np.flatnonzero(mask)
    if len(positions) == 0:
        return pd.DatetimeIndex([])
    chosen = positions[::every]
    if max_dates is not None and max_dates > 0 and len(chosen) > max_dates:
        idx = np.linspace(0, len(chosen) - 1, max_dates).round().astype(int)
        chosen = chosen[np.unique(idx)]
    return pd.DatetimeIndex(calendar[chosen])


def build_window(
    panel: pd.DataFrame,
    calendar: pd.DatetimeIndex,
    symbol: str,
    signal_date: pd.Timestamp,
    lookback: int,
    pred_len: int,
    entry_lag: int = 1,
) -> Window | None:
    """构造一个样本窗口；数据不足或关键价格缺失时返回 None。"""
    if lookback < 1 or pred_len < 1 or entry_lag < 0:
        raise ValueError("invalid window parameters")
    if entry_lag >= pred_len:
        raise ValueError("entry_lag must be < pred_len")

    pos = int(calendar.get_indexer([pd.Timestamp(signal_date)])[0])
    if pos < 0 or pos - lookback + 1 < 0 or pos + pred_len >= len(calendar):
        return None

    hist_dates = calendar[pos - lookback + 1 : pos + 1]
    future_dates = calendar[pos + 1 : pos + pred_len + 1]
    if len(hist_dates) != lookback or len(future_dates) != pred_len:
        return None

    missing = [c for c in OHLCVA if c not in panel.columns]
    if missing:
        raise ValueError(f"panel missing columns: {missing}")

    hist = panel.reindex(hist_dates)
    if not np.isfinite(hist[list(OHLCVA)].to_numpy(dtype=float)).all():
        return None

    entry_date = calendar[pos + entry_lag]
    entry_row = panel.reindex([entry_date])
    if entry_row.empty or not np.isfinite(float(entry_row["close"].iloc[0])):
        return None

    future = panel.reindex(future_dates)
    exit_close = float(future["close"].iloc[-1]) if len(future) else float("nan")
    last_close = float(hist["close"].iloc[-1])
    entry_price = float(entry_row["close"].iloc[0])
    if not (np.isfinite(exit_close) and last_close > 0 and entry_price > 0):
        return None

    entry_change = float("nan")
    if "change" in panel.columns:
        raw_change = entry_row["change"].iloc[0]
        if pd.notna(raw_change):
            entry_change = float(raw_change)

    x_df = hist[list(OHLCVA)].reset_index(drop=True)
    return Window(
        symbol=symbol,
        signal_date=pd.Timestamp(signal_date),
        hist_dates=hist_dates,
        future_dates=future_dates,
        x_df=x_df,
        last_close=last_close,
        entry_date=pd.Timestamp(entry_date),
        entry_price=entry_price,
        entry_change=entry_change,
        fwd_ret_model=exit_close / last_close - 1.0,
        fwd_ret_exec=exit_close / entry_price - 1.0,
    )


def score_prediction(pred_df: pd.DataFrame, last_close: float, mode: str = "mean") -> float:
    """把预测 K 线压成一个截面分数：预测期均价/末值相对最后收盘的涨幅。"""
    if "close" not in pred_df.columns or last_close <= 0:
        return float("nan")
    values = pd.to_numeric(pred_df["close"], errors="coerce").to_numpy(dtype=float)
    values = values[np.isfinite(values)]
    if len(values) == 0:
        return float("nan")
    if mode == "mean":
        pred = float(np.mean(values))
    elif mode == "end":
        pred = float(values[-1])
    elif mode == "max":
        pred = float(np.max(values))
    elif mode == "min":
        pred = float(np.min(values))
    else:
        raise ValueError(f"unknown score mode: {mode}")
    return pred / last_close - 1.0
