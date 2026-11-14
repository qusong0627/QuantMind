#!/usr/bin/env python3
"""市场阶段仪表盘：长期 / 中期 / 短期 / 当日 四层方向感。

每个指标都只在**已验证**的口径上使用；没验证的标 ``待验证``，不混进结论。

层级与用途：
1. 长期（季度）：指数 vs MA250、250 日动量、距 250 日高 —— 定基础暴露
2. 中期（2-6 周）：pct_above_ma20/60、ADL 20 日斜率、52 周新高新低、5 日广度均值
3. 短期（1-5 日）：收盘涨家占比、涨停净额、指数 5 日动量
4. 当日（盘中）：10:10 广度、MCL 5 日变化（来自 intraday_market_panel）

用法::

    python -m backend.scripts.market_stage_dashboard
"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

ROOT = Path(os.environ.get("QM_QUANTDB_DATA_DIR", "data/quantdb"))
KLINE = ROOT / "1_kline_data" / "daily_backward"
IDX = ROOT / "1_kline_data" / "index_daily"
PANEL = ROOT / "5_technical_derived" / "market_breadth" / "trin_daily.parquet"
INTRADAY = (
    ROOT / "5_technical_derived" / "market_breadth" / "intraday_market_panel.parquet"
)
VALUATION = ROOT / "5_technical_derived" / "market_breadth" / "valuation_daily.parquet"

WASHOUT_5D = 25.0  # 5 日涨家占比均值 < 25% → 投降段
VAL_CHEAP = 0.8  # PB 滚动 3 年便宜分位 > 80% → 便宜
BREADTH_STRONG = 50.0  # pct_above_ma20 > 50% → 修复
CLOSE_OVERSOLD = 15.0
CLOSE_STRONG = 80.0


# --------------------------------------------------------------------------
# 数据
# --------------------------------------------------------------------------
def load_index(symbol: str, days: int = 320) -> pd.Series:
    vals = {}
    for tag in sorted(os.listdir(IDX))[-days:]:
        p = IDX / tag / "data.parquet"
        if not p.exists():
            continue
        t = pq.read_table(p, columns=["symbol", "close"]).to_pandas()
        r = t[t["symbol"] == symbol]
        if not r.empty:
            vals[pd.Timestamp(tag.split("=")[1])] = float(r["close"].iloc[0])
    return pd.Series(vals).sort_index()


def load_breadth_tail(days: int = 260) -> pd.DataFrame:
    """从原始日线重算最近 days 天的全市场广度（补面板未更新的尾部）。"""
    tags = sorted(os.listdir(KLINE))[-(days + 1) :]
    frames = {}
    volumes = {}
    for tag in tags:
        t = pq.read_table(
            KLINE / tag / "data.parquet", columns=["symbol", "close", "volume"]
        ).to_pandas()
        t = t.set_index("symbol")
        frames[pd.Timestamp(tag.split("=")[1])] = t["close"]
        volumes[pd.Timestamp(tag.split("=")[1])] = t["volume"]
    dates = sorted(frames)
    close = pd.DataFrame({d: frames[d] for d in dates}).T
    vol = pd.DataFrame({d: volumes[d] for d in dates}).T
    ret = close / close.shift(1) - 1.0
    n_up = (ret > 0).sum(axis=1)
    n_down = (ret < 0).sum(axis=1)
    n_total = ret.notna().sum(axis=1)
    above20 = (close > close.rolling(20, min_periods=20).mean()).sum(axis=1)
    above60 = (close > close.rolling(60, min_periods=60).mean()).sum(axis=1)
    hi = close.rolling(252, min_periods=252).max()
    lo = close.rolling(252, min_periods=252).min()
    nh = (close >= hi - 1e-9).sum(axis=1)
    nl = (close <= lo + 1e-9).sum(axis=1)
    base = hi.notna().sum(axis=1)
    out = pd.DataFrame(
        {
            "n_up": n_up,
            "n_down": n_down,
            "n_total": n_total,
            "pct_above_ma20": above20 / n_total * 100,
            "pct_above_ma60": above60 / n_total * 100,
            "nh_nl": (nh - nl) / base.replace(0, np.nan) * 100,
        }
    )
    out["up_ratio"] = out["n_up"] / (out["n_up"] + out["n_down"]) * 100
    vol_up = vol.where(ret > 0).sum(axis=1)
    vol_down = vol.where(ret < 0).sum(axis=1)
    out["trin_vol"] = (out["n_up"] / out["n_down"]) / (vol_up / vol_down)
    out["adl"] = (out["n_up"] - out["n_down"]).cumsum()
    out["adl_slope20"] = out["adl"].diff(20)
    net = (out["n_up"] - out["n_down"]).astype(float)
    out["mcclellan"] = (
        net.ewm(span=19, adjust=False, min_periods=19).mean()
        - net.ewm(span=39, adjust=False, min_periods=39).mean()
    )
    out["mcl_chg5"] = out["mcclellan"] - out["mcclellan"].shift(5)
    return out


def load_intraday_tail(n: int = 5) -> pd.DataFrame:
    if not INTRADAY.exists():
        return pd.DataFrame()
    d = pd.read_parquet(INTRADAY).sort_index()
    return d.tail(n)[
        ["up_share", "mcl_chg5", "amt_gap_pp", "trin_amt", "ret_rest_ew", "ret_next_ew"]
    ]


def load_valuation() -> pd.DataFrame:
    if not VALUATION.exists():
        return pd.DataFrame()
    v = pd.read_parquet(VALUATION).sort_index()
    v.index = pd.to_datetime(v.index)
    return v


# --------------------------------------------------------------------------
# 阶段判定
# --------------------------------------------------------------------------
def long_stage(idx: pd.Series) -> dict:
    c = float(idx.iloc[-1])
    ma250 = float(idx.rolling(250, min_periods=250).mean().iloc[-1])
    hi = float(idx.rolling(250, min_periods=250).max().iloc[-1])
    mom250 = c / float(idx.iloc[-251]) - 1.0 if len(idx) > 251 else np.nan
    if c > ma250 and mom250 > 0:
        stage = "多头趋势"
    elif c < ma250 and mom250 < 0:
        stage = "空头趋势"
    else:
        stage = "震荡/转折"
    return {
        "阶段": stage,
        "数据日期": f"{idx.index[-1]:%Y-%m-%d}",
        "收盘": round(c, 2),
        "MA250": round(ma250, 2),
        "距MA250_%": round((c / ma250 - 1) * 100, 2),
        "距250日高_%": round((c / hi - 1) * 100, 2),
        "250日动量_%": round(mom250 * 100, 2) if np.isfinite(mom250) else np.nan,
    }


def bottom_stage(breadth: pd.DataFrame, val: pd.DataFrame | None = None) -> dict:
    """底部信号：估值便宜（PB 滚动 3 年分位 > 80%）+ 5 日广度投降（<25%）。"""
    if val is None:
        val = load_valuation()
    if val.empty:
        return {"阶段": "无估值数据"}
    last = val.iloc[-1]
    ur5 = float(breadth["up_ratio"].tail(5).mean())
    cheap = float(last["bp_capw_pct"]) > VAL_CHEAP
    wash = ur5 < WASHOUT_5D
    if cheap and wash:
        stage = "底部信号（便宜+投降）"
    elif cheap:
        stage = "估值便宜但未投降（等）"
    else:
        stage = "估值不便宜（底部信号不成立）"
    return {
        "阶段": stage,
        "数据日期": f"{val.index[-1]:%Y-%m-%d}",
        "PB": round(float(last["bp_capw"]), 3),
        "PB便宜分位": round(float(last["bp_capw_pct"]), 3),
        "EP_%": round(float(last["ep_capw"]) * 100, 2),
        "中位PE": round(float(last["med_pe"]), 2),
        "5日均涨家_%": round(ur5, 2),
        "ERP": "待接 10Y 历史",
    }


def medium_stage(breadth: pd.DataFrame) -> dict:
    b = breadth.iloc[-1]
    ur5 = float(breadth["up_ratio"].tail(5).mean())
    if ur5 < WASHOUT_5D:
        stage = "超卖/投降段"
    elif (b["pct_above_ma20"] < 30 or b["pct_above_ma60"] < 20) and b[
        "adl_slope20"
    ] < 0:
        stage = "下跌中继"
    elif b["pct_above_ma20"] > BREADTH_STRONG and b["adl_slope20"] > 0:
        stage = "上行修复"
    else:
        stage = "震荡/待确认"
    return {
        "阶段": stage,
        "数据日期": f"{breadth.index[-1]:%Y-%m-%d}",
        "涨家占比_%": round(float(b["up_ratio"]), 2),
        "5日均涨家_%": round(ur5, 2),
        "站上MA20_%": round(float(b["pct_above_ma20"]), 2),
        "站上MA60_%": round(float(b["pct_above_ma60"]), 2),
        "ADL20斜率": int(b["adl_slope20"]),
        "52周新高新低_%": round(float(b["nh_nl"]), 2),
        "TRIN_量": round(float(b.get("trin_vol", np.nan)), 2)
        if np.isfinite(b.get("trin_vol", np.nan)) else "—",
        "量能投降": "是（20日偏多）"
        if np.isfinite(b.get("trin_vol", np.nan)) and b["trin_vol"] > 2 else "否",
    }


def short_stage(breadth: pd.DataFrame) -> dict:
    b = breadth.iloc[-1]
    ur = float(b["up_ratio"])
    if ur < CLOSE_OVERSOLD:
        stage = "超卖（反抽候选）"
    elif ur > CLOSE_STRONG:
        stage = "强势延续"
    else:
        stage = "中性"
    return {
        "阶段": stage,
        "数据日期": f"{breadth.index[-1]:%Y-%m-%d}",
        "收盘涨家_%": round(ur, 2),
        "5日累计ADL": int(breadth["adl"].diff(5).iloc[-1]),
    }


def intraday_stage(breadth: pd.DataFrame) -> dict:
    latest = breadth.index[-1]
    d = load_intraday_tail(1)
    if not d.empty and d.index[-1] == latest:
        r = d.iloc[-1]
        b, m = float(r["up_share"]), float(r["mcl_chg5"])
        src, date, gap = "l2_min1 全市场", f"{latest:%Y-%m-%d}", float(r["amt_gap_pp"])
    else:
        # 全市场分钟数据未更新：用 L0.5 热集补当日 10:10 广度，MCL 用日线全市场
        from backend.scripts.intraday_breadth_probe import hot_set_path

        hp = hot_set_path(f"{latest:%Y-%m-%d}")
        row = hp[hp["time"] == "10:10"] if not hp.empty else hp
        if row.empty:
            return {"阶段": "无数据", "数据日期": f"{latest:%Y-%m-%d}"}
        b = float(row["up_share"].iloc[0])
        m = float(breadth["mcl_chg5"].iloc[-1])
        src = "L0.5 热集（非全市场）"
        date = f"{latest:%Y-%m-%d}"
        gap = np.nan
    if b > 60 and m > 0:
        stage = "强延续（下午偏强）"
    elif b > 60 and m <= 0:
        stage = "弱延续（下午转弱）"
    elif b < 30 and m < 0:
        stage = "弱延续（下午继续弱）"
    elif b < 30 and m >= 0:
        stage = "止跌观察"
    else:
        stage = "中性"
    return {
        "阶段": stage,
        "数据日期": date,
        "口径": src,
        "10:10涨家_%": round(b, 2),
        "MCL5日变化": round(m, 1),
        "额价差_pp": round(gap, 2) if np.isfinite(gap) else "—",
    }


# --------------------------------------------------------------------------
# 输出
# --------------------------------------------------------------------------
def _table(rows: dict) -> str:
    lines = ["| 项 | 值 |", "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in rows.items()]
    return "\n".join(lines)


def main() -> int:
    idx = load_index("000001.SH")
    breadth = load_breadth_tail()
    print(f"# 市场阶段仪表盘（截至 {breadth.index[-1]:%Y-%m-%d}）\n")
    for title, rows in (
        ("长期（季度/半年）— 定基础暴露", long_stage(idx)),
        ("底部（季度）— 定能不能抄底", bottom_stage(breadth)),
        ("中期（2-6 周）— 定转折/中继", medium_stage(breadth)),
        ("短期（1-5 日）— 定明天/本周", short_stage(breadth)),
        ("当日（盘中）— 定今天下午", intraday_stage(breadth)),
    ):
        print(f"## {title}\n")
        print(_table(rows))
        print()
    print(
        "注：长期用 MA250/动量；中期用广度与 ADL；短期用收盘广度；"
        "当日用 10:10 广度 + MCL。中期‘超卖/投降段’历史上 20 日正收益很高"
        "（上证 +6.9%、中证1000 +12.3%），但样本只有 19 段且集中在几次大底，"
        "只能当‘不追空、等确认’，不能当抄底扳机。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
