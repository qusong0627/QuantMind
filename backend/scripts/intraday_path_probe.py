#!/usr/bin/env python3
"""盘中路径确认：广度/MCL 的拐点规则能不能识别当日低点/高点。

数据：``l2_min1`` 全市场分钟线（2017-2026）+ ``daily_unadjusted`` 昨收。
在 14 个时点重建等权市场广度与 MCL，然后检验两条规则：

- 低点候选：10:10 广度 <30% → 11:30 广度 >50% 且 MCL5 日变化转正
- 高点候选：10:10 广度 >70% → 14:00 广度 <50% 且 MCL5 日变化转负

判据不是"事后看像不像"，而是：
1. 确认后的剩余时段收益是否显著优于未确认组；
2. 当日最低/最高点是否真的落在确认时点之后/之前（相对基准提升）。

用法::

    python -m backend.scripts.intraday_path_probe --build
    python -m backend.scripts.intraday_path_probe --reuse
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

ROOT = Path(os.environ.get("QM_QUANTDB_DATA_DIR", "data/quantdb"))
L2 = ROOT / "1_kline_data" / "l2_min1"
UNADJ = ROOT / "1_kline_data" / "daily_unadjusted"
OUT = ROOT / "5_technical_derived" / "market_breadth" / "intraday_path_panel.parquet"
REPORT = OUT.parent / "intraday_path.md"

TIMES = (
    "09:45:00",
    "10:00:00",
    "10:10:00",
    "10:30:00",
    "11:00:00",
    "11:30:00",
    "13:15:00",
    "13:30:00",
    "13:45:00",
    "14:00:00",
    "14:15:00",
    "14:30:00",
    "14:45:00",
    "15:00:00",
)


def build_panel() -> pd.DataFrame:
    times = ", ".join(f"TIME '{t}'" for t in TIMES)
    q = f"""
    WITH px AS (
        SELECT symbol, CAST(time AS DATE) d, close,
               lag(close) OVER (PARTITION BY symbol ORDER BY time) prev
        FROM read_parquet('{UNADJ}/dt=*/data.parquet')
    ),
    m AS (
        SELECT symbol, CAST(time AS DATE) d, CAST(time AS TIME) t, close
        FROM read_parquet('{L2}/dt=*/data.parquet')
        WHERE CAST(time AS TIME) IN ({times})
    )
    SELECT m.d AS date, m.t AS time, count(*) AS n,
           sum(CASE WHEN m.close / px.prev - 1 > 0 THEN 1 ELSE 0 END) AS n_up,
           sum(CASE WHEN m.close / px.prev - 1 < 0 THEN 1 ELSE 0 END) AS n_down,
           avg(m.close / px.prev - 1) AS ew_ret,
           median(m.close / px.prev - 1) AS med_ret
    FROM m JOIN px ON m.symbol = px.symbol AND m.d = px.d
    WHERE px.prev > 0 AND m.close > 0
    GROUP BY 1, 2
    ORDER BY 1, 2
    """
    df = duckdb.connect().execute(q).fetchdf()
    df["date"] = pd.to_datetime(df["date"])
    df["time"] = df["time"].astype(str).str.slice(0, 5)
    df["breadth"] = df["n_up"] / (df["n_up"] + df["n_down"]) * 100
    # 分时点的 MCL：把同一时点的净家数当成一条日频序列
    out = []
    for _t, g in df.groupby("time"):
        g = g.sort_values("date").copy()
        net = (g["n_up"] - g["n_down"]).astype(float)
        mcl = (
            net.ewm(span=19, adjust=False, min_periods=19).mean()
            - net.ewm(span=39, adjust=False, min_periods=39).mean()
        )
        g["mcl"] = mcl
        g["mcl_chg5"] = mcl - mcl.shift(5)
        out.append(g)
    df = pd.concat(out).sort_values(["date", "time"])
    df.to_parquet(OUT)
    return df


def load_panel() -> pd.DataFrame:
    if not OUT.exists():
        return build_panel()
    df = pd.read_parquet(OUT)
    df["date"] = pd.to_datetime(df["date"])
    df["time"] = df["time"].astype(str).str.slice(0, 5)
    return df


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


def _stat(x: pd.Series) -> dict:
    a = x.dropna()
    if len(a) < 2:
        return {
            "n": len(a),
            "mean_%": np.nan,
            "median_%": np.nan,
            "win_%": np.nan,
            "t": np.nan,
        }
    return {
        "n": len(a),
        "mean_%": round(float(a.mean() * 100), 3),
        "median_%": round(float(a.median() * 100), 3),
        "win_%": round(float((a > 0).mean() * 100), 1),
        "t": round(float(a.mean() / (a.std(ddof=1) / np.sqrt(len(a)))), 2),
    }


def _first_cross(
    row: pd.Series, start: str, threshold: float, above: bool, times: list[str]
) -> str | None:
    started = False
    for t in times:
        if t == start:
            started = True
            continue
        if not started:
            continue
        v = row.get(t)
        if pd.notna(v) and ((v > threshold) if above else (v < threshold)):
            return t
    return None


def cross_rules(
    b: pd.DataFrame, r: pd.DataFrame, low_t: pd.Series, high_t: pd.Series
) -> pd.DataFrame:
    """更早的确认：10:10<30 后首次回到 40；10:10>70 后首次跌破 60。"""
    times = list(b.columns)
    rows = []

    # 低点：首次回到 40，买入持有到收盘
    setup = b["10:10"] < 30
    sub = b[setup]
    cross = sub.apply(lambda row: _first_cross(row, "10:10", 40, True, times), axis=1)
    fwd, late = [], []
    for date, t in cross.items():
        if pd.isna(t):
            fwd.append(np.nan)
            late.append(np.nan)
            continue
        fwd.append((1 + r.loc[date, "15:00"]) / (1 + r.loc[date, t]) - 1)
        late.append(low_t.loc[date] < t)
    a = pd.Series(fwd).dropna()
    rows.append(
        {
            "规则": "低点：10:10<30 → 首次>40 买入持到收盘",
            "n": len(a),
            "均值_%": round(float(a.mean() * 100), 3),
            "中位_%": round(float(a.median() * 100), 3),
            "胜率_%": round(float((a > 0).mean() * 100), 1),
            "t": round(float(a.mean() / (a.std(ddof=1) / np.sqrt(len(a)))), 2),
            "极值已过_%": round(float(pd.Series(late).dropna().mean() * 100), 1),
        }
    )

    # 高点：首次跌破 60，卖出后持现金
    setup = b["10:10"] > 70
    sub = b[setup]
    cross = sub.apply(lambda row: _first_cross(row, "10:10", 60, False, times), axis=1)
    fwd, late = [], []
    for date, t in cross.items():
        if pd.isna(t):
            fwd.append(np.nan)
            late.append(np.nan)
            continue
        fwd.append((1 + r.loc[date, "15:00"]) / (1 + r.loc[date, t]) - 1)
        late.append(high_t.loc[date] > t)
    a = pd.Series(fwd).dropna()
    rows.append(
        {
            "规则": "高点：10:10>70 → 首次<60 离场（之后收益）",
            "n": len(a),
            "均值_%": round(float(a.mean() * 100), 3),
            "中位_%": round(float(a.median() * 100), 3),
            "胜率_%": round(float((a > 0).mean() * 100), 1),
            "t": round(float(a.mean() / (a.std(ddof=1) / np.sqrt(len(a)))), 2),
            "极值已过_%": round(float(pd.Series(late).dropna().mean() * 100), 1),
        }
    )
    return pd.DataFrame(rows)


def analyze(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    b = df.pivot(index="date", columns="time", values="breadth")
    m = df.pivot(index="date", columns="time", values="mcl_chg5")
    r = df.pivot(index="date", columns="time", values="ew_ret")
    low_t = r.idxmin(axis=1)
    high_t = r.idxmax(axis=1)

    # 剩余时段收益（相对确认时点）
    rest = {}
    for t in ("10:10", "11:30", "14:00"):
        rest[t] = (1 + r["15:00"]) / (1 + r[t]) - 1

    rows = []

    def add(label, mask, rest_t, timing, timing_label):
        mask = mask.reindex(b.index).fillna(False)
        ts = timing[timing_label]
        st = _stat(rest[rest_t][mask])
        base = _stat(rest[rest_t])
        base_timing = ts.mean() * 100
        rows.append(
            {
                "规则": label,
                "n": st["n"],
                "剩余收益_%": st["mean_%"],
                "中位_%": st["median_%"],
                "胜率_%": st["win_%"],
                "t": st["t"],
                "基准_%": base["mean_%"],
                "命中率_%": round(float(ts[mask].mean() * 100), 1)
                if mask.any()
                else np.nan,
                "基准命中_%": round(float(base_timing), 1),
            }
        )

    low_base = low_t >= "11:30"
    high_base = high_t <= "14:00"
    timing = {"low_after_1130": low_base, "high_before_1400": high_base}

    add(
        "全部（基准）",
        pd.Series(True, index=b.index),
        "10:10",
        timing,
        "low_after_1130",
    )
    add("低点：10:10<30", b["10:10"] < 30, "11:30", timing, "low_after_1130")
    add(
        "低点：10:10<30 ∧ 11:30>50",
        (b["10:10"] < 30) & (b["11:30"] > 50),
        "11:30",
        timing,
        "low_after_1130",
    )
    add(
        "低点：10:10<30 ∧ 11:30>50 ∧ MCL转正",
        (b["10:10"] < 30) & (b["11:30"] > 50) & (m["11:30"] > 0),
        "11:30",
        timing,
        "low_after_1130",
    )
    add(
        "低点：10:10<30 ∧ MCL转正",
        (b["10:10"] < 30) & (m["11:30"] > 0),
        "11:30",
        timing,
        "low_after_1130",
    )

    add("高点：10:10>70", b["10:10"] > 70, "14:00", timing, "high_before_1400")
    add(
        "高点：10:10>70 ∧ 14:00<50",
        (b["10:10"] > 70) & (b["14:00"] < 50),
        "14:00",
        timing,
        "high_before_1400",
    )
    add(
        "高点：10:10>70 ∧ 14:00<50 ∧ MCL转负",
        (b["10:10"] > 70) & (b["14:00"] < 50) & (m["14:00"] < 0),
        "14:00",
        timing,
        "high_before_1400",
    )
    add(
        "高点：10:10>70 ∧ MCL转负",
        (b["10:10"] > 70) & (m["14:00"] < 0),
        "14:00",
        timing,
        "high_before_1400",
    )

    # 分时点平均路径（全样本）
    path = (
        df.groupby("time")
        .agg(
            n_days=("date", "nunique"),
            breadth=("breadth", "mean"),
            ew_ret=("ew_ret", lambda s: s.mean() * 100),
        )
        .round(2)
        .reset_index()
    )
    return pd.DataFrame(rows), cross_rules(b, r, low_t, high_t), path


def report(stats: pd.DataFrame, cross: pd.DataFrame, path: pd.DataFrame) -> str:
    lines: list[str] = []
    a = lines.append
    a("# 盘中路径确认：能不能识别当日低点/高点")
    a("")
    a(
        f"14 个时点，{path['n_days'].max()} 个交易日；等权全市场（10:10 有成交、当日有收盘）。"
    )
    a("")
    a(
        "`剩余收益` 从确认时点到 15:00；`命中率`：低点规则=当日最低点出现在 11:30 之后的占比，"
        "高点规则=当日最高点出现在 14:00 之前的占比。"
    )
    a("")
    a("## 一、规则检验")
    a("")
    a(_md(stats))
    a("")
    a("## 一·B、更早的确认：首次穿越阈值")
    a("")
    a(
        "不等 11:30/14:00，而是在 10:10 之后**首次**回到 40 / 跌破 60 就动作；"
        "`极值已过_%` = 当日低点（或高点）已经发生在动作之前的占比——越高说明越是“事后确认”。"
    )
    a("")
    a(_md(cross))
    a("")
    a("## 二、全样本次日路径（等权收益，%）")
    a("")
    a(_md(path))
    a("")
    a("## 三、边界")
    a("")
    a("- 低/高点按 14 个采样时点定位，精度约 15 分钟；不是逐分钟极值。")
    a("- 未扣费、未考虑涨跌停/流动性；等权口径，不是指数。")
    a("- 规则是**路径确认**，不是预测；确认时点之后才可交易，收益从确认时点算。")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="盘中路径确认")
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--reuse", action="store_true")
    args = ap.parse_args(argv)
    df = load_panel() if args.reuse else build_panel()
    stats, cross, path = analyze(df)
    text = report(stats, cross, path)
    REPORT.write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
