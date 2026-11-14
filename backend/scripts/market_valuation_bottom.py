#!/usr/bin/env python3
"""底部信号层：全市场估值分位（PB / EP / 中位 PE）。

数据源：``5_technical_derived/valuation/dt=*/data.parquet``（逐票日频，
2018 起 PE 覆盖 ~99%；2016-2017 只有市值/股息，PE 不可用）。

口径：
- ``bp_capw`` = Σ净资产 / Σ总市值（账面市值比，越高越便宜；等价于 1/PB）
- ``ep_capw`` = Σ净利润TTM / Σ总市值（含亏损股，可能为负）
- ``med_pe``  = 截面 PE 中位数（只取 0<PE<500）
- ``div_yield`` = 市值加权股息率
- 分位一律用**滚动 3 年**（756 交易日，至少 500 日），不看未来。

输出：
- ``data/quantdb/5_technical_derived/market_breadth/valuation_daily.parquet``
- 报告 ``valuation_bottom.md``

用法::

    python -m backend.scripts.market_valuation_bottom --build
    python -m backend.scripts.market_valuation_bottom
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import pyarrow.parquet as pq

ROOT = Path(os.environ.get("QM_QUANTDB_DATA_DIR", "data/quantdb"))
VAL_DIR = ROOT / "5_technical_derived" / "valuation"
IDX_DIR = ROOT / "1_kline_data" / "index_daily"
OUT = ROOT / "5_technical_derived" / "market_breadth" / "valuation_daily.parquet"
REPORT = OUT.parent / "valuation_bottom.md"
TARGETS = {"000001.SH": "上证指数", "000300.SH": "沪深300", "000852.SH": "中证1000"}
HORIZONS = (60, 120, 250)
WINDOW = 756
MIN_PERIODS = 500


# --------------------------------------------------------------------------
# 构建
# --------------------------------------------------------------------------
def build_panel() -> pd.DataFrame:
    q = f"""
    SELECT
        CAST(time AS DATE) AS date,
        count(*) AS n,
        sum(CASE WHEN total_mv > 0 AND equity > 0 THEN equity END)
            / sum(CASE WHEN total_mv > 0 AND equity > 0 THEN total_mv END) AS bp_capw,
        sum(CASE WHEN total_mv > 0 THEN net_profit_ttm END)
            / sum(CASE WHEN total_mv > 0 THEN total_mv END) AS ep_capw,
        median(CASE WHEN pe_ttm > 0 AND pe_ttm < 500 THEN pe_ttm END) AS med_pe,
        sum(CASE WHEN total_mv > 0 THEN dividend_rate * total_mv END)
            / sum(CASE WHEN total_mv > 0 THEN total_mv END) AS div_yield
    FROM read_parquet('{VAL_DIR}/dt=*/data.parquet')
    GROUP BY 1
    ORDER BY 1
    """
    df = duckdb.connect().execute(q).fetchdf()
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date")
    for c in ("bp_capw", "ep_capw", "med_pe", "div_yield"):
        df[f"{c}_pct"] = (
            df[c]
            .rolling(WINDOW, min_periods=MIN_PERIODS)
            .apply(lambda s: float((s.iloc[-1] >= s).mean()), raw=False)
        )
    # med_pe 越低越便宜，翻转成“便宜分位”
    df["med_pe_cheap_pct"] = 1.0 - df["med_pe_pct"]
    df.to_parquet(OUT)
    return df


def load_panel() -> pd.DataFrame:
    if not OUT.exists():
        return build_panel()
    df = pd.read_parquet(OUT).sort_index()
    df.index = pd.to_datetime(df.index)
    return df


def load_index(symbol: str) -> pd.Series:
    vals = {}
    for tag in sorted(os.listdir(IDX_DIR)):
        p = IDX_DIR / tag / "data.parquet"
        if not p.exists():
            continue
        t = pq.read_table(p, columns=["symbol", "close"]).to_pandas()
        r = t[t["symbol"] == symbol]
        if not r.empty:
            vals[pd.Timestamp(tag.split("=")[1])] = float(r["close"].iloc[0])
    return pd.Series(vals).sort_index()


# --------------------------------------------------------------------------
# 条件收益
# --------------------------------------------------------------------------
def _stats(df: pd.DataFrame, mask: pd.Series, horizon: int) -> dict:
    y = df[f"fwd_{horizon}"]
    x = y[mask.reindex(df.index).fillna(False)].dropna()
    b = y.dropna()
    if len(x) < 2:
        return {
            "n": len(x),
            "mean_%": np.nan,
            "win_%": np.nan,
            "base_%": round(float(b.mean()), 2),
        }
    return {
        "n": len(x),
        "mean_%": round(float(x.mean()), 2),
        "median_%": round(float(x.median()), 2),
        "win_%": round(float((x > 0).mean() * 100), 1),
        "base_%": round(float(b.mean()), 2),
        "t": round(float(x.mean() / (x.std(ddof=1) / np.sqrt(len(x)))), 2),
    }


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


def report(panel: pd.DataFrame) -> str:
    idx = {s: load_index(s) for s in TARGETS}
    d = panel.loc["2018-01-01":].copy()
    for s in TARGETS:
        c = idx[s].reindex(d.index).ffill()
        for h in HORIZONS:
            d[f"fwd_{h}_{s}"] = (c.shift(-h) / c - 1.0) * 100
    # 月末采样，避免重叠样本把 n 吹大
    monthly = d.groupby([d.index.year, d.index.month]).tail(1)

    lines: list[str] = []
    a = lines.append
    a("# 全市场估值底部信号")
    a("")
    a(
        f"数据 {d.index[0]:%Y-%m-%d} ~ {d.index[-1]:%Y-%m-%d}，"
        f"{len(d)} 个交易日；月末采样 {len(monthly)} 个观测。"
    )
    a("")
    a("分位 = 滚动 3 年（756 日）分位，越高越便宜（PB/EP）或越贵（中位 PE 已翻转）。")
    a("")
    a("## 一、当前估值位置")
    a("")
    last = d.iloc[-1]
    a(
        _md(
            pd.DataFrame(
                [
                    {
                        "日期": f"{d.index[-1]:%Y-%m-%d}",
                        "PB": round(float(last["bp_capw"]), 3),
                        "PB便宜分位": round(float(last["bp_capw_pct"]), 3),
                        "EP_%": round(float(last["ep_capw"]) * 100, 2),
                        "EP便宜分位": round(float(last["ep_capw_pct"]), 3),
                        "中位PE": round(float(last["med_pe"]), 2),
                        "股息率_%": round(float(last["div_yield"]) * 100, 2),
                    }
                ]
            )
        )
    )
    a("")
    a("## 二、估值分位 → 未来 60/120/250 日（月末采样）")
    a("")
    for s, name in TARGETS.items():
        for meas, col in (("PB", "bp_capw_pct"), ("EP", "ep_capw_pct")):
            tmp = monthly.copy()
            tmp["fwd_60"] = tmp[f"fwd_60_{s}"]
            tmp["fwd_120"] = tmp[f"fwd_120_{s}"]
            tmp["fwd_250"] = tmp[f"fwd_250_{s}"]
            rows = []
            for lo, hi, lab in (
                (0.8, 1.01, "便宜前20%"),
                (0.6, 0.8, "20-40%"),
                (0.4, 0.6, "40-60%"),
                (0.2, 0.4, "60-80%"),
                (0.0, 0.2, "最贵20%"),
            ):
                m = (tmp[col] >= lo) & (tmp[col] < hi)
                row = {"估值": lab}
                for h in HORIZONS:
                    row[f"{h}d"] = _stats(tmp, m, h)["mean_%"]
                    row[f"{h}d_n"] = _stats(tmp, m, h)["n"]
                rows.append(row)
            a(f"**{name} · {meas} 分位**")
            a("")
            a(_md(pd.DataFrame(rows)))
            a("")
    a("## 三、底部组合：估值便宜 + 中期投降")
    a("")
    a("估值便宜 = PB 分位 > 0.8；投降 = 5 日涨家占比均值 < 25%。")
    a("")
    # 需要广度面板的 up_ratio
    breadth = (
        pd.read_parquet(
            ROOT / "5_technical_derived" / "market_breadth" / "trin_daily.parquet"
        )
        .set_index("date")
        .sort_index()
    )
    breadth["up_ratio"] = breadth["n_up"] / (breadth["n_up"] + breadth["n_down"]) * 100
    breadth["wash5"] = breadth["up_ratio"].rolling(5).mean() < 25
    rows = []
    for s, name in TARGETS.items():
        b = breadth.reindex(d.index)
        for lab, m in (
            ("便宜", d["bp_capw_pct"] > 0.8),
            ("便宜+投降", (d["bp_capw_pct"] > 0.8) & b["wash5"].fillna(False)),
            ("便宜+不投降", (d["bp_capw_pct"] > 0.8) & ~b["wash5"].fillna(False)),
        ):
            for h in (20, 60):
                y = (
                    idx[s].reindex(d.index).ffill().shift(-h)
                    / idx[s].reindex(d.index).ffill()
                    - 1
                ) * 100
                x = y[m.fillna(False)].dropna()
                rows.append(
                    {
                        "target": name,
                        "组合": lab,
                        "h": h,
                        "n": len(x),
                        "mean_%": round(float(x.mean()), 2) if len(x) else np.nan,
                        "win_%": round(float((x > 0).mean() * 100), 1)
                        if len(x)
                        else np.nan,
                    }
                )
    a(_md(pd.DataFrame(rows)))
    a("")
    mask = (d["bp_capw_pct"] > 0.8) & b["wash5"].fillna(False).reindex(d.index)
    hit = d.index[mask.fillna(False)]
    if len(hit):
        gaps = pd.Series(hit).diff().dt.days.fillna(999)
        n_ep = int((gaps > 10).sum())
        a(
            f"触发日共 {len(hit)} 天，分成 **{n_ep} 段**："
            + "、".join(str(x.date()) for x in hit)
            + "。"
        )
        a("")
    a("## 四、边界")
    a("")
    a("- 2016-2017 的 PE 覆盖不足 10%，样本从 2018 起；只有约 2-3 个估值周期。")
    a(
        "- 这是**全市场市值加权**估值，不是指数成分股口径；用于判断“整体贵/便宜”，不用于择时精确到日。"
    )
    a("- ERP（EP − 10Y 国债）待接 10Y 历史；当前 MCP 只有当日曲线。")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="全市场估值底部信号")
    ap.add_argument("--build", action="store_true")
    args = ap.parse_args(argv)
    panel = build_panel() if args.build else load_panel()
    text = report(panel)
    REPORT.write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
