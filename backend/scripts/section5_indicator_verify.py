#!/usr/bin/env python3
"""第五部分：新增指标的完整验证。

逐条验证：
1. 52 周新高−新低净占比（nh_nl）
2. ADL 新高背离（指数新高但 ADL 没新高）
3. 板块广度 / 板块离散度
4. 成交额集中度 HHI / 前 10 只成交占比
5. 两融余额变化（fin_chg20 / fin_bal_pct）
6. Breadth thrust（投降后 + EMA10 上穿 0.615）
7. 北向资金（仅历史，2024-08 后停更）
8. ERP / 10Y 国债 / 50ETF 期权 IV（数据缺口）

纪律：事件研究用 20/40 日、HAC t、前后两半同向、n_episodes≥30 才算“有效”。
输出：``section5_verification.md``。
"""

from __future__ import annotations

import os
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

from backend.scripts.breadth_divergence_probe import event_stats

ROOT = Path(os.environ.get("QM_QUANTDB_DATA_DIR", "data/quantdb"))
KLINE = ROOT / "1_kline_data" / "daily_backward"
BREADTH = ROOT / "5_technical_derived" / "market_breadth" / "trin_daily.parquet"
VAL = ROOT / "5_technical_derived" / "market_breadth" / "valuation_daily.parquet"
MARGIN = ROOT / "5_technical_derived" / "margin" / "panel_market.parquet"
SECTOR = ROOT / "5_technical_derived" / "margin" / "panel_sector_sentiment.parquet"
HHI = ROOT / "5_technical_derived" / "market_breadth" / "amount_concentration.parquet"
OUT = ROOT / "5_technical_derived" / "market_breadth" / "section5_verification.md"
SPLIT = pd.Timestamp("2021-07-01")
MIN_EP = 30


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


def build_hhi() -> pd.DataFrame:
    if HHI.exists():
        d = pd.read_parquet(HHI)
        d["date"] = pd.to_datetime(d["date"])
        return d.set_index("date")
    q = f"""
    WITH x AS (
        SELECT CAST(time AS DATE) d, symbol, amount
        FROM read_parquet('{KLINE}/dt=*/data.parquet')
        WHERE amount > 0
    ),
    y AS (
        SELECT d, symbol, amount, sum(amount) OVER (PARTITION BY d) tot
        FROM x
    ),
    z AS (
        SELECT *, row_number() OVER (PARTITION BY d ORDER BY amount DESC) rn
        FROM y
    )
    SELECT d AS date,
           sum((amount / tot) * (amount / tot)) AS hhi,
           sum(CASE WHEN rn <= 10 THEN amount / tot END) AS top10_share,
           sum(CASE WHEN rn <= 50 THEN amount / tot END) AS top50_share,
           count(*) AS n
    FROM z GROUP BY 1 ORDER BY 1
    """
    d = duckdb.connect().execute(q).fetchdf()
    d["date"] = pd.to_datetime(d["date"])
    d.to_parquet(HHI)
    return d.set_index("date")


def load_base() -> pd.DataFrame:
    d = pd.read_parquet(BREADTH).set_index("date").sort_index()
    d["up_ratio"] = d["n_up"] / (d["n_up"] + d["n_down"]) * 100
    d["nh_nl"] = (
        (d["n_high_52w"] - d["n_low_52w"]) / d["n_52w_base"].replace(0, np.nan) * 100
    )
    d["adl_hi"] = d["adl"] >= d["adl"].rolling(250, min_periods=250).max() - 1e-9
    d["idx_hi"] = (
        d["close_000001.SH"]
        >= d["close_000001.SH"].rolling(250, min_periods=250).max() - 1e-9
    )
    d["ema10"] = d["up_ratio"].ewm(span=10, adjust=False, min_periods=10).mean()
    d["wash5"] = d["up_ratio"].rolling(5).mean() < 25
    d["wash_recent"] = d["wash5"].rolling(20, min_periods=1).max().astype(bool)
    d["thrust"] = (d["ema10"] > 61.5) & (d["ema10"].shift(1) <= 61.5)
    d["thrust_after_wash"] = d["thrust"] & d["wash_recent"].shift(1).fillna(False)
    return d


def load_sector() -> pd.DataFrame:
    s = pd.read_parquet(SECTOR)
    s["date"] = pd.to_datetime(s["date"])
    g = s.groupby("date").agg(
        sector_up=("mom1", lambda x: float((x > 0).mean() * 100)),
        sector_disp=("mom1", "std"),
        n_sectors=("mom1", "size"),
    )
    return g


def load_margin() -> pd.DataFrame:
    m = pd.read_parquet(MARGIN)
    m["date"] = pd.to_datetime(m["date"])
    return m.set_index("date")


def _row(df: pd.DataFrame, mask: pd.Series, label: str, sym: str = "000001.SH") -> dict:
    s = event_stats(df, mask, sym, 20)
    s40 = event_stats(df, mask, sym, 40)
    a = event_stats(df[df.index < SPLIT], mask[mask.index < SPLIT], sym, 20)
    b = event_stats(df[df.index >= SPLIT], mask[mask.index >= SPLIT], sym, 20)
    if s.n_episodes < MIN_EP:
        verdict = "样本不足"
    elif abs(s.t_nw) >= 2 and np.sign(a.delta) == np.sign(b.delta):
        verdict = "有效"
    elif abs(s.t_nw) >= 2:
        verdict = "显著但前后半变号"
    else:
        verdict = "无效"
    return {
        "指标": label,
        "n_days": s.n_days,
        "n_episodes": s.n_episodes,
        "20日delta_%": round(s.delta, 2) if np.isfinite(s.delta) else np.nan,
        "20日t": round(s.t_nw, 2) if np.isfinite(s.t_nw) else np.nan,
        "40日delta_%": round(s40.delta, 2) if np.isfinite(s40.delta) else np.nan,
        "40日t": round(s40.t_nw, 2) if np.isfinite(s40.t_nw) else np.nan,
        "前半delta_%": round(a.delta, 2) if np.isfinite(a.delta) else np.nan,
        "后半delta_%": round(b.delta, 2) if np.isfinite(b.delta) else np.nan,
        "判定": verdict,
    }


def main() -> int:
    d = load_base()
    sector = load_sector()
    margin = load_margin()
    hhi = build_hhi()
    d = d.join(sector, how="left").join(hhi, how="left")
    d = d.join(
        margin[["fin_chg20", "fin_bal_pct", "fin_net20_ratio", "fin_chg5"]], how="left"
    )

    rows = []
    # 1. 52 周新高新低
    rows.append(_row(d, d["nh_nl"] > 5, "52周新高−新低 >5%"))
    rows.append(_row(d, d["nh_nl"] < -5, "52周新高−新低 <−5%"))
    # 2. ADL 新高背离
    rows.append(_row(d, d["idx_hi"] & ~d["adl_hi"], "指数250日新高，ADL未新高"))
    rows.append(_row(d, d["idx_hi"] & d["adl_hi"], "指数与ADL同时新高"))
    # 3. 板块广度/离散度
    rows.append(_row(d, d["sector_up"] < 30, "上涨板块占比 <30%"))
    rows.append(_row(d, d["sector_up"] > 70, "上涨板块占比 >70%"))
    disp_rank = d["sector_disp"].expanding(min_periods=500).rank(pct=True)
    rows.append(_row(d, disp_rank > 0.9, "板块离散度前10%"))
    # 4. 成交集中度
    hhi_rank = d["hhi"].expanding(min_periods=500).rank(pct=True)
    rows.append(_row(d, hhi_rank > 0.9, "成交额HHI前10%"))
    top_rank = d["top10_share"].expanding(min_periods=500).rank(pct=True)
    rows.append(_row(d, top_rank > 0.9, "前10只成交占比前10%"))
    # 5. 两融
    rows.append(_row(d, d["fin_chg20"] > 0, "两融余额20日变化 >0"))
    rows.append(_row(d, d["fin_chg20"] < 0, "两融余额20日变化 <0"))
    rows.append(_row(d, d["fin_bal_pct"] > 0.8, "两融余额历史分位 >80%"))
    # 6. breadth thrust
    rows.append(_row(d, d["thrust_after_wash"], "投降后 EMA10 上穿 0.615"))
    out = pd.DataFrame(rows)
    out = out.sort_values(["判定", "20日t"], ascending=[True, False])

    lines: list[str] = []
    a = lines.append
    a("# 第五部分：新增指标完整验证")
    a("")
    a(
        f"数据截至 {d.index[-1]:%Y-%m-%d}；20/40 日为上证指数条件收益，"
        "HAC t；判定要求 |t|≥2 且前后两半同向且 n_episodes≥30。"
    )
    a("")
    a(_md(out))
    a("")
    a("## 数据缺口（未验证）")
    a("")
    a("| 指标 | 状态 | 原因 |")
    a("|---|---|---|")
    a(
        "| ERP（EP − 10Y国债） | 缺口 | 本地无 10Y 收益率历史；MCP 当前只有当日曲线，且会话权限受限 |"
    )
    a("| 50ETF 期权 IV / 偏斜 | 缺口 | 本地只有美股/期货期权统计，无 A 股期权链 |")
    a(
        "| 北向资金 | 历史可用/当前停更 | 2024-08-19 起取消盘中与日频买卖披露；不能用于当前判断 |"
    )
    a("| 新增开户/基金发行 | 缺口 | 本地无数据 |")
    a("")
    a("## 结论")
    a("")
    a("- 只把“有效”且前后半同向的指标加入评分；其余降级为描述性或弃用。")
    a("- 样本不足的指标不做方向判断，只做风险提示。")
    text = "\n".join(lines)
    OUT.write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
