"""影子代价账报表的不变量（P1.6）。

盯三件事：
1. **两臂不混**（本模块最重要的口径）——影子臂（反事实）与已拦臂（既成事实）
   的代价绝不能被并进同一个均值，否则「假设 + 事实」会被当成一条规则的代价。
2. **判定只在足够样本上给方向**，且 (规则, 期) 一个都不能丢。
3. **不可得一律 `—`**，未计价样本按状态单列、不进 n。
"""

from __future__ import annotations

import pytest

from backend.shared.risk.gate_registry import REVIEW_SAMPLE_MIN
from backend.shared.risk.ghost import GhostRow
from backend.shared.risk.ghost_pricing import (
    HORIZONS,
    H_NOT_MATURED,
    H_UNTRADABLE,
    PriceInput,
    price_row,
)
from backend.shared.risk.ghost_report import (
    ARM_ENFORCED,
    ARM_SHADOW,
    ST_UNPRICED,
    T_CRIT,
    V_COSTLY,
    V_FLAT,
    V_INSUFFICIENT,
    V_SAVING,
    arm_of,
    build_report,
    render,
    stats_by_rule,
    verdict_rows,
)

PRICED_AT = "2026-09-24T08:00:00+08:00"
AS_OF = "2026-09-24"

RULE = "l3.stale_quote"
RULE2 = "l1.position_cap"


def _row(rule_id: str = RULE, *, date: str = "2026-09-18", enforced: bool = False, **kw) -> GhostRow:
    base = {
        "date": date,
        "rule_id": rule_id,
        "kind": "veto",
        "tenant": "default",
        "uid": "10000001",
        "symbol": f"SH6000{abs(hash(date + rule_id)) % 90 + 10:02d}",
        "side": "buy",
        "quantity": 100.0,
        "source": "rebalance",
        "reason": "r",
        "ts": 1790000000.0,
        "enforced": enforced,
    }
    return GhostRow(**{**base, **kw})


def _priced(cost: float, *, side: str = "buy", rule_id: str = RULE, date: str = "2026-09-18",
            enforced: bool = False, **kw) -> GhostRow:
    """造一条「四期成本都等于 cost」的已定价行（走真定价器，不手搓 fwd 形状）。"""
    row = _row(rule_id, date=date, enforced=enforced, side=side, **kw)
    exit_px = 1.0 + (cost if side == "buy" else -cost)
    inp = PriceInput(
        entry_day="2026-09-21",
        entry_px=1.0,
        tradable=True,
        exit_px=dict.fromkeys(HORIZONS, exit_px),
        bench=dict.fromkeys(HORIZONS, 0.0),
    )
    out = price_row(row, inp, priced_at=PRICED_AT)
    return out


def _unpriced(state_flag: str, *, rule_id: str = RULE, enforced: bool = False) -> GhostRow:
    """造一条不可计价的行（按需要的状态）。"""
    row = _row(rule_id, enforced=enforced)
    if state_flag == H_UNTRADABLE:
        inp = PriceInput(entry_day="2026-09-21", entry_px=1.0, tradable=False, reason="一字板")
    else:  # H_NOT_MATURED：有入场日但没到期
        inp = PriceInput(
            entry_day="2026-09-21",
            entry_px=1.0,
            tradable=True,
            exit_px=dict.fromkeys(HORIZONS),
        )
    return price_row(row, inp, priced_at=PRICED_AT)


# ── 臂：绝不合并 ────────────────────────────────────────────────────
def test_arm_of_splits_on_enforced():
    assert arm_of(_row(enforced=False)) == ARM_SHADOW
    assert arm_of(_row(enforced=True)) == ARM_ENFORCED


def test_shadow_and_enforced_rows_never_share_a_mean():
    """**本模块最关键的一条口径**：同规则同期两臂各出一行，均值互不掺和。"""
    rows = [_priced(0.02) for _ in range(3)] + [_priced(-0.01, enforced=True) for _ in range(3)]
    stats = stats_by_rule(rows, horizons=(1,))
    by_arm = {s.arm: s for s in stats if s.rule_id == RULE}
    assert set(by_arm) == {ARM_SHADOW, ARM_ENFORCED}
    assert by_arm[ARM_SHADOW].mean_cost == pytest.approx(0.02)
    assert by_arm[ARM_ENFORCED].mean_cost == pytest.approx(-0.01)
    assert by_arm[ARM_SHADOW].n_counted == 3 and by_arm[ARM_ENFORCED].n_counted == 3


def test_verdict_prefers_the_shadow_arm_when_both_exist():
    """决策相关的是「要不要启用」→ 取反事实那一臂。"""
    rows = [_priced(0.02) for _ in range(2)] + [_priced(-0.05, enforced=True) for _ in range(2)]
    got = [v for v in verdict_rows(stats_by_rule(rows, horizons=(1,))) if v.rule_id == RULE]
    assert len(got) == 1 and got[0].arm == ARM_SHADOW


def test_verdict_falls_back_to_enforced_when_no_shadow_rows():
    got = [v for v in verdict_rows(stats_by_rule([_priced(0.02, enforced=True)], horizons=(1,)))
           if v.rule_id == RULE]
    assert len(got) == 1 and got[0].arm == ARM_ENFORCED


# ── 判定门槛与方向 ─────────────────────────────────────────────────
def test_below_threshold_is_insufficient_no_matter_how_big_the_mean():
    """样本不足时**不给方向**——哪怕均值很吓人。"""
    rows = [_priced(0.50) for _ in range(REVIEW_SAMPLE_MIN - 1)]
    s = next(s for s in stats_by_rule(rows, horizons=(1,)) if s.arm == ARM_SHADOW)
    assert s.verdict == V_INSUFFICIENT


def test_enough_samples_positive_cost_is_flagged_costly():
    rows = [_priced(0.02 + 0.001 * (i % 5)) for i in range(REVIEW_SAMPLE_MIN + 5)]
    s = next(s for s in stats_by_rule(rows, horizons=(1,)) if s.arm == ARM_SHADOW)
    assert s.verdict == V_COSTLY and s.mean_cost > 0


def test_enough_samples_negative_cost_is_flagged_saving():
    rows = [_priced(-0.02 - 0.001 * (i % 5)) for i in range(REVIEW_SAMPLE_MIN + 5)]
    s = next(s for s in stats_by_rule(rows, horizons=(1,)) if s.arm == ARM_SHADOW)
    assert s.verdict == V_SAVING


def test_noisy_mean_stays_unjudged():
    """均值贴着 0 且噪声大 → 不显著（不给「净花钱」的假信号）。"""
    rows = [_priced(0.05 if i % 2 else -0.05) for i in range(REVIEW_SAMPLE_MIN + 2)]
    s = next(s for s in stats_by_rule(rows, horizons=(1,)) if s.arm == ARM_SHADOW)
    assert abs(s.t_stat or 0) < T_CRIT
    assert s.verdict == V_FLAT


def test_sell_side_cost_carries_the_same_sign_convention():
    """卖单被拦、后来跌了 → 成本为正（与买单同公式、相反符号）。"""
    s = next(s for s in stats_by_rule([_priced(0.03, side="sell")], horizons=(1,)))
    assert s.mean_cost == pytest.approx(0.03)


# ── 期与规则一个都不丢 ─────────────────────────────────────────────
def test_every_rule_and_horizon_survives_into_the_verdicts():
    """(规则, 期) 是结论的键——4 期 × 2 规则 = 8 行，一个都不能少。"""
    rows = [_priced(0.02, rule_id=RULE), _priced(0.03, rule_id=RULE2)]
    v = verdict_rows(stats_by_rule(rows))
    assert len(v) == len(HORIZONS) * 2
    assert {(s.rule_id, s.horizon) for s in v} == {
        (r, h) for r in (RULE, RULE2) for h in HORIZONS
    }


def test_horizons_can_differ_per_horizon():
    """t1 赚钱、t5 亏钱是常见形态——两期必须各自出结论，不能合成一个。"""
    row = _row()
    inp = PriceInput(
        entry_day="2026-09-21",
        entry_px=1.0,
        tradable=True,
        exit_px={1: 1.02, 5: 0.98, 20: 1.02, 60: 0.98},
        bench=dict.fromkeys(HORIZONS, 0.0),
    )
    rows = [price_row(row, inp, priced_at=PRICED_AT)] * (REVIEW_SAMPLE_MIN + 2)
    s = {x.horizon: x for x in stats_by_rule(rows)}
    assert s[1].mean_cost > 0 and s[5].mean_cost < 0


# ── 未计价：单列、不进 n、不写成 0 ─────────────────────────────────
def test_unpriced_rows_are_counted_by_state_and_excluded_from_n():
    rows = [_priced(0.02) for _ in range(3)] + [_unpriced(H_NOT_MATURED) for _ in range(7)]
    s = next(x for x in stats_by_rule(rows, horizons=(1,)) if x.arm == ARM_SHADOW)
    assert s.n_counted == 3, "未到期的行不得进 n"
    assert s.unpriced[H_NOT_MATURED] == 7
    assert s.n_total == 10


def test_untradable_rows_are_reported_not_silently_dropped():
    s = next(
        x
        for x in stats_by_rule([_unpriced(H_UNTRADABLE)], horizons=(1,))
        if x.arm == ARM_SHADOW
    )
    assert s.n_counted == 0 and s.unpriced[H_UNTRADABLE] == 1
    assert s.mean_cost is None, "不可得不得退化成 0"


def test_never_priced_rows_land_in_the_unpriced_bucket():
    s = next(
        x for x in stats_by_rule([_row()], horizons=(1,)) if x.arm == ARM_SHADOW
    )
    assert s.n_counted == 0 and s.unpriced[ST_UNPRICED] == 1


# ── 报表骨架与渲染 ─────────────────────────────────────────────────
def _report(rows, **kw):
    return build_report(rows, as_of=AS_OF, horizons=(1,), **kw)


def test_silent_and_unknown_rules_are_both_surfaced():
    """登记未触发 = 没有证据；留痕出现未登记 = 没被审阅过。两者都要可见。"""
    rep = _report([_priced(0.02, rule_id="l9.not_registered")])
    assert "l9.not_registered" in rep.unknown_rules
    assert any(g.rule_id != "l9.not_registered" for g in rep.silent_rules)


def test_render_always_states_the_shadow_mode():
    """这行是整份报表的免责声明，任何模式下都不能缺。"""
    rows = [_priced(0.02)]
    assert "反事实" in "\n".join(render(_report(rows, shadow_mode=True)))
    assert "闸已翻" in "\n".join(render(_report(rows, shadow_mode=False)))
    assert "未读到" in "\n".join(render(_report(rows, shadow_mode=None)))


def test_render_shows_dash_not_zero_for_missing_numbers():
    out = "\n".join(render(_report([_unpriced(H_UNTRADABLE)])))
    assert "—" in out
    assert "0.00%" not in out, "不可得的均值必须渲染成 —，不是 0.00%"


def test_render_lists_unpriced_states_next_to_the_block():
    out = "\n".join(render(_report([_priced(0.02), _unpriced(H_NOT_MATURED)])))
    assert "未计价" in out and H_NOT_MATURED in out


def test_render_of_an_empty_window_says_it_cannot_decide():
    out = "\n".join(render(_report([])))
    assert "不能支持任何取舍决定" in out


def test_report_counts_both_arms_and_versions():
    rep = _report([_priced(0.02), _priced(0.02, enforced=True)])
    assert rep.n_rows == 2 and rep.n_shadow == 1 and rep.n_enforced == 1
    assert rep.versions == (0,)


def test_window_is_the_span_of_the_rows():
    rep = _report([_priced(0.02, date="2026-09-14"), _priced(0.02, date="2026-09-18")])
    assert rep.window == ("2026-09-14", "2026-09-18")


def test_structural_rules_are_shown_not_filtered_out():
    """整手校验这类规则的代价必为 0，但**要让人看见**而不是在报表里消失。"""
    rep = _report([_priced(0.0, rule_id="l1.round_lot"), _priced(0.02)])
    assert any(s.rule_id == "l1.round_lot" for s in rep.verdicts)


def test_stats_are_deterministic_sorted():
    rows = [_priced(0.02, rule_id=RULE2), _priced(0.02, rule_id=RULE)]
    got = [(s.rule_id, s.horizon, s.arm) for s in stats_by_rule(rows, horizons=(1, 5))]
    assert got == sorted(got), "同一批输入必须给同一个顺序（报表可比、可 diff）"
