"""P5 步骤 3：隔壁 ``data/live_watch.json`` → 本仓止盈止损规则表（**纯层，不写任何东西**）。

要解的题
--------
隔壁的守护计划是 ``{agent: [规则]}`` 的一张表，**归属由结构给出**；本仓执行器的规则表
是一张平表，归属是 ``owner`` 字段（``""`` = 人工、``"llm:<agent>"`` = 决策层某组，见
``sltp_executor.OWNER_*``）。切换只有一次窗口：规则没挂上 = 当日**持仓裸奔**（计划里
的 CRITICAL）。所以迁移不能是「人照着文件在控制面点一遍」——那一步的失效形态是
「看起来搬完了」，而它没有任何信号。

本模块只做三件事，其余一律复用既有实现：

1. **读**（:func:`parse_legacy_watch`）：容错地读那张表，逐条转成决策层的
   :class:`~backend.shared.decision.contract.Decision`——用的是**生产解析器**的字段口径
   （``parse_pct`` 三态 / ``parse_level`` 价位），不是这里另写一份；
2. **映射**（:func:`plan_watch` 复用）：每条 watch 决策 → 待挂规则，含比例三态、
   去重、组合校验（棘轮成对、``reduce_pct`` 越界……）；
3. **预演**（:func:`simulate_apply`）：**不写任何东西**地算出「这批规则挂上去，逐家
   会发生什么」——用的是 ``watch_writer.resolve_plan`` **同一份**判定，所以预演与
   实际落库不会给出两个答案。

为什么不是「每条规则直接转成一条执行器规则」
--------------------------------------------
隔壁文件里的每一条都已经是**它那边算过的结果**（比例缺失已按全仓落定、``move_stop``
上移过的止损已写回 ``stop_loss``、当日熔断计数在 sidecar 里）。直接照抄会绕开本仓的
三态与整组替换语义；走决策层这一条路，则「重复项只留一条」「同标的多条只挂第一条」
「人工规则优先」这些判断**只有一份实现**，且都带着既有测试。

刻意不做的事
------------
* **不写存储**：本模块一行 IO 都没有；落库是 ``scripts/migrate_legacy_watch.py`` 调的
  ``watch_writer.write_watch_plan``；
* **不搬熔断 sidecar**（``live_watch_halt.json``）：它是「连续废单 ≥3 → 当日停手」的
  计数，治的是隔壁「触发→废单→重布防→再触发」的死循环；本仓执行器一次触发当日即
  终态（``armed → … → filled/failed``），**结构上不会重试**，搬过来是没有消费者的状态。
  它本身也是当日态（次日自动恢复），跨日迁移无意义；
* **不做多 agent 仲裁**：同一个标的两家都挂了条件位时（实测有：``600276.SH``、
  ``002518.SZ`` 在隔壁台账里就是两家各持一半），本仓是「先到先得、后来者每轮可见地
  被拒」——与决策层的既有口径**一字不差**，本层不另立一套（P2.4 已明确留给 P2.7）。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from backend.services.live_trading.services import sltp_executor as executor
from backend.shared.decision.contract import (
    WATCH,
    Decision,
    parse_level,
    parse_pct,
)
from backend.shared.decision.watch_map import WatchPlan, plan_watch
from backend.shared.order_contract import normalize_agent

#: 整轮拒收的理由（非空 ⇒ 一条都不许写：这种输入下「部分成功」比全不动更坏——
#: 操作员会把「挂了一半」读成「挂完了」）
REJECT_NOT_OBJECT = "顶层不是对象（应为 {agent: [规则]}）"
REJECT_AGENT_NOT_LIST = "该 agent 的值不是列表"
REJECT_NO_AGENT = "agent 名为空（无法构造归属标记 llm:<agent>）"
REJECT_DUPLICATE_AGENT = "归一后与另一个 agent 同名（两组会互相整组替换）"

#: 逐条规则的问题（不拒收整轮，但必须留痕：**静默少挂一条 = 那个防守位今天没人管**）
PROBLEM_RULE_NOT_OBJECT = "第 {index} 条不是对象"
PROBLEM_NO_CODE = "第 {index} 条缺 code"

#: 规则表容量：与执行器**同一常量**（比较用；真的判超限在 write_watch_plan 里）
CAP = executor.MAX_RULES


@dataclass(frozen=True)
class LegacyRule:
    """隔壁文件里的一条规则 + 它转出来的决策。

    :attr:`source` 是**原样的那条字典**：不进规则表（执行器词表里没有 ``reason``），
    但要进迁移记录——「为什么 44.60 是止损位」这句只有模型写的那段话能回答，而它是
    切换后复盘时的第一手材料。
    """

    agent: str
    index: int
    code: str
    decision: Decision
    created_ts: str = ""
    reason: str = ""


@dataclass(frozen=True)
class LegacyGroup:
    """一个 agent 的整组条件位（隔壁语义：最新分析说了算，整组替换）。"""

    agent: str
    rules: tuple[LegacyRule, ...] = ()

    def decisions(self) -> tuple[Decision, ...]:
        return tuple(r.decision for r in self.rules)


@dataclass(frozen=True)
class LegacyWatch:
    """隔壁 ``live_watch.json`` 的一份读入结果（**有序**：agent 按名排序）。"""

    groups: tuple[LegacyGroup, ...] = ()
    #: 拒收理由（非空 ⇒ ``ok`` 为假，一条都不许写）
    errors: tuple[str, ...] = ()
    #: 逐条规则的问题（可见即留痕）
    problems: tuple[str, ...] = ()
    #: 文件里的规则总数（含被拒的：对账用「读进来多少 / 挂出多少」）
    source_rules: int = 0

    @property
    def ok(self) -> bool:
        return not self.errors

    def agents(self) -> tuple[str, ...]:
        return tuple(g.agent for g in self.groups)

    def rules(self) -> tuple[LegacyRule, ...]:
        return tuple(r for g in self.groups for r in g.rules)


def _rule_of(agent: str, raw: Mapping[str, Any], index: int) -> LegacyRule:
    """一条隔壁规则 → :class:`LegacyRule`。

    只认隔壁**文档里**的字段（``code``/``stop_loss``/``take_profit``/``move_stop``/
    ``pct``/``reason``/``created_ts``）。文件里还混着哨兵自己的边车键（实测有
    ``_skip_notified``、``stop_from_move``）——**一律忽略**：那些是本仓哨兵状态机要
    自己重新建立的东西（通知去重、棘轮已用掉的标记），照抄过来等于把两套状态机的
    内部位混在一张表里。

    ``pct`` 走三态解析：隔壁文件里的比例已经是它**算过**的结果（缺失/脏值已按全仓
    落定），所以正常情况一律 ``given``；写成 0 是「不表达卖出量」→ 不挂（见
    ``watch_map`` 的比例三态），手改文件造出的 ``NaN`` 则回到「按全仓挂 + 留注记」。
    """
    return LegacyRule(
        agent=agent,
        index=index,
        code=str(raw.get("code") or "").strip(),
        decision=Decision(
            action=WATCH,
            code=str(raw.get("code") or "").strip(),
            pct=parse_pct(raw.get("pct")),
            stop_loss=parse_level(raw.get("stop_loss")),
            take_profit=parse_level(raw.get("take_profit")),
            move_stop=parse_level(raw.get("move_stop")),
            reason=str(raw.get("reason") or "").strip(),
        ),
        created_ts=str(raw.get("created_ts") or "").strip(),
        reason=str(raw.get("reason") or "").strip(),
    )


def parse_legacy_watch(doc: Any) -> LegacyWatch:
    """隔壁 ``live_watch.json`` 的文档 → :class:`LegacyWatch`（**不抛异常**）。

    agent 一律过 ``normalize_agent``（与决策轮、订单表、分账账本**同一个**身份口径）：
    迁移写下的 ``owner`` 必须与将来那个 agent 每轮整组替换时算出的 ``owner`` 逐字相同，
    否则这批规则会变成「没人认领的规则」——既不会被替换也不会被删除，静默长在表里。
    """
    if not isinstance(doc, Mapping):
        return LegacyWatch(errors=(REJECT_NOT_OBJECT,))

    groups: list[LegacyGroup] = []
    errors: list[str] = []
    problems: list[str] = []
    seen: dict[str, str] = {}  # 归一后 → 原键（重名的第二个拒收）
    source_rules = 0

    for raw_agent, rows in sorted((doc or {}).items(), key=lambda kv: str(kv[0])):
        agent = normalize_agent(raw_agent)
        if not agent:
            errors.append(f"{REJECT_NO_AGENT}：{raw_agent!r}")
            continue
        if agent in seen:
            errors.append(f"{REJECT_DUPLICATE_AGENT}：{raw_agent!r} 与 {seen[agent]!r}")
            continue
        seen[agent] = str(raw_agent)
        if not isinstance(rows, list):
            errors.append(f"{REJECT_AGENT_NOT_LIST}：{agent}")
            continue

        rules: list[LegacyRule] = []
        for i, raw in enumerate(rows):
            # 问题一律带 agent：两家各坏一条时，不带名字的「第 3 条缺 code」在留痕里
            # 指认不到人——而留痕的用途恰恰是事后有人能回答「少的那条是谁的」
            if not isinstance(raw, Mapping):
                problems.append(f"{agent}：{PROBLEM_RULE_NOT_OBJECT.format(index=i)}")
                continue
            source_rules += 1
            rule = _rule_of(agent, raw, i)
            if not rule.code:
                problems.append(f"{agent}：{PROBLEM_NO_CODE.format(index=i)}")
                continue
            rules.append(rule)
        groups.append(LegacyGroup(agent=agent, rules=tuple(rules)))

    return LegacyWatch(
        groups=tuple(groups),
        errors=tuple(errors),
        problems=tuple(problems),
        source_rules=source_rules,
    )


def plan_groups(watch: LegacyWatch) -> tuple[WatchPlan, ...]:
    """逐家 → :class:`WatchPlan`（映射、去重、组合校验全走 ``plan_watch``）。"""
    return tuple(plan_watch(g.decisions(), agent=g.agent) for g in watch.groups)


def _owner_of(agent: str) -> str:
    return executor.llm_owner(agent)


def has_rules(plan: WatchPlan) -> bool:
    """这一家本轮有没有可写的东西（**空集不写**）。

    与决策层的口径一字不差（``decision_round._maybe_write_watch``：本轮一条 ``watch``
    都没有时**整组保留**）：``write_watch_plan`` 是整组替换，拿空集去写等于把该 agent
    已挂上的守护规则全摘掉。「没提到」不等于「撤销全部守护」——迁移重复跑一次就把
    上一次挂好的止损全清了，是这个工具最难用肉眼发现的失效形态（跑完一切正常）。
    """
    return bool(plan.rules)


@dataclass(frozen=True)
class PreviewAgent:
    """预演里一家 agent 的处置（与 ``WatchWriteResult`` 同形，便于逐项对照）。"""

    agent: str
    owner: str = ""
    armed: tuple[str, ...] = ()
    #: 本轮真正要写进表里的规则（``normalize_rule`` + 归属已盖章，**逐字就是落库内容**）。
    #: 存档与复验都拿它当「应该长什么样」——复验因此不必自己再推一遍比例映射。
    arms: tuple[dict[str, Any], ...] = ()
    removed: tuple[str, ...] = ()
    #: 本轮无规则、按口径整组保留的旧标的（既没挂新的也没摘旧的）
    kept: tuple[str, ...] = ()
    conflicts: tuple[Any, ...] = ()
    rejected: tuple[Any, ...] = ()
    overflow: str = ""


@dataclass(frozen=True)
class Preview:
    """预演结果：逐家处置 + 表规模变化 + 要人看的地方。"""

    agents: tuple[PreviewAgent, ...] = ()
    table_before: int = 0
    table_after: int = 0
    cap: int = CAP
    problems: tuple[str, ...] = ()

    @property
    def armed_total(self) -> int:
        return sum(len(a.armed) for a in self.agents)

    @property
    def conflicts_total(self) -> int:
        return sum(len(a.conflicts) for a in self.agents)

    def attention(self) -> tuple[str, ...]:
        """「要人看一眼」的清单（预演与落库共用同一套判据）。"""
        out = list(self.problems)
        for a in self.agents:
            if a.overflow:
                out.append(f"{a.agent}: {a.overflow}")
            for c in a.conflicts:
                holder = f"（现持有：{c.holder}）" if getattr(c, "holder", "") else ""
                out.append(f"{a.agent}: {c.symbol} 未挂上——{c.reason}{holder}")
        return tuple(out)

    def summary(self) -> dict[str, Any]:
        return {
            "agents": [
                {
                    "agent": a.agent,
                    "owner": a.owner,
                    "armed": list(a.armed),
                    "arms": [dict(r) for r in a.arms],
                    "removed": list(a.removed),
                    "kept": list(a.kept),
                    "conflicts": [
                        {
                            "symbol": c.symbol,
                            "reason": c.reason,
                            "holder": c.holder,
                        }
                        for c in a.conflicts
                    ],
                    "rejected": [
                        {"code": r.code, "reason": r.reason} for r in a.rejected
                    ],
                    "overflow": a.overflow,
                }
                for a in self.agents
            ],
            "table_before": self.table_before,
            "table_after": self.table_after,
            "cap": self.cap,
            "problems": list(self.problems),
        }


def simulate_apply(
    plans: Sequence[WatchPlan],
    table: Sequence[Mapping[str, Any]],
) -> Preview:
    """**不写任何东西**地预演逐家整组替换（实际落库走 ``write_watch_plan``）。

    模型与 ``write_watch_plan`` 的那段循环**逐句对应**：先把表拆成「我的一组
    （``mine_old``，本轮会被替换掉）」与「别人的（``others``）」，再调
    ``resolve_plan`` 判冲突，最后把表更新成 ``others + 本轮挂上的``。两处若分叉，
    ``test_legacy_watch_migration.py`` 的差分用例（预演 vs 真写一遍）会立刻变红——
    这是本层唯一需要靠测试钉住的耦合。

    顺序即输入顺序（迁移固定按 agent 名排序，见 ``parse_legacy_watch``）：同一个标的
    两家都挂时**先处理的那家拿到槽位**，后来者记 ``REJECT_OTHER_AGENT``。顺序不定 =
    每次跑出来的归属不一样，而操作员是照着预演按确认的。
    """
    from backend.shared.decision import watch_writer as ww

    current = [dict(r) for r in table]
    out: list[PreviewAgent] = []
    problems: list[str] = []
    for plan in plans:
        owner = _owner_of(plan.agent)
        if not owner:
            problems.append(f"{plan.agent or '（空 agent）'}：归属为空，整组不写")
            out.append(PreviewAgent(agent=plan.agent, rejected=plan.rejected))
            continue
        mine_old = [r for r in current if str(r.get("owner") or "") == owner]
        if not has_rules(plan):
            # 空集：整组保留（同决策层口径）。它**不碰表**，所以也不进 current 的更新。
            preview_agent = PreviewAgent(
                agent=plan.agent,
                owner=owner,
                rejected=tuple(plan.rejected),
                kept=tuple(str(r.get("symbol") or "") for r in mine_old),
            )
            out.append(preview_agent)
            # 空集有两种：文件里这家**本来就没条目**（无事发生，不必吵），和**有条目但
            # 一条都挂不上**（这家本轮等于没更新，表里留着的是上一次的守护）。后者在
            # 切换窗口里与「搬完了」长得一模一样，必须吵——它正是本工具要消灭的那种
            # 「看起来有保护」。
            if plan.rejected:
                reasons = sorted({str(r.reason) for r in plan.rejected})
                problems.append(
                    f"{plan.agent}: 源 {len(plan.rejected)} 条一条都挂不上"
                    f"（{'；'.join(reasons)}）——整组保留旧规则 "
                    f"{list(preview_agent.kept)}，本轮这家没有更新"
                )
            continue
        others = [r for r in current if str(r.get("owner") or "") != owner]
        others_by_symbol = {str(r.get("symbol") or ""): r for r in others}
        armed, conflicts = ww.resolve_plan(plan, others_by_symbol, owner)
        total = len(others) + len(armed)
        overflow = ""
        if total > CAP:
            # 与 write_watch_plan 同判据：超上限**这一家一条都不写**（部分写入会让
            # 「挂了几条」变成没人说得准的数）
            overflow = f"合并后 {total} 条 > 上限 {CAP}：本轮一条都不写"
            armed = []
        else:
            current = others + [rule for _, rule in armed]
        kept_symbols = {str(r.get("symbol") or "") for r in current}
        out.append(
            PreviewAgent(
                agent=plan.agent,
                owner=owner,
                armed=tuple(str(r.get("symbol") or "") for _, r in armed),
                arms=tuple(dict(r) for _, r in armed),
                # 「被摘掉」按**写完之后表里还有没有这个槽位**算（write_watch_plan 的
                # 口径是「槽位已空才清状态」），不是「我的旧规则不在新清单里」——被别家
                # 接手的槽位不算摘除。
                removed=(
                    tuple(
                        str(r.get("symbol") or "")
                        for r in mine_old
                        if str(r.get("symbol") or "") not in kept_symbols
                    )
                    if not overflow
                    else ()
                ),
                conflicts=tuple(conflicts),
                rejected=tuple(plan.rejected),
                overflow=overflow,
            )
        )
    return Preview(
        agents=tuple(out),
        table_before=len(table),
        table_after=len(current),
        problems=tuple(problems),
    )


def roster_mismatches(agents: Iterable[str], known: Iterable[str]) -> tuple[str, ...]:
    """名册外的 agent（要写法对象：这些规则将来**没人认领**，长在表里换不掉）。

    迁移写下的每一条规则都带 ``owner="llm:<agent>"``，而「谁在替换这一组」由名册
    （P2.9 ``QM_DECISION_LLM_ROSTER``）决定。名册里没有的名字 = 那一组规则永远不会被
    整组替换：既不会被撤，也不会跟着新的分析走——比没挂上更坏，因为它**看起来有保护**。
    """
    known_set = {str(a or "") for a in known}
    return tuple(sorted({a for a in agents if a and a not in known_set}))


__all__ = [
    "CAP",
    "LegacyGroup",
    "LegacyRule",
    "LegacyWatch",
    "Preview",
    "PreviewAgent",
    "has_rules",
    "parse_legacy_watch",
    "plan_groups",
    "roster_mismatches",
    "simulate_apply",
]
