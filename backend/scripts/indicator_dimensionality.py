#!/usr/bin/env python3
"""20 个市场级指标的有效维度检验（PCA + 相关性）。

回答：机构说“用接近 20 个指标综合判断”，这 20 个到底有几个独立方向？
输出：``indicator_dimensionality.md``。

用法::

    python -m backend.scripts.indicator_dimensionality
"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(os.environ.get("QM_QUANTDB_DATA_DIR", "data/quantdb"))
BREADTH = ROOT / "5_technical_derived" / "market_breadth" / "trin_daily.parquet"
VAL = ROOT / "5_technical_derived" / "market_breadth" / "valuation_daily.parquet"
HHI = ROOT / "5_technical_derived" / "market_breadth" / "amount_concentration.parquet"
MARGIN = ROOT / "5_technical_derived" / "margin" / "panel_market.parquet"
SECTOR = ROOT / "5_technical_derived" / "margin" / "panel_sector_sentiment.parquet"
OUT = ROOT / "5_technical_derived" / "market_breadth" / "indicator_dimensionality.md"


def _md(df: pd.DataFrame) -> str:
    cols = [str(c) for c in df.columns]
    lines = ["| " + " | ".join(cols) + " |", "|" + "|".join(["---"] * len(cols)) + "|"]
    for _, row in df.iterrows():
        lines.append("| " + " | ".join(
            "" if isinstance(v, float) and not np.isfinite(v) else str(v)
            for v in row) + " |")
    return "\n".join(lines)


def build_features() -> pd.DataFrame:
    d = pd.read_parquet(BREADTH).set_index("date").sort_index()
    v = pd.read_parquet(VAL).sort_index()
    d = d.join(v[["bp_capw_pct", "ep_capw"]], how="left")
    h = pd.read_parquet(HHI)
    h["date"] = pd.to_datetime(h["date"])
    d = d.join(h.set_index("date")[["hhi", "top10_share"]], how="left")
    m = pd.read_parquet(MARGIN)
    m["date"] = pd.to_datetime(m["date"])
    d = d.join(m.set_index("date")[["fin_chg20"]], how="left")
    s = pd.read_parquet(SECTOR)
    s["date"] = pd.to_datetime(s["date"])
    sec = s.groupby("date").agg(
        sector_up=("mom1", lambda x: float((x > 0).mean() * 100)),
        sector_disp=("mom1", "std"),
    )
    d = d.join(sec, how="left")

    c = d["close_000001.SH"]
    out = pd.DataFrame(index=d.index)
    out["距离MA250"] = c / c.rolling(250, min_periods=250).mean() - 1
    out["250日动量"] = c / c.shift(250) - 1
    out["PB便宜分位"] = d["bp_capw_pct"]
    out["EP"] = d["ep_capw"]
    out["站上MA20"] = d["pct_above_ma20"]
    out["站上MA60"] = d["pct_above_ma60"]
    out["ADL20斜率"] = d["adl"].diff(20) / d["n_total"]
    out["52周新高新低"] = (d["n_high_52w"] - d["n_low_52w"]) / d["n_52w_base"].replace(0, np.nan)
    out["TRIN_额"] = d["trin_amt"]
    out["TRIN_量"] = d["trin_vol"]
    out["额价差"] = d["amt_up"] / d["amt_total"] * 100 - d["n_up"] / (d["n_up"] + d["n_down"]) * 100
    out["MCL"] = d["mcclellan"]
    out["涨家占比"] = d["n_up"] / (d["n_up"] + d["n_down"]) * 100
    out["5日涨家"] = out["涨家占比"].rolling(5).mean()
    out["20日波动"] = c.pct_change().rolling(20).std() * np.sqrt(244)
    out["距250日高"] = c / c.rolling(250, min_periods=250).max() - 1
    out["成交额HHI"] = d["hhi"]
    out["前10成交占比"] = d["top10_share"]
    out["两融20日变化"] = d["fin_chg20"]
    out["上涨板块占比"] = d["sector_up"]
    out["板块离散度"] = d["sector_disp"]
    return out.loc["2018-01-01":]


def main() -> int:
    feats = build_features()
    X = feats.dropna()
    Xz = (X - X.mean()) / X.std(ddof=0)
    corr = np.corrcoef(Xz.to_numpy().T)
    eigvals = np.linalg.eigvalsh(corr)[::-1]
    eigvals = np.clip(eigvals, 0, None)
    var = eigvals / eigvals.sum()
    cum = np.cumsum(var)
    n80 = int(np.searchsorted(cum, 0.80) + 1)
    n90 = int(np.searchsorted(cum, 0.90) + 1)
    n_eff = float(eigvals.sum() ** 2 / np.sum(eigvals ** 2))

    # 每个指标的最高相关伙伴
    pairs = []
    names = list(X.columns)
    for i, n in enumerate(names):
        j = int(np.argsort(np.abs(corr[i]))[-2])
        pairs.append({"指标": n, "最高相关": names[j],
                      "相关": round(float(corr[i, j]), 2)})
    pairs = pd.DataFrame(pairs).sort_values("相关", ascending=False)

    lines: list[str] = []
    a = lines.append
    a("# 20 个市场级指标的有效维度")
    a("")
    a(f"样本 {X.index[0]:%Y-%m-%d} ~ {X.index[-1]:%Y-%m-%d}，"
      f"{len(X)} 个交易日，{len(names)} 个指标（标准化后 PCA）。")
    a("")
    a("## 一、有效维度")
    a("")
    a(f"- 解释 80% 方差需要 **{n80} 个主成分**；解释 90% 需要 **{n90} 个**。")
    a(f"- 有效独立下注数 **N_eff = {n_eff:.1f}**（特征值平方和口径）。")
    a(f"- 第一主成分解释 **{var[0]*100:.1f}%**，前三个合计 **{cum[2]*100:.1f}%**，"
      f"前五个合计 **{cum[4]*100:.1f}%**。")
    a("")
    a(_md(pd.DataFrame({
        "主成分": [f"PC{i+1}" for i in range(min(10, len(var)))],
        "特征值": [round(float(x), 3) for x in eigvals[:10]],
        "解释方差_%": [round(float(x) * 100, 1) for x in var[:10]],
        "累计_%": [round(float(x) * 100, 1) for x in cum[:10]],
    })))
    a("")
    a("## 二、冗余度最高的指标对")
    a("")
    a(_md(pairs.head(15)))
    a("")
    a("## 三、怎么用这 20 个")
    a("")
    a(f"- 它们实际约 **{n80} 个独立方向**；直接等权相加 = 给高相关块重复加权。")
    a("- 先按 block 求块内均值/第一主成分，再对 block 等权或风险平价，是更稳的做法。")
    a("- 权重不要用全样本优化；用等权/风险平价 + walk-forward 验证。")
    text = "\n".join(lines)
    OUT.write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
