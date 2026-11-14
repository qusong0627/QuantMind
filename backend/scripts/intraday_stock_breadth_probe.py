#!/usr/bin/env python3
"""盘中广度 → 个股大面积涨/跌 的横截面验证。

问题：
1. 10:10 大盘广度，能否预测**收盘时上涨个股占比**（大面积涨/跌）？
2. 在给定大盘广度下，个股 10:10 相对强弱（vs 全市场中位数）能否预测
   它自己 10:10→收盘 的收益？
3. 视频口径「指数红、广度差」是否真的对应午后大面积下跌？

数据：``l2_min1`` 10:10 快照（2017-2026）+ ``daily_unadjusted``（昨收/收盘）。
输出：``intraday_stock_breadth.md``。

用法::

    python -m backend.scripts.intraday_stock_breadth_probe
"""

from __future__ import annotations

import os
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

ROOT = Path(os.environ.get("QM_QUANTDB_DATA_DIR", "data/quantdb"))
SNAP = (
    ROOT / "5_technical_derived" / "market_breadth" / "intraday_snapshot_1010.parquet"
)
PANEL = (
    ROOT / "5_technical_derived" / "market_breadth" / "intraday_market_panel.parquet"
)
UNADJ = ROOT / "1_kline_data" / "daily_unadjusted"
OUT = SNAP.parent / "intraday_stock_breadth.md"


def _md(df: pd.DataFrame) -> str:
    cols = [str(c) for c in df.columns]
    lines = ["| " + " | ".join(cols) + " |", "|" + "|".join(["---"] * len(cols)) + "|"]
    for _, row in df.iterrows():
        lines.append(
            "| "
            + " | ".join(
                "" if isinstance(v, float) and not np.isfinite(v) else str(v)
                for v in row
            )
            + " |"
        )
    return "\n".join(lines)


def build_stock_panel() -> pd.DataFrame:
    con = duckdb.connect()
    con.execute("SET threads=4")
    con.execute(f"""
        CREATE TEMP TABLE b AS
        SELECT CAST(date AS DATE) d, up_share, mcl_chg5, ret_rest_ew
        FROM read_parquet('{PANEL}')
    """)
    con.execute(f"""
        CREATE TEMP TABLE px AS
        SELECT symbol, CAST(time AS DATE) d, close,
               lag(close) OVER (PARTITION BY symbol ORDER BY time) prev_close
        FROM read_parquet('{UNADJ}/dt=*/data.parquet')
    """)
    q = f"""
    SELECT s.symbol, CAST(s.date AS DATE) d,
           s.amt40, s.px40, p.prev_close, p.close,
           s.px40 / p.prev_close - 1 AS ret_m,
           p.close / s.px40 - 1 AS ret_r,
           b.up_share, b.mcl_chg5
    FROM read_parquet('{SNAP}') s
    JOIN px p ON s.symbol = p.symbol AND CAST(s.date AS DATE) = p.d
    JOIN b ON CAST(s.date AS DATE) = b.d
    WHERE p.prev_close > 0 AND s.px40 > 0 AND p.close > 0
      AND s.amt40 > 0
    """
    df = con.execute(q).fetchdf()
    # 个股 10:10 相对强弱 = 早盘收益 − 当日全市场中位数
    med = df.groupby("d")["ret_m"].transform("median")
    df["rs"] = df["ret_m"] - med
    df["rs_q"] = df.groupby("d")["rs"].transform(
        lambda s: pd.qcut(
            s, 5, labels=["Q1最弱", "Q2", "Q3", "Q4", "Q5最强"], duplicates="drop"
        )
    )
    return df


def daily_agg(df: pd.DataFrame) -> pd.DataFrame:
    df["wret_m"] = df["ret_m"] * df["amt40"]
    g = (
        df.groupby("d")
        .agg(
            n=("ret_r", "size"),
            up_share_1010=("up_share", "first"),
            mcl_chg5=("mcl_chg5", "first"),
            median_morning=("ret_m", lambda s: s.median() * 100),
            share_up_rest=("ret_r", lambda s: (s > 0).mean() * 100),
            share_down_rest=("ret_r", lambda s: (s < 0).mean() * 100),
            median_rest=("ret_r", lambda s: s.median() * 100),
            mean_rest=("ret_r", lambda s: s.mean() * 100),
            wret_sum=("wret_m", "sum"),
            amt_sum=("amt40", "sum"),
        )
        .reset_index()
    )
    g["capw_morning"] = g["wret_sum"] / g["amt_sum"] * 100
    return g


def breadth_bucket_table(daily: pd.DataFrame) -> pd.DataFrame:
    bins = [-np.inf, 30, 40, 50, 60, 70, np.inf]
    labels = ["<30", "30-40", "40-50", "50-60", "60-70", ">=70"]
    daily = daily.copy()
    daily["bucket"] = pd.cut(daily["up_share_1010"], bins, labels=labels, right=False)
    out = (
        daily.groupby("bucket", observed=True)
        .agg(
            days=("d", "size"),
            share_up_rest=("share_up_rest", "mean"),
            share_down_rest=("share_down_rest", "mean"),
            median_rest=("median_rest", "mean"),
            mean_rest=("mean_rest", "mean"),
        )
        .round(2)
    )
    return out.reset_index()


def rs_table(df: pd.DataFrame, max_abs_morning: float | None = None) -> pd.DataFrame:
    d = df.copy()
    if max_abs_morning is not None:
        d = d[d["ret_m"].abs() <= max_abs_morning]
    d["bucket"] = pd.cut(
        d["up_share"],
        [-np.inf, 30, 40, 50, 60, 70, np.inf],
        labels=["<30", "30-40", "40-50", "50-60", "60-70", ">=70"],
        right=False,
    )
    out = (
        d.groupby(["bucket", "rs_q"], observed=True)
        .agg(
            n=("ret_r", "size"),
            p_up=("ret_r", lambda s: (s > 0).mean() * 100),
            mean_rest=("ret_r", lambda s: s.mean() * 100),
            median_rest=("ret_r", lambda s: s.median() * 100),
        )
        .round(2)
    )
    return out.reset_index()


def video_setup(daily: pd.DataFrame) -> pd.DataFrame:
    """视频口径「指数红、广度差」：早盘全市场中位收益 >0 且 10:10 广度 <40。"""
    rows = []
    for lab, m in (
        ("广度<40（全部）", daily["up_share_1010"] < 40),
        (
            "广度<40 ∧ 指数红(成交额加权)",
            (daily["up_share_1010"] < 40) & (daily["capw_morning"] > 0),
        ),
        (
            "广度<40 ∧ 中位股红",
            (daily["up_share_1010"] < 40) & (daily["median_morning"] > 0),
        ),
        (
            "广度<40 ∧ 指数绿",
            (daily["up_share_1010"] < 40) & (daily["capw_morning"] <= 0),
        ),
        ("广度>=60", daily["up_share_1010"] >= 60),
    ):
        x = daily[m]
        rows.append(
            {
                "状态": lab,
                "days": len(x),
                "收盘上涨占比_%": round(float(x["share_up_rest"].mean()), 1)
                if len(x)
                else np.nan,
                "收盘下跌占比_%": round(float(x["share_down_rest"].mean()), 1)
                if len(x)
                else np.nan,
                "中位剩余收益_%": round(float(x["median_rest"].mean()), 2)
                if len(x)
                else np.nan,
            }
        )
    return pd.DataFrame(rows)


def report(
    daily: pd.DataFrame,
    rs: pd.DataFrame,
    rs_ex: pd.DataFrame,
    video: pd.DataFrame,
    corr: dict,
) -> str:
    lines: list[str] = []
    a = lines.append
    a("# 盘中广度 → 个股大面积涨/跌")
    a("")
    a(
        f"区间 {daily['d'].min()} ~ {daily['d'].max()}，{len(daily)} 个交易日；"
        "个股样本为 10:10 有成交且当日有收盘的股票。"
    )
    a("")
    a("## 一、10:10 大盘广度 → 收盘时上涨个股占比")
    a("")
    a(_md(breadth_bucket_table(daily)))
    a("")
    a(
        "`share_up_rest` = 当日收盘价高于 10:10 价的个股占比；"
        "`median_rest` = 个股 10:10→收盘收益的中位数。"
    )
    a("")
    a("## 二、个股 10:10 相对强弱 → 它自己的剩余时段")
    a("")
    a(_md(rs))
    a("")
    a("`rs_q` 是当日横截面按 10:10 相对强弱分的五档（Q5 最强）。")
    a("")
    a("剔除早盘涨跌幅超过 ±9% 的股票后（排除涨跌停/接近涨跌停的机械效应）：")
    a("")
    a(_md(rs_ex))
    a("")
    a("## 三、视频口径：指数红、广度差")
    a("")
    a(_md(video))
    a("")
    a("## 四、相关性")
    a("")
    a(_md(pd.DataFrame([corr])))
    a("")
    a("## 五、边界")
    a("")
    a("- 这是**横截面概率**，不是个股点预测；同一状态下的离散度仍然很大。")
    a("- 10:10 快照不含 10:10 之后的路径；无法判断盘中高低点的精确位置。")
    a("- 未扣费、未考虑涨跌停/停牌/流动性；只用于判断‘大面积涨/跌’的倾斜方向。")
    return "\n".join(lines)


def main() -> int:
    df = build_stock_panel()
    daily = daily_agg(df)
    rs = rs_table(df)
    rs_ex = rs_table(df, max_abs_morning=0.09)
    corr = {
        "corr(10:10广度, 收盘上涨占比)": round(
            float(daily["up_share_1010"].corr(daily["share_up_rest"])), 3
        ),
        "corr(10:10广度, 中位剩余收益)": round(
            float(daily["up_share_1010"].corr(daily["median_rest"])), 3
        ),
        "corr(MCL5日变化, 收盘上涨占比)": round(
            float(daily["mcl_chg5"].corr(daily["share_up_rest"])), 3
        ),
    }
    text = report(daily, rs, rs_ex, video_setup(daily), corr)
    OUT.write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
