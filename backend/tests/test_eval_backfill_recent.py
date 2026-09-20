"""每日选股回填自动化（`scripts/eval/backfill_recent.py`，设计 §1.4）。

`daily_selection` 的事后验证维（T+1..T+H 真实超额）在评分当天必然缺省——
前向数据还没发生，只能等窗口闭合后重跑。若没有回填，这些天会**永久停在 pending**，
评估中心的每日选股榜会挂着一排「待回填 †」。

盯三件事：
- **窗口没闭合就不许重跑**：`daily_selection._forward_return` 在 bar 不够时会
  用最后一根 bar 顶替 → 悄悄把 T+5 读成 T+2 的收益。等不到就等，不许早跑；
- **键形必须与原行一致**：`eval_scores` 的唯一键是
  `(object_type, object_id, snapshot_date, tenant_id, user_id)`，原行 `user_id=''`
  时换一个 user 重跑会**新增一行**而不是覆盖（榜单出现同一天两条）；
- **空转不算成功**：没有可回填的日期时要如实说明是「都已回填」还是「窗口都没闭合」。
"""

from __future__ import annotations

import pytest

from backend.scripts.eval.backfill_recent import (
    count_bars_after,
    is_pending,
    normalize_date,
    plan_backfill,
)

HORIZON = 5


# ── pending 判定 ─────────────────────────────────────────────────────


@pytest.mark.unit
def test_is_pending_true_for_backfilled_placeholder():
    """落表时 realized 维 score=None（pending †）→ 待回填。"""
    assert (
        is_pending({"realized": {"score": None, "detail": {"pending": True}}}) is True
    )


@pytest.mark.unit
def test_is_pending_false_once_realized_is_scored():
    assert is_pending({"realized": {"score": 72.5}}) is False


@pytest.mark.unit
def test_is_pending_false_when_dimension_absent_entirely():
    """整维缺失（不是「待前向」）不算待回填——重跑也不会长出来。"""
    assert is_pending({}) is False
    assert is_pending(None) is False


# ── bar 计数 ─────────────────────────────────────────────────────────


@pytest.mark.unit
def test_count_bars_after_counts_strictly_after_the_candidate():
    """T+1..T+H 是**之后**的 bar：候选日当天的 bar 不算进前向。"""
    bars = ["2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04"]

    counts = count_bars_after(bars, ["2026-09-01", "2026-09-03", "2026-09-09"])

    assert counts == {"2026-09-01": 3, "2026-09-03": 1, "2026-09-09": 0}


@pytest.mark.unit
def test_count_bars_after_normalizes_yyyymmdd_bar_dates():
    """**实测踩到的坑**：QuantDB 的 `dt` 列 `astype(str)` 出来是 `20260831`，
    与快照的 `2026-09-18` 直接比字符串时 `"20260831" > "2026-09-18"` 为真
    （第 5 位 `0` > `-`）→ 15 根 bar 全被算成「该日之后」，闭窗闸门对所有日期放行。"""
    bars = ["20260831", "20260901", "20260917", "20260918"]

    counts = count_bars_after(bars, ["2026-09-17", "2026-09-18"])

    assert counts == {"2026-09-17": 1, "2026-09-18": 0}


@pytest.mark.unit
def test_normalize_date_accepts_both_forms_and_rejects_junk():
    assert normalize_date(20260918) == "2026-09-18"
    assert normalize_date("20260918") == "2026-09-18"
    assert normalize_date("2026-09-18") == "2026-09-18"
    assert normalize_date("2026-09-18T00:00:00") == "2026-09-18"
    assert normalize_date("") is None
    assert normalize_date(None) is None
    assert normalize_date("not-a-date") is None


@pytest.mark.unit
def test_unparseable_candidate_date_is_not_treated_as_closed():
    """认不出的日期算「不知道」，不算「够了」——放行会跑出短窗口假收益。"""
    counts = count_bars_after(["20260918"], ["not-a-date"])

    assert counts == {"not-a-date": 0}


# ── 计划 ─────────────────────────────────────────────────────────────


_PENDING = {"realized": {"score": None, "detail": {"pending": True}}}
_DONE = {"realized": {"score": 71.0}}


@pytest.mark.unit
def test_plan_backfill_needs_both_pending_and_closed_window():
    """两个条件缺一不可：本身待回填 ∧ 前向 bar 已够（≥ horizon+1）。"""
    scored = {
        "2026-09-10": _PENDING,  # 待回填，窗口已闭合 → 入选
        "2026-09-17": _PENDING,  # 待回填，但只有 3 根 bar → 等，不早跑
        "2026-09-15": _DONE,  # 已回填 → 不重跑
    }
    bars = {"2026-09-10": 6, "2026-09-17": 3, "2026-09-15": 6}

    assert plan_backfill(scored, bars, horizon=HORIZON) == ["2026-09-10"]


@pytest.mark.unit
def test_plan_backfill_boundary_is_horizon_plus_one_bars():
    """正好 horizon+1 根 bar = 窗口闭合（含起点后第 H 个交易日）。"""
    scored = {"2026-09-10": _PENDING, "2026-09-11": _PENDING}
    bars = {"2026-09-10": HORIZON + 1, "2026-09-11": HORIZON}

    assert plan_backfill(scored, bars, horizon=HORIZON) == ["2026-09-10"]


@pytest.mark.unit
def test_plan_backfill_returns_ascending_dates():
    """升序重跑：同日多条靠 user_id 区分，但日期顺序稳定便于日志对读。"""
    scored = dict.fromkeys(("2026-09-12", "2026-09-10", "2026-09-11"), _PENDING)
    bars = dict.fromkeys(scored, 9)

    assert plan_backfill(scored, bars, horizon=HORIZON) == [
        "2026-09-10",
        "2026-09-11",
        "2026-09-12",
    ]


@pytest.mark.unit
def test_plan_backfill_ignores_dates_without_bar_evidence():
    """连 bar 计数都没有（基准序列取不到）→ 不重跑，等下次；不许当 0 或当够。"""
    assert plan_backfill({"2026-09-10": _PENDING}, {}, horizon=HORIZON) == []


@pytest.mark.unit
def test_plan_backfill_end_to_end_on_yyyymmdd_bar_dates():
    """闭窗闸门与真实取数形态串起来跑：最新一根 bar 当天**不许**回填。"""
    bars = ["20260831", "20260901", "20260902", "20260903", "20260904", "20260907"]
    scored = {"2026-08-31": _PENDING, "2026-09-01": _PENDING}
    counts = count_bars_after(bars, sorted(scored))

    # 08-31 之后 5 根 = horizon → 还差一根；09-01 之后 4 根 → 更不够
    assert counts == {"2026-08-31": 5, "2026-09-01": 4}
    assert plan_backfill(scored, counts, horizon=HORIZON) == []
