#!/usr/bin/env python3
"""综合判断器：把长期/估值/中期/短期/盘中五层合成一个可对账的判断。

原则：
1. 长期（趋势 × 估值）决定**基础暴露**，用 250 日条件收益校准；
2. 中期（广度 × ADL）决定**倾斜**；
3. 短期/盘中只决定**执行时点**，不改基础暴露；
4. 样本不足的合取（如五指标见顶）不进入评分，只作案例回放。

用法::

    python -m backend.scripts.market_composite_judge
"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pandas as pd

from backend.scripts.market_stage_dashboard import (
    bottom_stage,
    intraday_stage,
    load_breadth_tail,
    load_index,
    load_valuation,
    medium_stage,
    short_stage,
)

ROOT = Path(os.environ.get("QM_QUANTDB_DATA_DIR", "data/quantdb"))
PANEL = ROOT / "5_technical_derived" / "market_breadth" / "trin_daily.parquet"
TARGETS = {"000001.SH": "上证指数", "000300.SH": "沪深300", "000852.SH": "中证1000"}
CHEAP = 0.5  # PB 滚动 3 年便宜分位 > 50% 视为“便宜侧”


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


def quadrant_table() -> pd.DataFrame:
    """长期 2×2：指数 vs MA250 × PB 便宜分位，250 日条件收益。"""
    d = pd.read_parquet(PANEL).set_index("date").sort_index()
    v = pd.read_parquet(
        ROOT / "5_technical_derived" / "market_breadth" / "valuation_daily.parquet"
    ).sort_index()
    d = d.join(v[["bp_capw_pct"]], how="left")
    d = d[d["bp_capw_pct"].notna()]
    rows = []
    for sym, name in TARGETS.items():
        c = d[f"close_{sym}"]
        above = c > c.rolling(250, min_periods=250).mean()
        cheap = d["bp_capw_pct"] > CHEAP
        y = (c.shift(-250) / c - 1) * 100
        for a in (True, False):
            for ch in (True, False):
                m = (above == a) & (cheap == ch)
                x = y[m].dropna()
                rows.append(
                    {
                        "指数": name,
                        "趋势": "MA250上方" if a else "MA250下方",
                        "估值": "便宜侧" if ch else "贵侧",
                        "n": len(x),
                        "250日均值_%": round(float(x.mean()), 2) if len(x) else np.nan,
                        "胜率_%": round(float((x > 0).mean() * 100), 1)
                        if len(x)
                        else np.nan,
                    }
                )
    return pd.DataFrame(rows)


def base_exposure(above_ma250: bool, cheap: bool) -> tuple[float, float, str]:
    """长期 2×2 → 基础暴露区间。"""
    if above_ma250 and cheap:
        return 0.8, 1.0, "趋势+估值双顺"
    if above_ma250 and not cheap:
        return 0.3, 0.5, "趋势在但估值贵，持有不加"
    if not above_ma250 and cheap:
        return 0.4, 0.6, "便宜但趋势未修复，等右侧"
    return 0.2, 0.4, "趋势向下且估值不便宜，防守"


def case_replay() -> pd.DataFrame:
    d = pd.read_parquet(PANEL).set_index("date").sort_index()
    v = pd.read_parquet(
        ROOT / "5_technical_derived" / "market_breadth" / "valuation_daily.parquet"
    ).sort_index()
    d = d.join(v[["bp_capw_pct"]], how="left")
    d["up"] = d.n_up / (d.n_up + d.n_down) * 100
    d["above60"] = d.pct_above_ma60 * 100
    d["adl20"] = d.adl.diff(20) / d.n_total * 100
    d["adl_chg10"] = d.adl20 - d.adl20.shift(10)
    rows = []
    for dt in ("2021-09-09", "2025-07-23", "2025-08-12", "2026-03-02"):
        r = d.loc[dt]
        c = d["close_000001.SH"]
        i = d.index.get_loc(pd.Timestamp(dt))
        ma = c.rolling(250).mean().iloc[i]
        rows.append(
            {
                "日期": dt,
                "vsMA250_%": round((c.iloc[i] / ma - 1) * 100, 1),
                "250日动量_%": round((c.iloc[i] / c.iloc[i - 250] - 1) * 100, 1),
                "PB便宜分位": round(float(r["bp_capw_pct"]), 3),
                "站上MA60_%": round(float(r["above60"]), 1),
                "ADL20": round(float(r["adl20"]), 1),
                "ADL10日变化": round(float(r["adl_chg10"]), 1),
                "收盘涨家_%": round(float(r["up"]), 1),
                "后20日_%": round(float((c.iloc[i + 20] / c.iloc[i] - 1) * 100), 2),
            }
        )
    return pd.DataFrame(rows)


def main() -> int:
    idx = load_index("000001.SH")
    breadth = load_breadth_tail()
    val = load_valuation()
    last = val.iloc[-1] if not val.empty else None
    c = float(idx.iloc[-1])
    ma250 = float(idx.rolling(250, min_periods=250).mean().iloc[-1])
    above = c > ma250
    cheap = bool(last["bp_capw_pct"] > CHEAP) if last is not None else False
    lo, hi, why = base_exposure(above, cheap)

    med = medium_stage(breadth)
    short = short_stage(breadth)
    intra = intraday_stage(breadth)
    bottom = bottom_stage(breadth, val)

    idx_hi = c >= float(idx.rolling(250, min_periods=250).max().iloc[-1]) - 1e-9
    adl = breadth["adl"]
    adl_hi = (
        float(adl.iloc[-1])
        >= float(adl.rolling(250, min_periods=250).max().iloc[-1]) - 1e-9
    )
    adl_div = bool(idx_hi and not adl_hi)
    top_risk = (
        "顶部背离：指数250日新高但ADL未新高（20日 −2.0%，t −2.18，39段）"
        if adl_div
        else "无（指数未在250日新高，或ADL同步新高）"
    )

    lines: list[str] = []
    a = lines.append
    a("# 综合判断：五层合成")
    a("")
    a(f"数据截至 {breadth.index[-1]:%Y-%m-%d}。")
    a("")
    a("## 一、当前五层")
    a("")
    a(
        _md(
            pd.DataFrame(
                [
                    {
                        "层级": "长期",
                        "结论": "MA250上方" if above else "MA250下方",
                        "关键读数": f"距MA250 {((c / ma250) - 1) * 100:+.1f}%，250日动量 "
                        f"{(c / float(idx.iloc[-251]) - 1) * 100:+.1f}%",
                    },
                    {
                        "层级": "顶风险",
                        "结论": "ADL背离" if adl_div else "无",
                        "关键读数": top_risk,
                    },
                    {
                        "层级": "估值",
                        "结论": "便宜侧" if cheap else "贵侧",
                        "关键读数": f"PB {float(last['bp_capw']):.3f}，便宜分位 "
                        f"{float(last['bp_capw_pct']):.3f}"
                        if last is not None
                        else "—",
                    },
                    {
                        "层级": "中期",
                        "结论": med["阶段"],
                        "关键读数": f"站上MA60 {med['站上MA60_%']}%，ADL20 {med['ADL20斜率']}，"
                        f"TRIN_量 {med['TRIN_量']}",
                    },
                    {
                        "层级": "短期",
                        "结论": short["阶段"],
                        "关键读数": f"收盘涨家 {short['收盘涨家_%']}%",
                    },
                    {
                        "层级": "盘中",
                        "结论": intra["阶段"],
                        "关键读数": f"{intra.get('数据日期', '')} 10:10涨家 "
                        f"{intra.get('10:10涨家_%', '—')}%，MCL {intra.get('MCL5日变化', '—')}",
                    },
                    {
                        "层级": "底部",
                        "结论": bottom["阶段"],
                        "关键读数": f"5日均涨家 {bottom.get('5日均涨家_%', '—')}%",
                    },
                ]
            )
        )
    )
    a("")
    a("## 二、长期 2×2：趋势 × 估值 → 250 日条件收益")
    a("")
    a(_md(quadrant_table()))
    a("")
    a("## 三、基础暴露")
    a("")
    a(f"**{lo:.1f}–{hi:.1f}**（{why}）")
    a("")
    a(
        "中期倾斜：上涨修复 +0.1~0.2；下跌中继 −0.1~0.2；投降段不追空。"
        "短期/盘中只决定买卖时点，不改基础暴露。"
    )
    a("")
    a("## 四、案例回放：为什么同形态有的见顶、有的继续涨")
    a("")
    a(_md(case_replay()))
    a("")
    a(
        "差异不在估值（四个案例都贵），而在**中期广度趋势**：ADL20 十日变化转负且"
        "站上 MA60 开始掉 → 顶部风险；ADL20 仍在改善 → 背离是噪声。"
        "但该复合条件全样本不显著（n=102，20 日 +0.14%，t 0.23），只能当案例解释，不能当规则。"
    )
    a("")
    a("## 五、结论")
    a("")
    a(
        f"- 长期：{'趋势向上' if above else '趋势向下'}，估值{'便宜' if cheap else '不便宜'} → "
        f"基础暴露 {lo:.1f}–{hi:.1f}。"
    )
    a(f"- 中期：{med['阶段']}；短期：{short['阶段']}；盘中：{intra['阶段']}。")
    a(
        f"- 顶风险：{'ADL背离（20日 −2.0%，t −2.18）' if adl_div else '无'}；"
        "HHI 前 10% 仅作 40 日风险提示（后半衰减）。"
    )
    a(f"- 底部信号：{bottom['阶段']}。")
    a(
        "- 操作：基础暴露 + 中期倾斜；短期只做执行。若中期广度修复"
        "（站上MA60 >30-40% 且 ADL20 转正）再加；若估值进入便宜侧"
        "（PB 便宜分位 >80%）+ 5 日投降 <25%，才按底部建仓。"
    )
    text = "\n".join(lines)
    (ROOT / "5_technical_derived" / "market_breadth" / "composite_judge.md").write_text(
        text, encoding="utf-8"
    )
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
