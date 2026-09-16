"""回测体检（T-P4-05a）：九项检验 → 四分类判定 → 报告（§6.2 形态）。

判定优先级（对齐设计《评估与打分体系》§六）：**E → L → B → A**
- E 证据不足：样本 < MinTRL（或无法回归——缺基准）；
- L 运气嫌疑：DSR < 0.95 / bootstrap CI 跨 0 / 剔 Top5 日后 alpha 消失；
- B beta 主导：alpha 不显著（t≤2）或 alpha 占收益 < 30%；
- A 真 alpha：以上全过。

用法（容器内）:
    python backend/scripts/eval/health_check.py --index 000001.SH --benchmark 000300.SH --days 250
    python backend/scripts/eval/health_check.py --nav-file /data/backtest/result.json --trials 240 --json
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.scripts.eval import stat_tools as st  # noqa: E402

VERDICT_LABELS = {
    "A": "✅ 真 alpha（收益来自可重复能力）",
    "B": "➖ Beta 主导（收益主要来自市场/风格暴露）",
    "L": "⚠ 运气嫌疑（统计上站不住）",
    "E": "❓ 证据不足（样本/数据不够下结论）",
}


def classify_backtest_health(
    tests: dict[str, Any], *, n_days: int, n_trials: int = 1
) -> dict[str, Any]:
    """九项检验结果 → 四分类判定 + 可信度分（0-100）+ 理由/建议（纯函数）。"""
    fr = tests.get("factor_regression") or {}
    dsr = tests.get("dsr") or {}
    boot = tests.get("bootstrap") or {}
    conc = tests.get("concentration") or {}
    regime = tests.get("regime") or {}
    mtrl = tests.get("min_trl") or {}

    reasons: list[str] = []
    suggestions: list[str] = []

    # ── E：证据不足（优先级最高）──
    insufficient = False
    if not fr.get("sufficient"):
        insufficient = True
        reasons.append(f"缺基准回归证据：{fr.get('reason', '未提供基准')}")
        suggestions.append("提供基准指数序列后重跑（alpha/beta 判定必需）")
    elif mtrl.get("sufficient") and mtrl.get("adequate") is False:
        insufficient = True
        reasons.append(
            f"样本不足：{mtrl.get('observed_years')} 年 < MinTRL {mtrl.get('min_trl_years')} 年"
        )
        suggestions.append(f"延长样本至 ≥{mtrl.get('min_trl_years')} 年再下结论")

    if insufficient:
        verdict = "E"
    else:
        luck_flags: list[str] = []
        if dsr.get("sufficient") and not dsr.get("passes_095"):
            luck_flags.append(
                f"DSR {dsr.get('dsr')} < 0.95（曾试 {dsr.get('n_trials')} 组参数，需更强证据）"
            )
            suggestions.append("减少参数扫描次数或延长样本（提升 DSR）")
        if boot.get("sufficient") and boot.get("return_ci_crosses_zero"):
            luck_flags.append(f"Bootstrap 收益 CI 跨 0：{boot.get('return_ci')}")
            suggestions.append("收益置信区间含 0：视为不可区分于噪声")
        if conc.get("sufficient") and conc.get("kills_alpha"):
            luck_flags.append(
                f"剔除最好 {conc.get('top_k')} 日后超额 {conc.get('full_total_return')} → "
                f"{conc.get('ex_top_total_return')}（收益集中在少数几天）"
            )
            suggestions.append("核对是否有事件依赖/单日暴利，考虑更稳健的样本外验证")

        alpha_sig = bool(fr.get("alpha_significant"))
        r2 = float(fr.get("r2") or 0.0)
        # alpha 占收益比：因子回归 alpha 年化 / 实际年化（由 returns 均值提供）
        actual_annual = tests.get("_actual_annual")
        alpha_share = None
        if actual_annual and actual_annual > 0 and fr.get("alpha_annual") is not None:
            alpha_share = float(fr["alpha_annual"]) / float(actual_annual)

        if not alpha_sig:
            # 语义优先级：alpha 不显著且收益可由市场解释（R² 高）→ B（beta 主导）；
            # 否则才谈"运气"（无暴露解释的弱显著性 = 统计噪声嫌疑）
            if r2 > 0.5:
                verdict = "B"
                reasons.append(
                    f"alpha 不显著（年化 {fr.get('alpha_annual')}，t={fr.get('alpha_t')}）"
                    f"且 R²={r2:.2f}：收益可由基准解释（beta 主导）"
                )
                suggestions.append("收益来源以 beta 为主：关注对冲后超额或风格暴露管理")
            elif luck_flags:
                verdict = "L"
                reasons.extend(luck_flags)
            else:
                verdict = "B"
                reasons.append(
                    f"alpha 不显著（年化 {fr.get('alpha_annual')}，t={fr.get('alpha_t')}），"
                    f"无显著超额能力"
                )
        elif luck_flags:
            verdict = "L"
            reasons.extend(luck_flags)
        elif alpha_share is not None and alpha_share < 0.30:
            verdict = "B"
            reasons.append(
                f"alpha 占收益 {alpha_share:.0%} < 30%（收益主要来自市场暴露）"
            )
            suggestions.append("收益来源以 beta 为主：关注对冲后超额或风格暴露管理")
        else:
            verdict = "A"
            reasons.append(
                f"alpha 显著（年化 {fr.get('alpha_annual')}，t={fr.get('alpha_t')}），"
                f"DSR {dsr.get('dsr')} 通过，跨样本稳健"
            )

    # ── 可信度分（30/25/15/15/15 分量）──
    sig_score = (
        30
        if (fr.get("sufficient") and fr.get("alpha_significant"))
        else (12 if fr.get("sufficient") else 0)
    )
    dsr_score = (
        25
        if (dsr.get("sufficient") and dsr.get("passes_095"))
        else (8 if dsr.get("sufficient") else 0)
    )
    conc_score = (
        15
        if (conc.get("sufficient") and not conc.get("kills_alpha"))
        else (4 if conc.get("sufficient") else 0)
    )
    regime_score = (
        15
        if (
            regime.get("sufficient")
            and regime.get("all_alive")
            and len(regime.get("regimes_covered") or []) >= 2
        )
        else (8 if regime.get("sufficient") and regime.get("all_alive") else 3)
    )
    sample_score = 15 if (mtrl.get("sufficient") and mtrl.get("adequate")) else 4
    confidence = int(
        min(100, sig_score + dsr_score + conc_score + regime_score + sample_score)
    )

    return {
        "verdict": verdict,
        "verdict_label": VERDICT_LABELS[verdict],
        "confidence": confidence,
        "reasons": reasons,
        "suggestions": suggestions,
        "n_days": int(n_days),
        "n_trials": int(n_trials),
    }


def run_health_check(
    returns: Any,
    *,
    benchmark_returns: Any | None = None,
    index_closes: Any | None = None,
    daily_turnover: Any | None = None,
    performance_matrix: Any | None = None,
    style_factors: dict[str, Any] | None = None,
    n_trials: int = 1,
) -> dict[str, Any]:
    """九项检验全量执行 + 判定（纯编排）。"""
    r = st._clean(returns)
    tests: dict[str, Any] = {
        "psr": st.psr(r),
        "dsr": st.deflated_sharpe_ratio(r, n_trials=n_trials),
        "min_trl": st.min_track_record_length(r),
        "bootstrap": st.block_bootstrap(r),
        "concentration": st.return_concentration(r),
        "_actual_annual": round(float(np.mean(r)) * st.TRADING_DAYS, 6)
        if len(r)
        else None,
    }
    if benchmark_returns is not None:
        tests["factor_regression"] = st.factor_regression(
            r, benchmark_returns, style_factors
        )
    else:
        tests["factor_regression"] = {"sufficient": False, "reason": "未提供基准序列"}
    if index_closes is not None:
        tests["regime"] = st.regime_split(r, index_closes)
    else:
        tests["regime"] = {"sufficient": False, "reason": "未提供指数序列"}
    if daily_turnover is not None:
        tests["cost"] = st.cost_sensitivity(r, daily_turnover)
    else:
        tests["cost"] = {"sufficient": False, "reason": "未提供换手序列"}
    if performance_matrix is not None:
        tests["pbo"] = st.pbo_cscv(performance_matrix)
    else:
        tests["pbo"] = {"sufficient": False, "reason": "未提供参数扫描矩阵（有则必跑）"}

    verdict = classify_backtest_health(tests, n_days=len(r), n_trials=n_trials)
    return {"tests": tests, **verdict}


def render_report(result: dict[str, Any]) -> str:
    """§6.2 形态文本报告（简单模式：标签 + 关键句 + 建议）。"""
    t = result["tests"]
    lines = [
        f"结论标签：{result['verdict_label']}          可信度分：{result['confidence']}/100",
        "─" * 46,
    ]
    fr = t.get("factor_regression") or {}
    if fr.get("sufficient"):
        lines.append(
            f"alpha 回归: 年化 {fr.get('alpha_annual'):.2%}, t={fr.get('alpha_t')} "
            f"{'✓ 显著' if fr.get('alpha_significant') else '✗ 不显著'}; "
            f"R²={fr.get('r2')}, beta={fr.get('beta')}"
        )
    dsr = t.get("dsr") or {}
    if dsr.get("sufficient"):
        mark = "✓" if dsr.get("passes_095") else "✗"
        lines.append(
            f"DSR: {dsr.get('dsr')} {mark} 0.95（试验 N={dsr.get('n_trials')}）"
        )
    psr = t.get("psr") or {}
    if psr.get("sufficient"):
        lines.append(f"PSR: {psr.get('psr')}（Sharpe>0 概率，偏度/峰度校正）")
    mtrl = t.get("min_trl") or {}
    if mtrl.get("sufficient"):
        mark = "✓" if mtrl.get("adequate") else "✗"
        lines.append(
            f"样本: {mtrl.get('observed_years')} 年 vs MinTRL {mtrl.get('min_trl_years')} 年 {mark}"
        )
    boot = t.get("bootstrap") or {}
    if boot.get("sufficient"):
        lines.append(
            f"Bootstrap: 年化收益 CI {boot.get('return_ci')}（{'跨0 ⚠' if boot.get('return_ci_crosses_zero') else '不跨0'}）"
        )
    conc = t.get("concentration") or {}
    if conc.get("sufficient"):
        mark = "⚠ 集中" if conc.get("kills_alpha") else "✓ 分散"
        lines.append(
            f"集中度: 剔 Top{conc.get('top_k')} 日 {conc.get('full_total_return')} → "
            f"{conc.get('ex_top_total_return')} {mark}"
        )
    regime = t.get("regime") or {}
    if regime.get("sufficient"):
        parts = [
            f"{k}:n={v['n']}/cum={v['cum_return']}"
            for k, v in (regime.get("regimes") or {}).items()
            if v.get("n")
        ]
        lines.append(
            f"regime: {'；'.join(parts)}（覆盖 {regime.get('regimes_covered')}）"
        )
    cost = t.get("cost") or {}
    if cost.get("sufficient"):
        lines.append(
            f"成本敏感性: 上浮 {cost.get('uplift_bps')}bps → 年化 {cost.get('adjusted_annual')}"
            f"（{'✓ 仍正' if cost.get('still_positive_after_cost') else '✗ 转负'}）"
        )
    pbo = t.get("pbo") or {}
    if pbo.get("sufficient"):
        lines.append(
            f"PBO: {pbo.get('pbo')}（{pbo.get('overfit_risk')} 过拟合风险，{pbo.get('n_params')} 组）"
        )
    lines.append("─" * 46)
    for reason in result["reasons"]:
        lines.append(f"· {reason}")
    for i, sug in enumerate(result["suggestions"][:3], 1):
        lines.append(f"建议 {i}: {sug}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# IO：真库序列装载（CLI 用）
# ---------------------------------------------------------------------------


def load_index_closes(symbol: str, days: int = 250) -> tuple[list[str], np.ndarray]:
    """指数收盘序列（QuantDB index_daily，升序）→ (dates, closes)。"""
    from datetime import date, timedelta

    from backend.services.engine.data_platform.quantdb_hub import QuantDBDataHub

    hub = QuantDBDataHub.get_instance()
    end = date.today()
    start = end - timedelta(days=int(days * 1.8) + 30)
    df = hub.fetch_series(
        "qdb_index_daily",
        symbol,
        int(start.strftime("%Y%m%d")),
        int(end.strftime("%Y%m%d")),
        columns=["close"],
    )
    if df is None or len(df) == 0:
        return [], np.empty(0)
    df = df.sort_values("dt").tail(days)
    return [str(int(d)) for d in df["dt"]], df["close"].astype(float).to_numpy()


def nav_curve_to_returns(nav: list[float]) -> np.ndarray:
    arr = np.asarray([float(x) for x in nav if x is not None], dtype=float)
    if len(arr) < 2:
        return np.empty(0)
    prev = arr[:-1]
    with np.errstate(divide="ignore", invalid="ignore"):
        rets = arr[1:] / prev - 1.0
    return rets[np.isfinite(rets)]


def parse_nav_file(path: str) -> np.ndarray:
    """支持 backtest result JSON（nav_curve/净值数组）或纯数字 JSON 数组。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict):
        curve = data.get("nav_curve") or data.get("nav") or data.get("equity_curve")
        if isinstance(curve, list) and curve and isinstance(curve[0], dict):
            nav = [row.get("nav", row.get("value")) for row in curve]
        else:
            nav = curve or []
    else:
        nav = data
    return nav_curve_to_returns(nav or [])


def main() -> int:
    parser = argparse.ArgumentParser(description="回测体检（九项检验 → 四分类）")
    parser.add_argument(
        "--index", default=None, help="策略曲线来源：指数代码（sanity 自检用）"
    )
    parser.add_argument("--nav-file", default=None, help="策略净值来源：回测结果 JSON")
    parser.add_argument(
        "--benchmark", default="000300.SH", help="基准指数（默认沪深300）"
    )
    parser.add_argument(
        "--regime-index", default="000300.SH", help="regime 划分指数（默认沪深300）"
    )
    parser.add_argument("--days", type=int, default=250)
    parser.add_argument(
        "--trials", type=int, default=1, help="参数扫描试验次数 N（DSR 去胀）"
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if args.nav_file:
        strategy_returns = parse_nav_file(args.nav_file)
    elif args.index:
        _d, closes = load_index_closes(args.index, args.days)
        strategy_returns = nav_curve_to_returns(list(closes))
    else:
        # 缺省 sanity：上证指数当"策略"跑体检
        _d, closes = load_index_closes("000001.SH", args.days)
        strategy_returns = nav_curve_to_returns(list(closes))

    if len(strategy_returns) < 30:
        print("策略序列不足 30 期，无法体检", file=sys.stderr)
        return 2

    _bd, bench_closes = load_index_closes(args.benchmark, args.days)
    bench_returns = nav_curve_to_returns(list(bench_closes))
    _rd, regime_closes = load_index_closes(args.regime_index, args.days + 90)

    result = run_health_check(
        strategy_returns,
        benchmark_returns=bench_returns if len(bench_returns) else None,
        index_closes=regime_closes if len(regime_closes) else None,
        n_trials=args.trials,
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        print(render_report(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
