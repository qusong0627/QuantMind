#!/usr/bin/env python3
"""单因子机构级 PDF 报告导出。

数据源与页面**完全同源**：直接调 ``factor_report.service.compute_detail``，
不另写一套计算 —— 报告里出现第二个口径，读者就再也分不清哪个是真的。

产出落在 ``<报告档案根>/因子研究/``，档案根经 ``shared/report_archive`` 解析
（唯一实现，勿在本文件里再抄一份目录推断逻辑）。

中文 PDF 只能走 ``scripts/md_to_pdf_report``：它自带 CJK 字体注册。
前端 ``utils/pdfExport.ts``（jsPDF，仅 Helvetica）与
``qlib_app/services/report_generator.py`` 都**没有**中文字体，走错路中文变方框。

用法::

    python backend/scripts/export_factor_report.py --dataset alpha_library --factor a158_ROC20
    python backend/scripts/export_factor_report.py --dataset alpha_library --factor a158_ROC20 \\
        --long-group 1 --short-group 10 --cost-bps 30 --bench 000905.SH
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.shared.report_archive import archive_root  # noqa: E402

DASH = "—"

DISCLAIMER = (
    "本项目仅供学习研究与技术演示，不构成任何投资建议。本报告由 QuantMind 依据历史数据自动生成，"
    "可能存在错误或偏差，历史表现不代表未来收益。投资决策请咨询持有中国证监会颁发资质的专业机构。"
)


# ─────────────────────────── 格式化 ───────────────────────────


def pct(v: Any, d: int = 2) -> str:
    """小数 → 百分数文本；None/非有限 → 破折号（**绝不显示 0** —— 0 与缺数据必须可区分）。"""
    f = _f(v)
    if f is None:
        return DASH
    return f"{f * 100:+.{d}f}%"


def num(v: Any, d: int = 4) -> str:
    f = _f(v)
    return DASH if f is None else f"{f:+.{d}f}"


def raw(v: Any, d: int = 2) -> str:
    """不带正负号的数值 —— 波动率、标准差、跟踪误差这类量**没有方向**，加 `+` 是错的。"""
    f = _f(v)
    return DASH if f is None else f"{f:.{d}f}"


def raw_pct(v: Any, d: int = 2) -> str:
    f = _f(v)
    return DASH if f is None else f"{f * 100:.{d}f}%"


def ints(v: Any) -> str:
    f = _f(v)
    return DASH if f is None else f"{int(round(f))}"


def _f(v: Any) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


def table(header: list[str], rows: list[list[str]]) -> str:
    """Markdown 表格。空行集返回显式说明，不留一张空表。"""
    if not rows:
        return "_（无数据）_\n"
    out = ["| " + " | ".join(header) + " |", "|" + "|".join(["---"] * len(header)) + "|"]
    out += ["| " + " | ".join(r) + " |" for r in rows]
    return "\n".join(out) + "\n"


def get(block: Any, key: str, default: Any = None) -> Any:
    """块可能是降级块（只有 available/reason）—— 一律安全取值。"""
    return block.get(key, default) if isinstance(block, dict) else default


def ok(block: Any) -> bool:
    return isinstance(block, dict) and block.get("available") is not False


def reason_of(block: Any) -> str:
    return str(get(block, "reason") or "该项数据不可用")


# ─────────────────────────── 各章节 ───────────────────────────


def sec_overview(d: dict[str, Any], blocks: dict[str, Any]) -> str:
    h = blocks.get("headline")
    lg = int(get(h, "long_group", 3) or 3)
    sg = int(get(h, "short_group", 9) or 9)
    lines = ["## 一、概览\n"]

    if ok(h):
        lines.append(
            f"多空组合：**G{lg} 多 50% / G{sg} 空 50%**（美元中性、总杠杆 100%、每日再平衡、等权持有）。"
            f"G1 = 因子值最小 … G10 = 因子值最大，与 IC 符号无关。\n"
        )
        lines.append(table(
            ["指标", "值", "口径"],
            [
                ["Returns", pct(get(h, "returns"), 2), "多空组合年化收益 = 日均 × 252（简单年化，非 CAGR）"],
                ["IR", num(get(h, "ir"), 4), "日均收益 / 日收益标准差 × √252，无风险利率 = 0"],
                ["Turnover", pct(get(h, "turnover"), 2), "两腿日均单边换手均值，越低越省成本"],
                ["IC", num(get(h, "ic"), 5), "日频横截面 Spearman 秩相关均值"],
                ["ICIR", num(get(h, "icir"), 4), "IC 均值 / IC 标准差（**不年化**）"],
                ["Fitness", num(get(h, "fitness"), 4), "IR × √(|Returns| / max(Turnover, 0.125))"],
                ["Margin", num(get(h, "margin"), 4), "Returns / Turnover"],
                ["累计收益", pct(get(h, "cum_return"), 2), "毛口径"],
                ["年化波动", raw_pct(get(h, "ann_vol"), 2), "日收益标准差 × √252（恒非负）"],
                ["净 Returns", pct(get(h, "net_returns"), 2), f"扣 {raw(get(h, 'cost_bps'), 0)}bp 双边成本"],
                ["净 IR", num(get(h, "net_ir"), 4), "同口径扣费后"],
            ],
        ))
    else:
        lines.append(f"> 指标环不可用：{reason_of(h)}\n")

    q = d.get("quantile_mean") or []
    if q:
        rows = [[f"G{i + 1}", pct(v, 3)] for i, v in enumerate(q)]
        lines.append("### 分位平均前瞻收益\n")
        lines.append(table(["分组", "平均前瞻收益"], rows))

    cost = blocks.get("cost_block")
    lines.append("### 可实施性\n")
    if ok(cost):
        sens = get(cost, "sensitivity", {}) or {}
        rows = [
            [f"{raw(r.get('bps'), 0)}bp", num(r.get("net_ir"), 4), num(r.get("net_fitness"), 4)]
            for r in (sens.get("rows") or [])
        ]
        be = _f(sens.get("break_even_bps"))
        lines.append(f"**盈亏平衡成本**：{DASH if be is None else f'{be:.1f}bp'}"
                     "（净 IR 归零的 bps；低于你的真实交易成本时该因子不可交易）\n")
        lines.append(table(["双边成本", "净 IR", "净 Fitness"], rows))
        if sens.get("break_even_note"):
            lines.append(f"\n> {sens['break_even_note']}\n")
        cap = get(cost, "capacity")
        if cap:
            lines.append(
                "\n**容量估算**（简化模型，含显式假设，不是实测）：\n\n"
                + table(["项", "值"], [
                    ["估算可承载规模", f"{(_f(get(cap, 'est_aum')) or 0) / 1e8:.2f} 亿"],
                    ["参与率假设", pct(get(cap, "assumed_participation"), 0)],
                    ["持仓只数", ints(get(cap, "n_positions"))],
                    ["组合换手", pct(get(cap, "turnover"), 1)],
                    ["口径", str(get(cap, "median_amount_scope") or DASH)],
                    ["说明", str(get(cap, "note") or DASH)],
                ])
            )
    else:
        lines.append(f"> 成本敏感性不可用：{reason_of(cost)}\n")

    return "\n".join(lines) + "\n"


def sec_ic(blocks: dict[str, Any]) -> str:
    ic = blocks.get("ic_block")
    sig = blocks.get("significance")
    lines = ["## 二、IC\n"]
    if not ok(ic):
        return "\n".join(lines + [f"> IC 派生块不可用：{reason_of(ic)}\n"])

    decay = get(ic, "decay") or {}
    lines.append(table(["IC 统计量", "值", "说明"], [
        ["IC 均值", num(get(ic, "ic_mean"), 5), "全截面"],
        ["IC 标准差", raw(get(ic, "ic_std"), 5), "日 IC 离散度（恒非负）"],
        ["ICIR", num(get(blocks.get("headline"), "icir"), 4), "不年化"],
        ["IC 胜率", pct(get(ic, "win_rate"), 1), "IC > 0 的交易日占比"],
        ["Top 半 IC 均值", num(get(ic, "ic_top_mean"), 5), "按因子值中位切分的上半"],
        ["Bottom 半 IC 均值", num(get(ic, "ic_bot_mean"), 5), "下半；与 Top 半**异号**才是全截面单调"],
        ["中性化 IC", num(get(ic, "ic_neutral_mean"), 5),
         f"行业去均值 + 对市值正交 · {ints(get(ic, 'ic_neutral_days'))} 天"],
        ["IC 半衰期", f"{ints(get(ic, 'half_life_days'))} 个交易日", "IC 衰减到一半所需天数"],
        ["单调性", num(get(ic, "monotonicity"), 3), "分位序号与分位收益的秩相关"],
        ["覆盖率", pct(get(ic, "clip_frac_mean"), 2), "逐日去极值比例均值"],
        ["日均有效股票数", ints(get(ic, "n_valid_mean")), "参与 IC 计算的股票数"],
    ]))

    if decay:
        keys = sorted(decay, key=lambda x: int(x))
        lines.append("### IC 衰减\n")
        lines.append(table(["前瞻期", "IC 均值"], [[f"T+{k}", num(decay[k], 5)] for k in keys]))

    dom = get(ic, "ic_domain") or {}
    if any(_f(v) is not None for v in dom.values()):
        lines.append("### 分市值域 IC\n")
        lines.append(table(["域", "IC 均值", "含义"], [
            ["大盘", num(dom.get("large"), 5), "大资金可用性"],
            ["中盘", num(dom.get("mid"), 5), ""],
            ["小盘", num(dom.get("small"), 5), ""],
        ]))

    if ok(sig):
        boot = get(sig, "bootstrap_ic_mean") or {}
        lines.append("### 显著性检验\n")
        lines.append(
            "普通 t 与 **Newey-West t 并列** —— IC 存在自相关时普通 t 会严重高估显著性，"
            "两者差距越大越应以后者为准。\n"
        )
        lines.append(table(["检验", "值", "说明"], [
            ["普通 t 值", num(get(sig, "t_value"), 2), "ICIR × √n"],
            ["Newey-West t 值", num(get(sig, "nw_t_value"), 2), f"自相关修正，滞后 {ints(get(sig, 'nw_lag'))} 阶"],
            ["p 值", num(get(sig, "p_value"), 4), "双尾"],
            ["BHY q 值", num(get(sig, "q_value_bhy"), 4),
             f"全库 {ints(get(sig, 'n_factors_tested'))} 个因子多重检验校正后"],
            ["是否因自相关降显著", "是" if get(sig, "nw_shrunk") else "否", "NW t 绝对值 < 普通 t 的 80%"],
            ["Deflated Sharpe", num(get(sig, "deflated_sharpe"), 3), "多重检验下的夏普折减"],
            ["Bootstrap IC 95% CI",
             f"[{num(boot.get('lo'), 5)}, {num(boot.get('hi'), 5)}]", "不依赖正态假设"],
        ]))

    indep = get(ic, "independence") or {}
    peers = indep.get("peers") or []
    if peers:
        lines.append("### 独立性（是不是又一个复制品）\n")
        lines.append(f"与最相关的 {ints(indep.get('n_peers'))} 个同库因子："
                     f"最大 |ρ| = **{num(indep.get('max_corr'), 3)}**、"
                     f"平均 |ρ| = {num(indep.get('mean_corr_top'), 3)}\n")
        lines.append(table(["相关因子", "相关系数"], [[str(p.get("name")), num(p.get("corr"), 4)] for p in peers]))
        if indep.get("note"):
            lines.append(f"\n> {indep['note']}\n")

    rob = blocks.get("robust_block")
    if ok(rob):
        subs = get(rob, "sub_period") or []
        if subs:
            lines.append("### 稳健性分段\n")
            lines.append(table(["区间", "天数", "IC 均值", "ICIR"], [
                [f"{s.get('start', DASH)} → {s.get('end', DASH)}", ints(s.get("n")),
                 num(s.get("ic_mean"), 5), num(s.get("icir"), 3)]
                for s in subs
            ]))
        oos = get(rob, "oos") or {}
        lines.append("### 样本外衰减\n")
        lines.append(table(["项", "值"], [
            ["样本内 IC", num(oos.get("in_sample_ic"), 5)],
            ["样本外 IC", num(oos.get("out_sample_ic"), 5)],
            ["衰减", num(oos.get("decay"), 5)],
            ["拥挤度", num(get(get(rob, "crowding") or {}, "score"), 3)],
        ]))
        if oos.get("note"):
            lines.append(f"\n> {oos['note']}\n")

    return "\n".join(lines) + "\n"


def sec_group(blocks: dict[str, Any]) -> str:
    gb = blocks.get("group_block")
    lines = ["## 三、分组回测\n"]
    if not ok(gb):
        return "\n".join(lines + [f"> 分组回测块不可用：{reason_of(gb)}\n"])

    lg = int(get(gb, "long_group", 3) or 3)
    sg = int(get(gb, "short_group", 9) or 9)
    dist = get(gb, "ls_dist") or {}
    lines.append(
        f"多空日收益 ``ls_t = 0.5 × (G{lg} − G{sg})``，**毛收益**（不含成本）。"
        "「空头腿累计」按 ∏(1−r) 复利 —— 不是把多头累计取负。\n"
    )
    lines.append(table(["项", "值", "说明"], [
        ["多空累计收益", pct(_last(gb.get("ls_cum"), minus_one=True), 2), "毛口径"],
        ["最大回撤", pct(get(gb, "ls_dd"), 2), "累计净值相对历史高点（≤0）"],
        ["多头腿累计", pct(_last(gb.get("long_cum"), minus_one=True), 2), f"持有 G{lg}"],
        ["空头腿做空累计", pct(_short_book_end(gb.get("short_daily")), 2), f"做空 G{sg} · ∏(1−r) 复利"],
        ["日收益均值", pct(dist.get("mu"), 3), f"n = {ints(dist.get('n'))}"],
        ["日收益标准差", raw_pct(dist.get("sigma"), 2), "恒非负"],
        ["偏度", raw(dist.get("skew"), 2), ""],
        ["超额峰度", raw(dist.get("kurt"), 2), "正态 = 0；为正说明极端日更频繁"],
        ["VaR 95 / 99", f"{pct(dist.get('var_95'), 2)} / {pct(dist.get('var_99'), 2)}", "历史法"],
        ["CVaR 95 / 99", f"{pct(dist.get('cvar_95'), 2)} / {pct(dist.get('cvar_99'), 2)}", "尾部条件均值"],
        ["两腿合计换手", pct(get(gb, "turnover_ls"), 1), "G/G 两腿日均单边平均"],
    ]))

    gd = gb.get("group_daily_mean") or []
    gt = gb.get("group_turnover") or []
    if gd:
        lines.append("### 各组日均收益与换手\n")
        lines.append(table(["分组", "日均收益", "日均换手"], [
            [f"G{i + 1}", pct(gd[i], 3), pct(gt[i], 1) if i < len(gt) else DASH]
            for i in range(len(gd))
        ]))

    eps = gb.get("ls_dd_episodes") or []
    if eps:
        lines.append("### 前 5 大回撤区间\n")
        lines.append(table(["开始", "结束", "深度", "天数", "是否收复"], [
            [str(e.get("start")), str(e.get("end")), pct(e.get("dd"), 2),
             ints(e.get("days")), "是" if e.get("recovered") else "否"]
            for e in eps
        ]))

    trad = get(gb, "tradable") or {}
    lines.append("### 可交易口径 vs 理想口径\n")
    if trad.get("available"):
        lines.append(table(["项", "值"], [
            ["理想口径累计", pct(trad.get("ideal_cum_end"), 2)],
            ["可交易口径累计", pct(trad.get("tradable_cum_end"), 2)],
            ["差额", pct(trad.get("lost_return"), 2)],
            ["被挡天数", ints(trad.get("blocked_days"))],
            ["多头腿累计被挡只次", ints(trad.get("blocked_long_total"))],
            ["空头腿累计被挡只次", ints(trad.get("blocked_short_total"))],
        ]))
        lines.append(f"\n> {trad.get('note') or ''}\n")
    else:
        lines.append(f"> 可交易轨不可用：{reason_of(trad)}\n")

    sweep = gb.get("holding_sweep") or []
    if sweep:
        lines.append("### 持有期扫描\n")
        lines.append(
            "> ⚠️ 本表用的是 **Q10−Q1 极值价差**（构建期只存了这一种前瞻期序列），"
            f"与页面可配的 G{lg}/G{sg} **不是同一个组合**，仅用于横向比较哪个持有期更优。\n"
        )
        lines.append(table(["持有期", "毛收益", "净收益", "换手", "IR"], [
            [f"{ints(s.get('hold_days'))} 日", pct(s.get("gross_return"), 2),
             pct(s.get("net_return"), 2), pct(s.get("turnover"), 1), num(s.get("ir"), 3)]
            for s in sweep
        ]))

    return "\n".join(lines) + "\n"


def sec_excess(blocks: dict[str, Any]) -> str:
    ex = blocks.get("excess_block")
    lines = ["## 四、相对基准超额\n"]
    if not ok(ex):
        return "\n".join(lines + [f"> 超额块不可用：{reason_of(ex)}\n"])

    st = get(ex, "excess_stats") or {}
    lines.append(f"基准：**{get(ex, 'bench_name') or get(ex, 'bench_symbol') or DASH}**"
                 f"（{get(ex, 'bench_symbol') or DASH}）· 多头腿相对该指数\n")
    lines.append(table(["项", "值", "说明"], [
        ["年化超额", pct(get(ex, "excess_annual"), 2), "多头腿年化 − 基准年化"],
        ["跟踪误差", raw_pct(st.get("tracking_error"), 2), "超额收益年化标准差（恒非负）"],
        ["信息比率", num(st.get("information_ratio"), 3), "年化超额 / 跟踪误差"],
        ["Beta", num(st.get("beta"), 3), "相对基准"],
        ["相关性", num(st.get("corr"), 3), "与基准的日收益相关（**符号有意义**）"],
        ["CVaR 95 / 99", f"{pct(get(ex, 'cvar_95'), 2)} / {pct(get(ex, 'cvar_99'), 2)}", "超额收益尾部"],
        ["有效天数", ints(st.get("n_days")), ""],
    ]))

    annual = ex.get("annual") or []
    if annual:
        lines.append("### 分年度超额收益\n")
        lines.append(table(["年度", "天数", "多头收益", "基准收益", "超额", "多空"], [
            [str(a.get("year")), ints(a.get("n_days")), pct(a.get("long_ret"), 2),
             pct(a.get("bench_ret"), 2), pct(a.get("excess"), 2), pct(a.get("ls_ret"), 2)]
            for a in annual
        ]))

    others = [b for b in (ex.get("benchmarks") or []) if b.get("symbol") != ex.get("bench_symbol")]
    if others:
        lines.append("### 其他基准对照\n")
        lines.append(table(["基准", "代码", "可用", "有效天数", "年化超额"], [
            [str(b.get("name")), str(b.get("symbol")),
             "是" if b.get("available") else "否", ints(b.get("n_days")),
             pct(b.get("excess_annual"), 2)]
            for b in others
        ]))

    eps = ex.get("top_drawdowns") or []
    if eps:
        lines.append("### 超额回撤区间（前 5）\n")
        lines.append(table(["开始", "结束", "深度", "天数"], [
            [str(e.get("start")), str(e.get("end")), pct(e.get("dd"), 2), ints(e.get("days"))]
            for e in eps
        ]))

    return "\n".join(lines) + "\n"


def sec_style(blocks: dict[str, Any]) -> str:
    st = blocks.get("style_block")
    lines = ["## 五、风格相关性数值汇总\n"]
    if not ok(st):
        return "\n".join(lines + [f"> 风格块不可用：{reason_of(st)}\n"])

    lines.append("> 风格模型为**自算 CNE5 式口径**（风格描述子标准化后 WLS 求纯因子收益），"
                 "不声称与商业 Barra 数据完全可比。\n")

    exp = st.get("exposures") or []
    if exp:
        lines.append(f"### Barra 风格 · 观测期均值相关（{len(exp)} 项）\n")
        lines.append(table(["排名", "风格", "说明", "均值相关", "标准差", "有效天数"], [
            [ints(e.get("rank")), str(e.get("style")), str(e.get("label") or ""),
             num(e.get("mean_corr"), 4), raw(e.get("std_corr"), 4), ints(e.get("n_days"))]
            for e in exp
        ]))

    ec = st.get("excess_corr") or []
    if ec:
        lines.append("### 相对基准超额的风格相关性\n")
        lines.append(table(["风格", "相关系数", "有效天数"], [
            [str(e.get("style")), num(e.get("corr"), 4), ints(e.get("n_days"))] for e in ec
        ]))

    attr = st.get("attribution")
    if attr:
        lines.append("### 风格归因回归（超额是真本事还是风格 beta）\n")
        lines.append(
            f"回归 ``ls_daily = α + Σ β_k · f_k + ε``：**α = {num(attr.get('alpha'), 5)}**"
            f"（t = {num(attr.get('t_alpha'), 2)}），R² = {raw(attr.get('r_squared'), 3)}，"
            f"n = {ints(attr.get('n'))}\n"
        )
        betas = attr.get("betas") or []
        if betas:
            lines.append(table(["风格", "Beta", "t 值"], [
                [str(b.get("style")), num(b.get("beta"), 4), num(b.get("t"), 2)] for b in betas
            ]))
        if attr.get("note"):
            lines.append(f"\n> {attr['note']}\n")
    elif st.get("attribution_reason"):
        lines.append(f"> 风格归因不可用：{st['attribution_reason']}\n")

    return "\n".join(lines) + "\n"


def _last(seq: Any, *, minus_one: bool = False) -> float | None:
    """序列末值；``minus_one`` 用于净值曲线（cum_curve 返回 ∏(1+r)，不是收益）。"""
    if not isinstance(seq, (list, tuple)) or not seq:
        return None
    v = _f(seq[-1])
    return None if v is None else (v - 1.0 if minus_one else v)


def _short_book_end(short_daily: Any) -> float | None:
    """做空腿累计 = ∏(1−r) − 1（与「多头累计取负」不是一回事）。"""
    if not isinstance(short_daily, (list, tuple)) or not short_daily:
        return None
    nav = 1.0
    seen = False
    for r in short_daily:
        f = _f(r)
        if f is None:
            continue
        nav *= 1.0 - f
        seen = True
    return (nav - 1.0) if seen else None


# ─────────────────────────── 组装 ───────────────────────────


def build_markdown(d: dict[str, Any], *, dataset: str, factor: str, params: dict[str, Any]) -> str:
    blocks = d.get("blocks") if isinstance(d.get("blocks"), dict) else {}
    today = datetime.now().strftime("%Y-%m-%d")
    head = [
        f"# 因子研究报告 · {factor}",
        "",
        f"> 数据集：{dataset}　报告日期：{today}",
        f"> 数据截至：{d.get('end', DASH)}　样本：{d.get('n_dates', DASH)} 个交易日"
        f"　前瞻期：{params.get('horizon', 'fwd_ret_5')}",
        "",
        f"> 参数：G{params.get('long_group')} 多 / G{params.get('short_group')} 空　"
        f"双边成本 {params.get('cost_bps')}bp　基准 {params.get('bench') or '默认（沪深300）'}",
        "",
    ]
    if d.get("empty"):
        head.append(f"> ⚠️ 该因子在本数据集下无有效数据：{d.get('reason') or '未知原因'}\n")
        return "\n".join(head)

    body = [
        sec_overview(d, blocks),
        sec_ic(blocks),
        sec_group(blocks),
        sec_excess(blocks),
        sec_style(blocks),
    ]
    defs = blocks.get("definitions")
    tail = ["## 口径说明\n"]
    if isinstance(defs, dict) and defs:
        tail += [f"- **{k}**：{v}" for k, v in defs.items()]
    else:
        tail.append("_（后端未提供口径文案）_")
    tail += ["", "---", "", f"> {DISCLAIMER}", ""]
    return "\n".join(head) + "\n" + "\n".join(body) + "\n".join(tail)


def main() -> int:
    ap = argparse.ArgumentParser(description="单因子机构级报告导出（Markdown + PDF）")
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--factor", required=True)
    ap.add_argument("--horizon", default="fwd_ret_5")
    ap.add_argument("--lookback", type=int, default=0,
                    help="回看交易日数；0 = 全窗口（报告默认全窗口，页面默认 250）")
    ap.add_argument("--long-group", type=int, default=3)
    ap.add_argument("--short-group", type=int, default=9)
    ap.add_argument("--cost-bps", type=float, default=20.0)
    ap.add_argument("--bench", default=None)
    ap.add_argument("--out-dir", default=None)
    ap.add_argument("--no-pdf", action="store_true", help="只出 Markdown（排障用）")
    ap.add_argument("--json-out", default=None, help="额外落一份原始 detail JSON（对拍用）")
    args = ap.parse_args()

    from backend.services.engine.factor_report.service import compute_detail

    print(f"[1/3] 计算 {args.dataset} / {args.factor} …")
    detail = compute_detail(
        args.dataset, args.factor, args.horizon, args.lookback,
        long_group=args.long_group, short_group=args.short_group,
        cost_bps=args.cost_bps, bench=args.bench,
    )
    if detail.get("empty"):
        print(f"[warn] 明细为空：{detail.get('reason')}")

    params = {
        "horizon": args.horizon, "long_group": args.long_group,
        "short_group": args.short_group, "cost_bps": args.cost_bps, "bench": args.bench,
    }
    md = build_markdown(detail, dataset=args.dataset, factor=args.factor, params=params)

    out_dir = Path(args.out_dir) if args.out_dir else (archive_root() / "因子研究")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d")
    safe = args.factor.replace("/", "_")
    md_path = out_dir / f"因子报告_{safe}_{stamp}.md"
    md_path.write_text(md, encoding="utf-8")
    print(f"[2/3] Markdown: {md_path}")

    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(detail, ensure_ascii=False, default=str), encoding="utf-8"
        )
        print(f"      原始 JSON: {args.json_out}")

    if args.no_pdf:
        return 0

    # PDF 必须走 md_to_pdf_report（自带 CJK 字体注册）；它按**路径**读写
    pdf_path = out_dir / f"因子报告_{safe}_{stamp}.pdf"
    tmp_md: str | None = None
    try:
        from backend.scripts.md_to_pdf_report import main as md_to_pdf

        with tempfile.NamedTemporaryFile("w", suffix=".md", encoding="utf-8", delete=False) as tf:
            tf.write(md)
            tmp_md = tf.name
        md_to_pdf(tmp_md, str(pdf_path))
        print(f"[3/3] PDF: {pdf_path}（{pdf_path.stat().st_size / 1024:.0f} KB）")
    except Exception as e:  # noqa: BLE001
        print(f"[warn] PDF 生成失败（Markdown 已产出）：{e}")
        return 1
    finally:
        if tmp_md:
            Path(tmp_md).unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
