"""闸门登记表的不变量（P1.6）。

这份测试的作用不是"跑一遍代码"，而是**让规则无法沉默地绕过审阅**：
- 新增一条风控规则却忘了登记 → 红（词表与登记表双向覆盖）；
- 把 `falsify` 抄成 `evidence` → 红（依据与失效条件必须是两段话）；
- 把全表标成 `structural` 以逃过代价记账 → 红（关键规则必须可计价）；
- 复核日过去却没人复核 → 由 `review_overdue` 在报告里显式列出（本测试只钉判定口径）。

覆盖的是**两族**规则的并集（P2.1b 起）：执行族（`builtin_rules.py` 里走
`@rule` 注册的判定）+ 决策族（`backend/shared/decision/gates.py` 的纯函数）。
两族的区别与"为什么不合成一族"写在 `gate_registry` 的模块 docstring 里。
"""

from __future__ import annotations

import importlib
from datetime import date

import pytest

from backend.shared.decision.gates import ALL_RULE_IDS as DECISION_RULE_IDS
from backend.shared.risk import builtin_rules  # noqa: F401  —— 触发 @rule 注册
from backend.shared.risk.gate_registry import (
    KIND_STRUCTURAL,
    KIND_UNSEEN,
    KIND_VETO,
    KINDS,
    REVIEW_ACTIONS,
    REVIEW_SAMPLE_MIN,
    all_gates,
    gate_spec,
    kind_of,
    priced_kinds,
    review_overdue,
)
from backend.shared.risk.registry import all_rules

#: `where` 的首段是**模块名**（如 `builtin_rules.l1_available_cash`），这里给出
#: 它到导入路径的映射。新增一族规则要往这里加一行——**没有映射即红**，
#: 因为猜模块路径正是这条不变量要防的漂移（改名/搬家后 `where` 会烂掉）。
_MODULE_PATHS = {
    "builtin_rules": "backend.shared.risk.builtin_rules",
    "gates": "backend.shared.decision.gates",
}


#: 必须可计价的规则（**不能**被改成 structural 逃过记账）。选它们是因为
#: 它们的判定本身就是经济权衡：每一条都在"少赚"与"少亏"之间做选择。
#: 新增这类规则时往这里加一行——不要求全表，只要求这条底线不被悄悄抹掉。
_MUST_BE_PRICED = (
    "l1.available_cash",
    "l1.position_cap",
    "l1.per_order_pct",
    "l1.new_buys_per_day",
    "l1.leverage_cap",
    "l1.industry_cap",
    "l1.daily_loss_limit",
    "l3.max_order_value",
    "l3.price_deviation",
    "l3.stale_quote",
    "l3.order_frequency",
)


def _registered_ids() -> set[str]:
    """两族规则的**并集**：执行族（@rule 注册表）+ 决策族（gates.py 的常量表）。"""
    return {r.rule_id for r in all_rules()} | set(DECISION_RULE_IDS)


def test_every_code_rule_is_registered_and_vice_versa():
    """双向覆盖：代码里的规则 ↔ 登记表条目，任一边多出即红。"""
    code_ids = _registered_ids()
    spec_ids = {g.rule_id for g in all_gates()}

    missing = sorted(code_ids - spec_ids)
    extra = sorted(spec_ids - code_ids)
    assert not missing, f"以下规则在代码里注册了但登记表没有条目：{missing}"
    assert not extra, f"登记表有以下条目但代码里没有对应规则：{extra}"


def test_decision_family_is_covered_by_the_union():
    """决策族的 id 必须真的被登记表收下（并集不是"看着像有覆盖"）。

    少了这条，若 `ALL_RULE_IDS` 被误改成空元组，上面的并集覆盖会**静默通过**
    ——那正是「零项参与 = 通过」的变体。
    """
    assert DECISION_RULE_IDS, "决策族规则常量为空，说明 gates.py 的 ALL_RULE_IDS 坏了"
    spec_ids = {g.rule_id for g in all_gates()}
    missing = sorted(set(DECISION_RULE_IDS) - spec_ids)
    assert not missing, f"决策族规则没有登记表条目：{missing}"


def test_where_points_at_a_real_function():
    """`where` 必须能**机械解析**到一个真实函数：指针要是指空，等于没写。

    只写文件名（`gates.py`）也能糊过去，但那样改名/搬家之后没人知道断没断。
    模块名到导入路径的映射写在 `_MODULE_PATHS`（不许猜），没有映射即红。
    """
    for g in all_gates():
        module_name, _, attr = g.where.partition(".")
        path = _MODULE_PATHS.get(module_name)
        assert path is not None, (
            f"{g.rule_id} 的 where={g.where!r} 首段模块名未登记到 _MODULE_PATHS"
        )
        assert attr, f"{g.rule_id} 的 where={g.where!r} 只给了模块名，没有函数名"
        module = importlib.import_module(path)
        assert callable(getattr(module, attr, None)), (
            f"{g.rule_id} 的 where={g.where!r} 指向的不是可调用对象（改名/搬家了？）"
        )


def test_registry_ids_are_unique():
    ids = [g.rule_id for g in all_gates()]
    assert len(ids) == len(set(ids)), "登记表出现重复 rule_id"


def test_every_field_is_filled():
    """骨架字段非空——空字段等于没写，而没写就没人能复核。"""
    for g in all_gates():
        for field in (
            "rule_id",
            "title",
            "where",
            "kind",
            "evidence",
            "falsify",
            "review_by",
        ):
            assert str(getattr(g, field)).strip(), f"{g.rule_id} 的 {field} 为空"


def test_kind_is_known():
    for g in all_gates():
        assert g.kind in KINDS, f"{g.rule_id} 的 kind={g.kind!r} 不在 {KINDS}"


def test_falsify_is_not_a_copy_of_evidence():
    """`falsify` 必须是**可观测的证伪条件**，不是依据的复述。

    这条钉住的是一种很自然的退化：写给老板看的"依据"抄一遍当"失效条件"，
    于是这条规则永远不会因为任何观测而下线——正是本层要消灭的单向棘轮。
    """
    for g in all_gates():
        assert g.falsify.strip() != g.evidence.strip(), (
            f"{g.rule_id} 的 falsify 与 evidence 完全相同（失效条件被复述成依据）"
        )
        assert len(g.falsify.strip()) >= 20, (
            f"{g.rule_id} 的 falsify 过短，无法据此判定（{g.falsify!r}）"
        )


def test_review_by_is_parseable_iso_date():
    for g in all_gates():
        try:
            date.fromisoformat(g.review_by)
        except ValueError as exc:  # pragma: no cover - 失败信息才是价值
            raise AssertionError(
                f"{g.rule_id} 的 review_by={g.review_by!r} 不是 ISO 日期：{exc}"
            ) from exc


def test_structural_gates_explain_how_to_read_their_triggers():
    """标成「不计代价」的规则必须写明它的触发数该怎么读。

    否则下一个人看到 structural 规则触发 500 次会顺手删掉它——
    而那 500 次很可能是上游缺陷（非整手、账不平）的**症状**，删了症状就没了。
    """
    for g in all_gates():
        if g.kind == KIND_STRUCTURAL:
            assert g.note.strip(), (
                f"{g.rule_id} 标为 structural 但没写 note（触发数怎么读、该修哪里）"
            )


def test_must_be_priced_rules_are_still_priced():
    """底线：经济权衡型规则不许被改成 structural 以逃过代价记账。"""
    for rid in _MUST_BE_PRICED:
        spec = gate_spec(rid)
        assert spec is not None, f"{rid} 从登记表消失"
        assert spec.kind == KIND_VETO, f"{rid} 被改成 {spec.kind}，代价将不再被记账"


def test_priced_kinds_is_the_single_source_for_accounting():
    assert priced_kinds() == (KIND_VETO,)


def test_unseen_is_not_priced():
    """`unseen` 没有「放行即成交」的反事实（模型未必会选那只票），不许进代价账。"""
    assert KIND_UNSEEN in KINDS
    assert KIND_UNSEEN not in priced_kinds()


def test_kind_of_unknown_rule_defaults_to_veto():
    """未登记规则按 veto 记：宁可按最强口径读，不静默丢类别。"""
    assert kind_of("does.not.exist") == KIND_VETO
    assert gate_spec("does.not.exist") is None
    assert kind_of("") == KIND_VETO


def test_kind_of_matches_registry():
    for g in all_gates():
        assert kind_of(g.rule_id) == g.kind


def test_review_overdue_uses_injected_date_not_wall_clock():
    """复核判定按注入日期，不读时钟——否则测试会随日历腐烂。"""
    # 早于所有复核日：无到期
    assert review_overdue(date(2026, 1, 1)) == []
    # 恰在首轮复核日（含当日）即视为到期
    due = review_overdue(date(2026, 10, 23))
    assert len(due) == len(all_gates()), "首轮复核日当天应全部到期"
    assert all(g.review_by <= "2026-10-23" for g in due)
    # 排序：按复核日升序（同日的按 id）
    assert due == sorted(due, key=lambda g: (g.review_by, g.rule_id))


def test_review_overdue_accepts_iso_string():
    assert review_overdue("2026-10-22") == []
    assert len(review_overdue("2026-10-23")) == len(all_gates())


def test_review_actions_are_the_three_way_choice():
    assert REVIEW_ACTIONS == ("保留（写明理由）", "放宽（改参数）", "删除")
    assert REVIEW_SAMPLE_MIN == 30


@pytest.mark.parametrize("rid", [g.rule_id for g in all_gates()])
def test_each_gate_has_nonempty_title(rid: str):
    """逐条参数化：报告里每个 id 都有人能读的标题。"""
    spec = gate_spec(rid)
    assert spec is not None and spec.title.strip()
