"""影子代价账·报表（纯函数，无 I/O）——回答「这条闸门该不该留」（P1.6）。

读法（先把这段读三遍再看得数）
------------------------------
每条规则的账按**臂**（arm）分开算，两臂不可混：

``shadow``   ``enforced=false``：判定照跑、**单没被拦**。这一臂的代价是
             **反事实**——「假如当初翻了闸会怎样」。它是回答「该不该启用」的那一支。
``enforced`` ``enforced=true``：单真被拦了。这一臂是**已实现**代价——
             「拦下来之后实际损失了多少」。

同一个公式在两臂上含义不同（一个假设、一个既成事实），**把两臂并进一个均值
就是把假设和事实相加**。故本模块按 (规则, 期, 臂) 出行，判定也只依据本臂样本；
报告每行都打出臂名，不让读者有机会误读。

**在哪一臂上判定**：同一 (规则, 期) 两臂都有样本时取 ``shadow``（决策相关的
是「要不要启用」）；只有 ``enforced`` 时取其（闸已翻，反事实不再新增）。

样本不足就直说
--------------
``n < REVIEW_SAMPLE_MIN``（30，与登记表同口径）时判定为「样本不足」，**不给方向**。
少样本的均值等于掷硬币，而规则一旦被这条结论删掉，删掉的理由会永远留在台账里。

不可计价的样本单列
------------------
未到期 / 一字板买不到 / 缺 bar / 重试成功一律不进 n，但**按状态计数打出来**——
「有 316 条样本、其中 200 条还没到期」和「只有 116 条样本」是两件事，
只有把分母拆开才看得出来。
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from backend.shared.risk.gate_registry import (
    REVIEW_ACTIONS,
    REVIEW_SAMPLE_MIN,
    GateSpec,
    all_gates,
    gate_spec,
    review_overdue,
)
from backend.shared.risk.ghost import GhostRow
from backend.shared.risk.ghost_pricing import (
    HORIZONS,
    H_NOT_MATURED,
    H_NO_DATA,
    H_RETRIED,
    H_UNTRADABLE,
    costs_by_horizon,
    state_of,
)

#: 两臂名（见模块头）
ARM_SHADOW = "shadow"
ARM_ENFORCED = "enforced"
ARMS: tuple[str, ...] = (ARM_SHADOW, ARM_ENFORCED)

#: 未定价（没走过定价器）的行的归位标签——与「未到期」等状态并列展示
ST_UNPRICED = "未定价"

#: 判定词表（报告侧唯一口径；四选一，绝不出现第五种说法）
V_INSUFFICIENT = "样本不足"
V_COSTLY = "净花钱"
V_SAVING = "净省钱"
V_FLAT = "不显著"
VERDICTS: tuple[str, ...] = (V_INSUFFICIENT, V_COSTLY, V_SAVING, V_FLAT)

#: t 值多大才肯给方向（双侧 5%，正态近似）。**只是提示**：代价序列有厚尾与
#: 自相关（同一天的多个信号共享同一个市场收益），不是独立同分布，故 t 仅作参考，
#: 报告里不把它写成「显著」，也绝不用它单独推翻符号。
T_CRIT = 1.96

#: 报告里按状态单列的不可计价档位（顺序即展示序）
UNPRICED_STATES: tuple[str, ...] = (
    H_NOT_MATURED,
    H_UNTRADABLE,
    H_NO_DATA,
    H_RETRIED,
    ST_UNPRICED,
)


def arm_of(row: GhostRow) -> str:
    """行归哪一臂（见模块头）。"""
    return ARM_ENFORCED if row.enforced else ARM_SHADOW


@dataclass(frozen=True)
class RuleStats:
    """(规则 × 期 × 臂) 的账。"""

    rule_id: str
    kind: str
    registered: bool
    horizon: int
    arm: str
    n_counted: int
    unpriced: Mapping[str, int]
    mean_cost: float | None
    median_cost: float | None
    win_rate: float | None
    t_stat: float | None
    total_cost: float | None
    first_date: str | None
    last_date: str | None
    verdict: str

    @property
    def n_unpriced(self) -> int:
        return sum(self.unpriced.values())

    @property
    def n_total(self) -> int:
        return self.n_counted + self.n_unpriced


def _verdict(n: int, mean: float | None, t: float | None) -> str:
    """四选一（见模块头「样本不足就直说」）。"""
    if n < REVIEW_SAMPLE_MIN or mean is None:
        return V_INSUFFICIENT
    if mean > 0 and (t is None or t >= T_CRIT):
        return V_COSTLY
    if mean < 0 and (t is None or t <= -T_CRIT):
        return V_SAVING
    return V_FLAT


def _one_stats(
    rule_id: str,
    kind: str,
    registered: bool,
    horizon: int,
    arm: str,
    rows: Sequence[GhostRow],
) -> RuleStats:
    costs: list[float] = []
    unpriced: dict[str, int] = {}
    dates: list[str] = []
    for r in rows:
        dates.append(r.date)
        c = costs_by_horizon(r, horizon)
        if c is None:
            st = state_of(r, horizon) or ST_UNPRICED
            unpriced[st] = unpriced.get(st, 0) + 1
            continue
        costs.append(c)

    n = len(costs)
    mean = sum(costs) / n if n else None
    t: float | None = None
    if n >= 3 and mean is not None:
        sd = statistics.stdev(costs)
        if sd > 0:
            t = mean / (sd / math.sqrt(n))
    return RuleStats(
        rule_id=rule_id,
        kind=kind,
        registered=registered,
        horizon=horizon,
        arm=arm,
        n_counted=n,
        unpriced=unpriced,
        mean_cost=mean,
        median_cost=statistics.median(costs) if n else None,
        win_rate=(sum(1 for c in costs if c > 0) / n) if n else None,
        t_stat=t,
        total_cost=sum(costs) if n else None,
        first_date=min(dates) if dates else None,
        last_date=max(dates) if dates else None,
        verdict=_verdict(n, mean, t),
    )


def stats_by_rule(
    rows: Sequence[GhostRow],
    *,
    horizons: Iterable[int] = HORIZONS,
) -> tuple[RuleStats, ...]:
    """按 (规则, 期, 臂) 汇总（只含至少有一行的组合）。

    ``structural`` 类规则（整手校验等）同样出表：放行也不会成交、代价恒为 0，
    它们该回答的是「能不能不发生」而不是代价问题——**打出来让人看见**，
    比在报表里悄悄过滤掉更诚实（过滤掉的话，读报表的人会以为它们不存在）。
    """
    hs = tuple(int(h) for h in horizons)
    buckets: dict[tuple[str, int, str], list[GhostRow]] = {}
    for r in rows:
        for h in hs:
            buckets.setdefault((r.rule_id, h, arm_of(r)), []).append(r)

    out: list[RuleStats] = []
    for (rule_id, h, arm), group in buckets.items():
        spec = gate_spec(rule_id)
        out.append(
            _one_stats(
                rule_id=rule_id,
                kind=spec.kind if spec else "veto",
                registered=spec is not None,
                horizon=h,
                arm=arm,
                rows=group,
            )
        )
    return tuple(sorted(out, key=lambda s: (s.rule_id, s.horizon, s.arm)))


def verdict_rows(stats: Sequence[RuleStats]) -> tuple[RuleStats, ...]:
    """每个 (规则, 期) **用来下结论**的那一行（见模块头：两臂都有 → 取 shadow）。"""
    best: dict[tuple[str, int], RuleStats] = {}
    for s in stats:
        key = (s.rule_id, s.horizon)
        cur = best.get(key)
        if cur is None or (cur.arm == ARM_ENFORCED and s.arm == ARM_SHADOW):
            best[key] = s
    return tuple(best[k] for k in sorted(best))


@dataclass(frozen=True)
class Report:
    """一份可渲染的影子账报表（纯数据；`lines()` 出人读文本）。"""

    stats: tuple[RuleStats, ...]
    verdicts: tuple[RuleStats, ...]
    n_rows: int
    n_shadow: int
    n_enforced: int
    versions: tuple[int, ...]
    window: tuple[str, str] | None
    horizons: tuple[int, ...]
    as_of: str
    silent_rules: tuple[GateSpec, ...]
    unknown_rules: tuple[str, ...]
    overdue: tuple[GateSpec, ...]
    shadow_mode: bool | None

    def lines(self) -> list[str]:
        return render(self)


def _fmt(v: float | None, *, nd: int = 2) -> str:
    """数值渲染：**None 一律 `—`**（不可得绝不写成 0）。数字按 pp 显示。"""
    if v is None:
        return "—"
    return f"{v * 100:.{nd}f}%"


def build_report(
    rows: Sequence[GhostRow],
    *,
    as_of: str,
    horizons: Iterable[int] = HORIZONS,
    shadow_mode: bool | None = None,
    gates: Sequence[GateSpec] | None = None,
) -> Report:
    """把行汇总成报表（``as_of`` 由调用方注入，本模块不读时钟）。

    ``shadow_mode``：风控配置里 ``shadow`` 的当前值（None = 没读到）。它决定
    「当日成交」是不是污染，**必须显式打出来**——读报表的人靠它判断这些反事实
    问句还要不要继续攒（见 `ghost_pricing` 模块头）。
    """
    hs = tuple(int(h) for h in horizons)
    stats = stats_by_rule(rows, horizons=hs)
    days = sorted({r.date for r in rows if r.date})
    gate_list = tuple(gates) if gates is not None else all_gates()
    registered = {g.rule_id for g in gate_list}
    seen_rules = {r.rule_id for r in rows}
    try:
        overdue = tuple(review_overdue(as_of, gate_list))
    except ValueError:
        overdue = ()
    return Report(
        stats=stats,
        verdicts=verdict_rows(stats),
        n_rows=len(rows),
        n_shadow=sum(1 for r in rows if not r.enforced),
        n_enforced=sum(1 for r in rows if r.enforced),
        versions=tuple(sorted({int(r.version or 0) for r in rows})),
        window=(days[0], days[-1]) if days else None,
        horizons=hs,
        as_of=str(as_of),
        silent_rules=tuple(g for g in gate_list if g.rule_id not in seen_rules),
        unknown_rules=tuple(sorted(seen_rules - registered)),
        overdue=overdue,
        shadow_mode=shadow_mode,
    )


def _arm_banner(shadow_mode: bool | None) -> str:
    """整份报表的免责声明，不能省：影子期与翻闸后的代价不是同一个东西。"""
    if shadow_mode is None:
        return "  ⚠ shadow 配置未读到：无法判断这些是反事实还是已实现代价"
    if shadow_mode:
        return (
            "  ⚠ 当前 shadow=true（判定照跑、未拦单）：影子臂的代价是【反事实】"
            "（「假如当初翻闸」），当日成交是实现分支、不是污染"
        )
    return (
        "  ⚠ 当前 shadow=false（闸已翻）：影子臂样本已不再新增，"
        "判定依据应转向已拦臂；拦截后 ≤300s 内又被成交的样本已按重试剔除"
    )


def render(rep: Report) -> list[str]:
    """人读渲染（CLI 直接打印；测试断言它在关键结论上不退化成空话）。"""
    out: list[str] = []
    win = f"{rep.window[0]} ~ {rep.window[1]}" if rep.window else "（无行）"
    out.append("=" * 78)
    out.append("风控影子代价账（P1.6）")
    out.append(f"  窗口: {win}    出表日: {rep.as_of}")
    out.append(
        f"  行数: {rep.n_rows}（影子臂 {rep.n_shadow} / 已拦臂 {rep.n_enforced}）"
        f"    口径版本: {list(rep.versions) or '—'}"
    )
    out.append(_arm_banner(rep.shadow_mode))
    out.append("=" * 78)

    if not rep.verdicts:
        out.append("")
        out.append("  窗口内没有任何拦截样本——本报告还不能支持任何取舍决定。")

    for h in rep.horizons:
        block = [s for s in rep.verdicts if s.horizon == h]
        if not block:
            continue
        out.append("")
        out.append(f"── t{h}（入场日算第 1 个交易日）" + "─" * 40)
        out.append(
            f"{'规则':26} {'臂':9} {'n':>5} {'均值':>9} {'中位':>9} {'胜率':>7} {'t':>7}  结论"
        )
        for s in sorted(block, key=lambda x: x.rule_id):
            t = "—" if s.t_stat is None else f"{s.t_stat:.2f}"
            flag = "" if s.registered else "  [未登记]"
            out.append(
                f"{s.rule_id[:26]:26} {s.arm:9} {s.n_counted:>5} "
                f"{_fmt(s.mean_cost):>9} {_fmt(s.median_cost):>9} "
                f"{_fmt(s.win_rate):>7} {t:>7}  {s.verdict}{flag}"
            )
        # 分母拆开：让「样本不够」与「样本没到期」分得清
        unpriced = [s for s in rep.stats if s.horizon == h and s.n_unpriced]
        if unpriced:
            out.append("  未计价（不进 n）:")
            for s in sorted(unpriced, key=lambda x: (x.rule_id, x.arm)):
                parts = ", ".join(f"{k}={v}" for k, v in sorted(s.unpriced.items()))
                out.append(f"    {s.rule_id[:32]:32} {s.arm:9} {parts}")

    out.append("")
    out.append("── 未触发 / 未登记" + "─" * 55)
    if rep.silent_rules:
        out.append(
            f"  登记在册、窗口内【一次都没触发】（{len(rep.silent_rules)} 条）——"
            "不是「没有代价」，是「没有证据」：先确认生产者还在"
        )
        for g in rep.silent_rules:
            out.append(f"    {g.rule_id:32} {g.title}")
    else:
        out.append("  登记在册的规则窗口内都触发过")
    if rep.unknown_rules:
        out.append(f"  ⚠ 留痕里出现了未登记的规则 id：{', '.join(rep.unknown_rules)}")
        out.append("    未登记 ≠ 没问题：它没有被审阅过，请补进 gate_registry")

    out.append("")
    out.append("── 到期复核（review_by ≤ 出表日）" + "─" * 42)
    if rep.overdue:
        for g in rep.overdue:
            out.append(f"    {g.review_by}  {g.rule_id:32} {g.title}")
        out.append(f"  可选动作：{' / '.join(REVIEW_ACTIONS)}")
    else:
        out.append("  无到期条目")

    costly = [s for s in rep.verdicts if s.verdict == V_COSTLY]
    saving = [s for s in rep.verdicts if s.verdict == V_SAVING]
    thin = [s for s in rep.verdicts if s.verdict == V_INSUFFICIENT]
    out.append("")
    out.append("── 一句话结论" + "─" * 62)
    if not rep.verdicts:
        out.append("  窗口内没有可计价的拦截样本，本报告还不能支持任何取舍决定。")
    else:
        out.append(
            f"  可下结论的规则：净花钱 {len(costly)} 条、净省钱 {len(saving)} 条、"
            f"样本不足 {len(thin)} 条（门槛 n≥{REVIEW_SAMPLE_MIN}）。"
        )
        if costly:
            names = ", ".join(f"{s.rule_id}(t{s.horizon})" for s in costly[:5])
            out.append(f"  → 优先复核净花钱的：{names}")
    if rep.shadow_mode:
        out.append("  注意：以上均为影子期反事实代价；闸门尚未生效，翻闸后需重新出表。")
    return out


__all__ = [
    "ARMS",
    "ARM_ENFORCED",
    "ARM_SHADOW",
    "ST_UNPRICED",
    "T_CRIT",
    "UNPRICED_STATES",
    "VERDICTS",
    "V_COSTLY",
    "V_FLAT",
    "V_INSUFFICIENT",
    "V_SAVING",
    "Report",
    "RuleStats",
    "arm_of",
    "build_report",
    "render",
    "stats_by_rule",
    "verdict_rows",
]
