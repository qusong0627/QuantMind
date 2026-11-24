"""FF5 归因报告排版：Markdown/JSON 输出与判读文案。

从 ``ff_attribution.py`` 拆出；纯展示层，不做数据/回归计算。
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np
import pandas as pd

FACTOR_LABELS = {
    "MKT": "市场",
    "SMB": "市值",
    "HML": "价值(B/M)",
    "RMW": "盈利",
    "CMA": "投资",
    "TURN": "换手",
}


def verdict(ff5: dict, ff5t: dict) -> list[str]:
    rules = []
    a5, t5 = ff5["alpha_annual"], ff5["alpha_t"]
    rules.append(
        f"- FF5 α 年化 {a5:+.2%}（HAC t={t5:.2f}，n={ff5['n']}，R²={ff5['r2']:.2f}）"
        + (
            "：扣风格后仍有显著残余 α。"
            if a5 > 0 and t5 >= 2
            else "：未达显著（t<2 或 α≤0），不能认定选股能力。"
        )
    )
    at, tt = ff5t["alpha_annual"], ff5t["alpha_t"]
    if np.isfinite(at):
        if ff5["alpha_t"] >= 2 and tt < 1.5:
            rules.append(
                f"- 加入换手因子后 α 年化 {at:+.2%}（t={tt:.2f}）：{('α 塌陷，模型更像换手/流动性收割。')}"
            )
        else:
            rules.append(
                f"- 加入换手因子后 α 年化 {at:+.2%}（t={tt:.2f}）：换手未解释主要 α。"
            )
    smb = ff5["loadings"].get("SMB", {})
    hml = ff5["loadings"].get("HML", {})
    if smb:
        rules.append(
            f"- 市值载荷 β_SMB={smb['beta']:+.2f}（t={smb['t']:.1f}）；价值载荷 β_HML={hml['beta']:+.2f}（t={hml['t']:.1f}）——判断超额收益是否只是风格 beta。"
        )
    return rules


def write_report(
    out_dir: Path,
    model_id: str,
    args: argparse.Namespace,
    monthly: pd.Series,
    stats: dict,
    cohort_stats: dict,
    factors: pd.DataFrame,
    reg5: dict,
    reg6: dict,
    year_rows: list[dict],
    longshort: dict | None = None,
    ff_diag: dict | None = None,
    multi_n: list[dict] | None = None,
) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    md_path = out_dir / f"ff_attribution_{stamp}.md"
    json_path = out_dir / f"ff_attribution_{stamp}.json"

    def _fmt_pct(x: float) -> str:
        return "—" if not np.isfinite(x) else f"{x * 100:+.2f}%"

    def _fmt_wan(x: float | None) -> str:
        return "—" if x is None or not np.isfinite(x) else f"{x / 1e4:.0f} 万"

    lines = [
        f"# FF5 归因报告 — {model_id}",
        "",
        f"- 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 信号窗口：{args.start} ~ {args.end}（split={args.split}）",
        f"- 因子口径：sort={args.sort_mode}（monthly=逐月重排 / academic=6 月固定一年）、"
        f"Inv⟂OP={'是' if args.orth_inv else '否'}、rf={args.rf_annual:.0%}",
        f"- 组合：Top{args.top_n} 等权，T+1 开盘买入、持有 {args.horizon} 个交易日；双边成本 {args.cost_bps}bp；"
        + ("已剔除信号日 PIT ST" if not args.include_st else "未剔除 ST"),
        f"- 有效 cohort：{cohort_stats['cohorts']} 个（信号 {cohort_stats['signals']} 个；剔除开盘涨停 {cohort_stats['dropped_limit_up']}、低流动性 {cohort_stats.get('dropped_low_adv', 0)}、缺价 {cohort_stats['dropped_missing']}、不足3只 {cohort_stats['dropped_short']}；平均持仓 {cohort_stats['avg_names']:.1f} 只；跌停顺延 {cohort_stats['exit_extended']} 次）",
        f"- 流动性门槛：20 日日均成交额 ≥ {args.min_adv / 1e4:.0f} 万元；容量（等权、5%/10% 参与率）"
        f"中位 {_fmt_wan(cohort_stats.get('capacity_p5'))} / {_fmt_wan(cohort_stats.get('capacity_p10'))}"
        f"（5% 参与率 P10 下限 {_fmt_wan(cohort_stats.get('capacity_p5_p10'))}）",
        "",
        "## 组合表现（月度）",
        "",
        f"- 月数 {stats.get('months', 0)}；年化收益 {_fmt_pct(stats.get('ann_return', np.nan))}；年化波动 {stats.get('ann_vol', np.nan) * 100:.2f}%；Sharpe {stats.get('sharpe', np.nan):.2f}；最大回撤 {stats.get('max_drawdown', np.nan) * 100:.2f}%；月胜率 {stats.get('win_rate', np.nan):.0%}",
        "",
        "## FF5 回归（因变量：组合月超额收益；rf=0）",
        "",
        "| 因子 | 载荷 | HAC t |",
        "|---|---:|---:|",
        f"| α（月） | {reg5['alpha_monthly'] * 100:+.2f}% | {reg5['alpha_t']:.2f} |",
    ]
    for name, item in reg5["loadings"].items():
        lines.append(
            f"| {item['label']} {name} | {item['beta']:+.3f} | {item['t']:.2f} |"
        )
    lines.append("")
    lines.append(
        f"α 年化（复利）**{_fmt_pct(reg5['alpha_annual'])}**；R² {reg5['r2']:.2f}；n={reg5['n']} 月；HAC lag={reg5['hac_lags']}。"
    )
    if reg6["n"]:
        lines += [
            "",
            "## FF5 + 换手因子（CH-4 式）",
            "",
            "| 因子 | 载荷 | HAC t |",
            "|---|---:|---:|",
            f"| α（月） | {reg6['alpha_monthly'] * 100:+.2f}% | {reg6['alpha_t']:.2f} |",
        ]
        for name, item in reg6["loadings"].items():
            lines.append(
                f"| {item['label']} {name} | {item['beta']:+.3f} | {item['t']:.2f} |"
            )
        lines.append("")
        lines.append(
            f"α 年化（复利）**{_fmt_pct(reg6['alpha_annual'])}**；R² {reg6['r2']:.2f}；n={reg6['n']} 月。"
        )

    if multi_n and len(multi_n) > 1:
        lines += [
            "",
            "## TopN 长多对比（同一口径：T+1 开盘、持 N 日、扣双侧成本、剔 ST/低流动性/开盘涨停）",
            "",
            "| TopN | 月数 | 年化 | 波动 | Sharpe | 最大回撤 | FF5 α 年化 | α t | SMB(t) | R² |",
            "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
        for row in multi_n:
            p = row["perf"]
            lines.append(
                f"| {row['n']} | {p.get('months', 0)} | {p.get('ann_return', np.nan) * 100:+.1f}% | "
                f"{p.get('ann_vol', np.nan) * 100:.1f}% | {p.get('sharpe', np.nan):.2f} | "
                f"{p.get('max_drawdown', np.nan) * 100:.1f}% | {row['alpha5'] * 100:+.1f}% | "
                f"{row['t5']:.2f} | {row['smb']:+.2f} ({row['smb_t']:.1f}) | {row['r2']:.2f} |"
            )
        lines.append("")
        lines.append(
            "判读：TopN 越大越接近「市场+风格」；若 α 只在某 N 上为正且不随 N 单调，多半是噪声/口径差异。"
        )

    if longshort:
        ls_stats = longshort["stats"]
        ls5 = longshort["reg5"]
        ls6 = longshort["reg6"]
        lines += [
            "",
            f"## Top{args.top_n} − Bottom{longshort['bottom_n']} 多空（FF5）",
            "",
            "> ⚠️ 诊断口径：A 股无法做空，Bottom 组合多为高波动/低流动性标的，该多空收益**不可投资**；"
            "年化数字由重叠 cohort 逐日复利得出，会放大倍数，只用于判断模型排序能力。",
            "",
            f"- 月数 {ls_stats.get('months', 0)}；年化收益 {_fmt_pct(ls_stats.get('ann_return', np.nan))}；年化波动 {ls_stats.get('ann_vol', np.nan) * 100:.2f}%；Sharpe {ls_stats.get('sharpe', np.nan):.2f}；最大回撤 {ls_stats.get('max_drawdown', np.nan) * 100:.2f}%",
            "",
            "| 因子 | 载荷 | HAC t |",
            "|---|---:|---:|",
            f"| α（月） | {ls5['alpha_monthly'] * 100:+.2f}% | {ls5['alpha_t']:.2f} |",
        ]
        for name, item in ls5["loadings"].items():
            lines.append(
                f"| {item['label']} {name} | {item['beta']:+.3f} | {item['t']:.2f} |"
            )
        lines.append("")
        lines.append(
            f"多空 α 年化（复利）**{_fmt_pct(ls5['alpha_annual'])}**；R² {ls5['r2']:.2f}；n={ls5['n']} 月。"
        )
        if ls6.get("n"):
            lines.append(
                f"加换手因子后多空 α 年化 **{_fmt_pct(ls6['alpha_annual'])}**（t={ls6['alpha_t']:.2f}）。"
            )

    if year_rows:
        lines += [
            "",
            "## 分年 α（用全样本载荷，非逐年重估）",
            "",
            "| 年 | α 年化 | 正月份占比 | 月数 |",
            "|---|---:|---:|---:|",
        ]
        for row in year_rows:
            lines.append(
                f"| {row['year']} | {_fmt_pct(row['alpha_annual'])} | {row['positive_months']:.0%} | {row['months']} |"
            )

    if ff_diag and ff_diag.get("premium"):
        lines += [
            "",
            "## 因子模型自检（本窗口）",
            "",
            "| 因子 | 月均 | 年化 | t |",
            "|---|---:|---:|---:|",
        ]
        for name, stat in ff_diag["premium"].items():
            lines.append(
                f"| {FACTOR_LABELS.get(name, name)} {name} | {stat['mean_monthly'] * 100:+.2f}% | "
                f"{stat['ann'] * 100:+.1f}% | {stat['t']:.2f} |"
            )
        corr = ff_diag.get("corr") or {}
        cma_rmw = (corr.get("CMA") or {}).get("RMW")
        if cma_rmw is not None:
            lines.append("")
            lines.append(
                f"CMA/RMW 相关 {cma_rmw:+.2f}"
                + (
                    "（|corr|>0.5：共线明显，需看正交化后结果）"
                    if abs(cma_rmw) > 0.5
                    else "（共线不显著）"
                )
            )
        if ff_diag.get("spanning"):
            span = " · ".join(
                f"{FACTOR_LABELS.get(k, k)} α t={v['alpha_t']:.2f}"
                for k, v in ff_diag["spanning"].items()
            )
            lines.append(
                f"因子互相回归（spanning）α t：{span}（|t|>2 = 有独立信息或模型设定偏误）"
            )

    lines += ["", "## 判读", "", *verdict(reg5, reg6), ""]
    lines += [
        "规则参考：α>0 且 HAC t≥2 才算残余选股能力；加入换手后 α 塌陷（t<1.5）=流动性/换手收割；",
        "分年至少 2/3 年份 α 为正；载荷显著说明部分收益只是风格 beta。",
    ]
    md_path.write_text("\n".join(lines), encoding="utf-8")

    payload = {
        "model_id": model_id,
        "args": vars(args),
        "cohort_stats": cohort_stats,
        "perf": stats,
        "ff5": {k: v for k, v in reg5.items() if not k.startswith("_")},
        "ff5_turn": {k: v for k, v in reg6.items() if not k.startswith("_")},
        "yearly_alpha": year_rows,
        "long_short": {
            "bottom_n": longshort["bottom_n"],
            "stats": longshort["stats"],
            "ff5": {
                k: v for k, v in longshort["reg5"].items() if not k.startswith("_")
            },
            "ff5_turn": {
                k: v for k, v in longshort["reg6"].items() if not k.startswith("_")
            },
        }
        if longshort
        else None,
        "factors_window": [factors["month"].min(), factors["month"].max()],
        "factor_diagnostics": ff_diag,
        "multi_n": multi_n,
    }
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=float),
        encoding="utf-8",
    )
    monthly.to_csv(out_dir / f"portfolio_monthly_{stamp}.csv", header=["ret"])
    factors.to_csv(out_dir / f"ff_factors_{stamp}.csv", index=False)
    return md_path
