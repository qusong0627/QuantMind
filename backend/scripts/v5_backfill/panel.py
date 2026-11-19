"""V5 因子重建 · 数据面板（只读 QuantDB）。"""
from __future__ import annotations

import os
from pathlib import Path

import pandas as pd

QDB = Path(os.environ.get("QM_QUANTDB_ROOT", "/home/zbox/projects/quantmind/data/quantdb"))


def trading_dates(ds: str = "daily_forward", end: str | None = None) -> list[str]:
    base = QDB / "1_kline_data" / ds
    out = []
    for name in os.listdir(base):
        if not name.startswith("dt="):
            continue
        d = name[3:]
        if end and d > end:
            continue
        if (base / name / "data.parquet").exists():
            out.append(d)
    return sorted(out)


def load_bars(target: str, window: int = 300) -> pd.DataFrame:
    """目标日 + 前 window 个交易日的日线（symbol/time 升序）。"""
    dates = trading_dates(end=target)[-window:]
    frames = []
    for d in dates:
        f = QDB / "1_kline_data" / "daily_forward" / f"dt={d}" / "data.parquet"
        x = pd.read_parquet(f, columns=["symbol", "time", "open", "high", "low", "close",
                                        "volume", "amount"])
        x["dt"] = d
        frames.append(x)
    df = pd.concat(frames, ignore_index=True)
    return df.sort_values(["symbol", "time"], kind="stable")


def load_circ_daily(symbols, start: str, end: str) -> pd.DataFrame:
    """流通股本日序列（capital 表 PIT：报告期 <= 当日最近一期，ffill）。

    返回 [symbol, dt, circ]；缺失返回空表。数据不足的标的 circ=NaN。
    """
    cap_dir = QDB / "3_financial_data" / "capital"
    if not cap_dir.is_dir():
        return pd.DataFrame(columns=["symbol", "dt", "circ"])
    dates = [d for d in trading_dates(end=end) if d >= start]
    rows = []
    for sym in symbols:
        p = cap_dir / f"{sym}.parquet"
        if not p.exists():
            continue
        try:
            df = pd.read_parquet(p, columns=["m_timetag", "circulating_capital"])
        except Exception:  # noqa: BLE001
            continue
        tt = pd.to_datetime(df["m_timetag"].astype(str).str[:8], format="%Y%m%d", errors="coerce")
        df = df.assign(tt=tt).dropna(subset=["tt"]).sort_values("tt")
        if df.empty:
            continue
        # 按交易日 as-of 取最近一期
        idx = pd.to_datetime(dates, format="%Y%m%d")
        s = pd.Series(df["circulating_capital"].to_numpy(), index=df["tt"])
        s = s[~s.index.duplicated(keep="last")]
        vals = s.reindex(s.index.union(idx)).ffill().reindex(idx)
        rows.append(pd.DataFrame({"symbol": sym, "dt": dates, "circ": vals.to_numpy()}))
    if not rows:
        return pd.DataFrame(columns=["symbol", "dt", "circ"])
    return pd.concat(rows, ignore_index=True)


def load_index_return(target: str, window: int = 300, symbol: str = "000905.SH") -> pd.Series:
    dates = trading_dates("index_daily", end=target)[-window:]
    return load_index_return_dates(dates, symbol=symbol)


def load_index_return_dates(dates: list[str], symbol: str = "000905.SH") -> pd.Series:
    """给定交易日列表 → 指数日收益（index=dt 字符串）。"""
    frames = []
    for d in dates:
        f = QDB / "1_kline_data" / "index_daily" / f"dt={d}" / "data.parquet"
        if not f.exists():
            continue
        x = pd.read_parquet(f, columns=["symbol", "close"])
        x = x[x["symbol"] == symbol]
        if x.empty:
            continue
        x = x.assign(dt=d)
        frames.append(x)
    if not frames:
        return pd.Series(dtype=float)
    df = pd.concat(frames, ignore_index=True).sort_values("dt")
    s = df.set_index("dt")["close"].astype(float)
    return s.pct_change(fill_method=None)
