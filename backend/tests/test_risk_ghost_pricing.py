"""影子代价账定价核心的不变量（P1.6）。

这份测试盯的是**口径**而非算术：符号约定、入场可成交性、以及"不可得绝不写成 0"。
最后一条最容易退化——一旦某个状态分支返回 0.0，报告里就多出一批"不花钱"的规则，
而它们其实一个样本都没有。
"""

from __future__ import annotations

import math
from dataclasses import replace

import pytest

from backend.shared.risk.ghost import GhostRow
from backend.shared.risk.ghost_pricing import (
    COUNTED_STATES,
    H_NOT_MATURED,
    H_NO_DATA,
    H_OK,
    H_RETRIED,
    H_UNTRADABLE,
    HORIZONS,
    PRICE_EPS,
    RETRY_WINDOW_S,
    PriceInput,
    costs_by_horizon,
    cross_section_mean,
    entry_unfillable_reason,
    excess_of,
    horizon_days,
    horizon_key,
    is_retry_superseded,
    merge_fwd,
    next_trading_day,
    price_row,
    price_row_monotone,
    state_of,
    window_return,
)

#: 一周交易日（含跨周末：09-18 周五 → 09-21 周一）
CAL = [
    "2026-09-16",
    "2026-09-17",
    "2026-09-18",
    "2026-09-21",
    "2026-09-22",
    "2026-09-23",
]

PRICED_AT = "2026-09-24T08:00:00+08:00"


def _row(**kw) -> GhostRow:
    base = {
        "date": "2026-09-18",
        "rule_id": "l1.position_cap",
        "kind": "veto",
        "tenant": "default",
        "uid": "10000001",
        "symbol": "SH600000",
        "side": "buy",
        "quantity": 100.0,
        "source": "rebalance",
        "reason": "集中度超限",
        "ts": 1790000000.0,
    }
    return GhostRow(**{**base, **kw})


def _full_input(**kw) -> PriceInput:
    """一个「处处有数」的定价输入：入场 10.00，四期收盘各涨一点，基准 0。"""
    exit_px = {h: 10.00 * (1 + 0.01 * h) for h in HORIZONS}
    bench = dict.fromkeys(HORIZONS, 0.0)
    base = {
        "entry_day": "2026-09-21",
        "entry_px": 10.00,
        "tradable": True,
        "exit_px": exit_px,
        "bench": bench,
    }
    return PriceInput(**{**base, **kw})


# ── 日历与到期 ──────────────────────────────────────────────────────
def test_entry_is_the_next_trading_day_never_the_block_day():
    """次开入场：拦下当天**不是**入场日（否则对上午被拦的单就是前视）。"""
    assert next_trading_day("2026-09-18", CAL) == "2026-09-21"
    assert next_trading_day("2026-09-17", CAL) == "2026-09-18"


def test_entry_from_a_non_trading_day_lands_on_the_next_session():
    """周末批跑（实测 l0.session 106 条全是非交易日）也要有确定的入场日。"""
    assert next_trading_day("2026-09-19", CAL) == "2026-09-21", "周六 → 下周一"
    assert next_trading_day("2026-09-20", CAL) == "2026-09-21", "周日 → 下周一"


def test_entry_is_none_when_calendar_runs_out():
    """日历用尽 → None（不是拿最后一天凑）。"""
    assert next_trading_day("2026-09-23", CAL) is None
    assert next_trading_day("2026-09-18", []) is None
    assert next_trading_day("", CAL) == "2026-09-16"


def test_horizon_days_counts_entry_as_day_one():
    # 入场日在 CAL 的下标 3 → t1=下标3、t3=下标5（CAL 只剩 3 天）
    got = horizon_days("2026-09-21", CAL, horizons=(1, 3))
    assert got[1] == "2026-09-21", "t1 = 入场日收盘（开→收一日）"
    assert got[3] == "2026-09-23", "t3 = 入场日起第 3 个交易日"
    # 日历不足 → None（=未到期），绝不是空字符串或末日
    assert horizon_days("2026-09-21", CAL, horizons=(20,))[20] is None


def test_horizon_days_from_a_longer_calendar():
    """t5 落在第 5 个交易日（用一段足够长的日历验，避免边界掩盖口径错误）。"""
    cal = [f"2026-09-{d:02d}" for d in (1, 2, 3, 4, 7, 8, 9, 10)]
    got = horizon_days(cal[0], cal, horizons=(1, 5))
    assert got[1] == "2026-09-01"
    # 入场日算第 1 个 → t5 落在下标 4（09-07），不是下标 5（09-08）
    assert got[5] == "2026-09-07", "入场日起第 5 个交易日，不是第 6 个"


def test_horizon_days_unknown_entry_is_all_none():
    assert horizon_days("2026-01-01", CAL, horizons=(1, 5)) == {1: None, 5: None}


# ── 收益与基准 ──────────────────────────────────────────────────────
def test_window_return_is_open_to_close():
    assert window_return(10.0, 11.0) == pytest.approx(0.10)
    assert window_return(10.0, 10.0) == 0.0


def test_window_return_none_on_dirty_input():
    assert window_return(None, 11.0) is None
    assert window_return(10.0, None) is None
    assert window_return(0.0, 11.0) is None, "入场价非正 → 不可算（不是 0 收益）"
    assert window_return(-1.0, 11.0) is None
    assert window_return(float("nan"), 11.0) is None


def test_excess_and_cross_section_mean_skip_missing():
    assert excess_of(0.02, 0.005) == pytest.approx(0.015)
    assert excess_of(0.02, None) is None
    assert cross_section_mean([0.01, 0.03]) == pytest.approx(0.02)
    assert cross_section_mean([0.01, None, float("nan")]) == pytest.approx(0.01)
    assert cross_section_mean([]) is None
    assert cross_section_mean([None, float("inf"), float("nan")]) is None


# ── 入场可成交性（一字板 / 停牌）─────────────────────────────────────
def test_open_sealed_at_limit_up_cannot_be_bought():
    why = entry_unfillable_reason(
        "buy", open_px=11.0, limit_up=11.0, limit_down=9.0, volume=1e6, has_bar=True
    )
    assert why and "涨停" in why


def test_open_one_cent_below_limit_up_is_buyable():
    """差一分就不是一字板——这是「别把整批涨停股误判成买不到」的边界。"""
    assert (
        entry_unfillable_reason(
            "buy",
            open_px=11.0 - 0.01,
            limit_up=11.0,
            limit_down=9.0,
            volume=1e6,
            has_bar=True,
        )
        is None
    )


def test_open_at_limit_down_blocks_sells_only():
    why = entry_unfillable_reason(
        "sell", open_px=9.0, limit_up=11.0, limit_down=9.0, volume=1e6, has_bar=True
    )
    assert why and "跌停" in why
    # 同一天买入不受跌停影响（跌停照样买得到）
    assert (
        entry_unfillable_reason(
            "buy", open_px=9.0, limit_up=11.0, limit_down=9.0, volume=1e6, has_bar=True
        )
        is None
    )


def test_sell_into_limit_up_is_allowed():
    assert (
        entry_unfillable_reason(
            "sell",
            open_px=11.0,
            limit_up=11.0,
            limit_down=9.0,
            volume=1e6,
            has_bar=True,
        )
        is None
    )


def test_suspended_and_missing_bar_are_both_unfillable_but_distinguishable():
    suspended = entry_unfillable_reason(
        "buy", open_px=10.0, limit_up=11.0, limit_down=9.0, volume=0.0, has_bar=True
    )
    missing = entry_unfillable_reason(
        "buy", open_px=None, limit_up=None, limit_down=None, volume=None, has_bar=False
    )
    assert suspended and "停牌" in suspended
    assert missing and "无行情" in missing
    assert suspended != missing, "停牌与缺数据是两件事"


def test_no_limit_price_means_no_limit_check():
    """新股首日无涨跌幅限制（limit_up=inf / limit_down=0）不该被判成买不到。"""
    assert (
        entry_unfillable_reason(
            "buy",
            open_px=50.0,
            limit_up=math.inf,
            limit_down=0.0,
            volume=1e6,
            has_bar=True,
        )
        is None
    )


def test_price_eps_is_tiny():
    """容差只吸收浮点误差，不该大到吞掉一分钱。"""
    assert PRICE_EPS < 0.005


# ── 重试剔除：只对真被拦的行成立 ────────────────────────────────────
def test_shadow_rows_are_never_treated_as_retried():
    """**本模块最关键的一条口径**：影子放行的行恒不剔除。

    影子期单照常成交，「当天有成交」是实现分支而非污染；若按"有成交就剔除"
    会把影子期最干净的样本整批丢掉。详见 ghost_pricing 模块头。
    """
    row = _row(enforced=False, ts=1790000000.0)
    assert is_retry_superseded(row, 1790000030.0) is False, "影子行 + 30s 后成交"
    assert is_retry_superseded(row, 1790000000.0) is False
    assert (
        price_row(row, _full_input(), priced_at=PRICED_AT).fwd[horizon_key(1)]["state"]
        == H_OK
    )


def test_enforced_row_filled_soon_after_is_superseded():
    row = _row(enforced=True, ts=1790000000.0)
    assert is_retry_superseded(row, 1790000030.0) is True
    assert is_retry_superseded(row, 1790000000.0) is True, (
        "同秒也算（拦截没有改变结果）"
    )
    assert is_retry_superseded(row, 1790000000.0 + RETRY_WINDOW_S) is True
    assert is_retry_superseded(row, 1790000000.0 + RETRY_WINDOW_S + 1) is False, (
        "超窗不算重试"
    )


def test_fill_before_the_block_is_not_a_retry():
    """成交早于拦截 = 这是另一笔单，不是"拦了又被成交" → 不剔除。"""
    row = _row(enforced=True, ts=1790000000.0)
    assert is_retry_superseded(row, 1789999000.0) is False


def test_retry_needs_a_fill_timestamp_and_a_block_timestamp():
    assert is_retry_superseded(_row(enforced=True), None) is False
    assert is_retry_superseded(_row(enforced=True), float("nan")) is False
    assert is_retry_superseded(_row(enforced=True, ts=0.0), 1790000000.0) is False


# ── 定价：符号、状态、None 纪律 ─────────────────────────────────────
def test_blocked_buy_that_rose_costs_money():
    """被拦的买单后来涨了 → 错过收益 → 成本为正。"""
    out = price_row(_row(side="buy"), _full_input(), priced_at=PRICED_AT)
    assert costs_by_horizon(out, 1) == pytest.approx(0.01)
    assert costs_by_horizon(out, 5) == pytest.approx(0.05)
    assert out.entry_date == "2026-09-21" and out.entry_px == 10.00
    assert out.tradable is True and out.priced_at == PRICED_AT


def test_blocked_buy_that_fell_saved_money():
    inp = _full_input(exit_px={h: 10.00 * (1 - 0.01 * h) for h in HORIZONS})
    assert costs_by_horizon(
        price_row(_row(side="buy"), inp, priced_at=PRICED_AT), 5
    ) == (pytest.approx(-0.05))


def test_blocked_sell_that_fell_costs_money():
    """卖单被拦、后来跌了 → 没卖掉被套住 → 成本为正（与买单同一公式、相反符号）。"""
    inp = _full_input(exit_px={h: 10.00 * (1 - 0.01 * h) for h in HORIZONS})
    assert costs_by_horizon(
        price_row(_row(side="sell"), inp, priced_at=PRICED_AT), 5
    ) == (pytest.approx(0.05))


def test_excess_is_net_of_the_market():
    """标的涨 2%、市场涨 2% → 超额 0（规则没花一分钱）——基准必须真的减掉。"""
    inp = _full_input(
        exit_px=dict.fromkeys(HORIZONS, 10.2), bench=dict.fromkeys(HORIZONS, 0.02)
    )
    out = price_row(_row(side="buy"), inp, priced_at=PRICED_AT)
    assert costs_by_horizon(out, 1) == pytest.approx(0.0, abs=1e-12)


def test_untradable_row_is_marked_not_zeroed():
    """一字板买不到：状态显式、成本 None（**不是 0**）。"""
    inp = _full_input(tradable=False, reason="入场日开盘涨停（买不到）")
    out = price_row(_row(), inp, priced_at=PRICED_AT)
    for h in HORIZONS:
        assert state_of(out, h) == H_UNTRADABLE
        assert costs_by_horizon(out, h) is None
        assert out.fwd[horizon_key(h)]["cost"] is None
        assert out.fwd[horizon_key(h)]["ret"] is None


def test_not_matured_row_is_none_not_zero():
    inp = _full_input(exit_px={1: 10.1, 5: 10.5, 20: None, 60: None})
    out = price_row(_row(), inp, priced_at=PRICED_AT)
    assert state_of(out, 5) == H_OK
    assert state_of(out, 20) == H_NOT_MATURED
    assert costs_by_horizon(out, 20) is None, "未到期不得写成 0"
    assert out.fwd[horizon_key(20)]["cost"] is None


def test_no_entry_day_marks_no_data():
    out = price_row(_row(), PriceInput(), priced_at=PRICED_AT)
    for h in HORIZONS:
        assert state_of(out, h) == H_NO_DATA
        assert costs_by_horizon(out, h) is None


def test_entry_pending_is_not_matured_and_distinguishable_from_no_data():
    """入场日还没到日历上 → `not_matured`（过一天就能算），不是 `no_data`。

    两者在报告里意义相反：一个是"明天再看"，一个是"数据缺了要查"。混起来会让
    每个交易日的账都飘着一批假的坏数据告警，真缺口反而被淹掉。
    """
    pending = price_row(_row(), PriceInput(entry_pending=True), priced_at=PRICED_AT)
    missing = price_row(_row(), PriceInput(), priced_at=PRICED_AT)
    for h in HORIZONS:
        assert state_of(pending, h) == H_NOT_MATURED
        assert costs_by_horizon(pending, h) is None, "还没到 ≠ 0 代价"
        assert state_of(missing, h) == H_NO_DATA
    assert state_of(pending, 1) != state_of(missing, 1)


def test_entry_pending_does_not_mask_a_real_entry_day():
    """只是"日历还没到"的标记，不能覆盖真取到的入场日（有数就算数）。"""
    out = price_row(
        _row(),
        _full_input(entry_pending=True),
        priced_at=PRICED_AT,
    )
    assert state_of(out, 1) == H_OK
    assert costs_by_horizon(out, 1) == pytest.approx(0.01)
    assert out.entry_date == "2026-09-21"


def test_entry_pending_with_an_entry_day_but_no_price_is_no_data():
    """有入场日却没取到开盘价 —— 这是数据缺口，不是"还没到"。"""
    out = price_row(
        _row(),
        PriceInput(entry_day="2026-09-21", entry_px=None, entry_pending=True),
        priced_at=PRICED_AT,
    )
    assert state_of(out, 1) == H_NO_DATA, "有入场日就是试图取过数，缺了要报缺口"


def test_retried_row_is_excluded_from_costs_but_visible():
    row = _row(enforced=True, ts=1790000000.0)
    out = price_row(row, _full_input(), priced_at=PRICED_AT, retry_fill_ts=1790000060.0)
    for h in HORIZONS:
        assert state_of(out, h) == H_RETRIED
        assert costs_by_horizon(out, h) is None, "重试成功 = 拦截没有改变结果"


def test_missing_benchmark_leaves_cost_none():
    """基准拿不到 → 超额不可得 → 成本 None（不能当 0 处理）。"""
    inp = _full_input(bench={})
    out = price_row(_row(), inp, priced_at=PRICED_AT)
    assert costs_by_horizon(out, 1) is None
    assert out.fwd[horizon_key(1)]["ret"] is not None, "标的收益仍应记下"


def test_only_ok_state_is_counted():
    assert COUNTED_STATES == (H_OK,)


def test_no_state_branch_ever_produces_a_zero():
    """把"不可得"写成 0.0 会让报告多出一批"不花钱"的规则——
    这条用参数化把所有退化分支逐个钉死。"""
    cases = {
        H_UNTRADABLE: _full_input(tradable=False),
        H_NOT_MATURED: _full_input(exit_px=dict.fromkeys(HORIZONS)),
        H_NO_DATA: PriceInput(),
    }
    for expected, inp in cases.items():
        out = price_row(_row(), inp, priced_at=PRICED_AT)
        for h in HORIZONS:
            ent = out.fwd[horizon_key(h)]
            assert ent["state"] == expected
            for f in ("cost", "ret", "bench", "excess"):
                assert ent[f] is None, f"{expected} 状态的 {f} 必须是 None，不是 0"


# ── 不可变与读数入口 ────────────────────────────────────────────────
def test_price_row_does_not_mutate_the_original():
    row = _row()
    price_row(row, _full_input(), priced_at=PRICED_AT)
    assert row.fwd is None and row.entry_px is None and row.priced_at is None


def test_is_priced_means_went_through_the_pricer_not_has_a_cost():
    """走过定价器 ≠ 有代价数——统计口径不能拿 is_priced 当分母。"""
    out = price_row(_row(), PriceInput(), priced_at=PRICED_AT)
    assert out.is_priced is True
    assert costs_by_horizon(out, 1) is None


def test_costs_by_horizon_none_for_untouched_row():
    assert costs_by_horizon(_row(), 1) is None
    assert state_of(_row(), 1) == ""


def test_horizon_key_matches_neighbour_scorecard_names():
    assert [horizon_key(h) for h in HORIZONS] == ["t1", "t5", "t20", "t60"]


# ── 重跑合并：已观测到的 ok 不可被降级 ──────────────────────────────
def _fwd(by_h: dict[int, str]) -> dict:
    """造一份 fwd：`{1: H_OK, 5: H_NOT_MATURED}` → `{"t1": {...}, "t5": {...}}`。"""
    return {
        horizon_key(h): ({"state": H_OK, "cost": 0.01} if st == H_OK else {"state": st})
        for h, st in by_h.items()
    }


def test_retry_downgrade_of_an_ok_horizon_is_refused():
    """今天的读盘失败不得抹掉昨天算出来的数——这是重跑最危险的失败模式。"""
    prev = _fwd({1: H_OK, 5: H_OK, 20: H_NOT_MATURED, 60: H_NOT_MATURED})
    new = _fwd({1: H_NO_DATA, 5: H_NOT_MATURED, 20: H_NOT_MATURED, 60: H_NOT_MATURED})
    merged, rejected = merge_fwd(prev, new)
    assert rejected == (1, 5)
    assert merged["t1"] == prev["t1"] and merged["t5"] == prev["t5"]
    assert costs_by_horizon(replace(_row(), fwd=merged), 1) is not None


def test_untradable_cannot_overwrite_an_ok_horizon_either():
    prev = _fwd({1: H_OK})
    merged, rejected = merge_fwd(prev, _fwd({1: H_UNTRADABLE}))
    assert rejected == (1,) and merged["t1"]["state"] == H_OK


def test_maturation_still_upgrades_not_matured_to_ok():
    """正向必须放行，否则影子账永远停在第一批能算的期上。"""
    merged, rejected = merge_fwd(_fwd({1: H_NOT_MATURED}), _fwd({1: H_OK}))
    assert rejected == () and merged["t1"]["state"] == H_OK


def test_backfilled_data_still_upgrades_no_data_to_ok():
    merged, rejected = merge_fwd(_fwd({5: H_NO_DATA}), _fwd({5: H_OK}))
    assert rejected == () and merged["t5"]["state"] == H_OK


def test_retried_may_replace_an_ok_horizon():
    """**唯一**的例外：`retried` 是关于单据的事实，晚到才发现正是常态。

    锁住 `ok` 会把一个本该剔除的样本永久留在分母里（拦截其实没改变结果）。
    """
    merged, rejected = merge_fwd(_fwd({1: H_OK}), _fwd({1: H_RETRIED}))
    assert rejected == ()
    assert merged["t1"]["state"] == H_RETRIED
    assert costs_by_horizon(replace(_row(), fwd=merged), 1) is None


def test_recompute_refreshes_the_values_of_an_ok_horizon():
    """前复权价会被除权改写，重算出的新值更当前——`ok → ok` 必须真的换值。"""
    prev = {"t1": {"state": H_OK, "cost": 0.01}}
    new = {"t1": {"state": H_OK, "cost": 0.02}}
    merged, rejected = merge_fwd(prev, new)
    assert rejected == () and merged["t1"]["cost"] == pytest.approx(0.02)


def test_merge_keeps_unknown_keys_written_by_others():
    """`fwd` 是 JSONB，别人往里加的键不能被这次合并吃掉。"""
    merged, _ = merge_fwd(None, {"t1": {"state": H_OK}, "probe": 1})
    assert merged["probe"] == 1


def test_price_row_monotone_does_not_erase_a_previous_ok():
    """重跑入口的端到端形态：一次失败的读盘 + 已存结果 → 已存的数还在。"""
    first = price_row(_row(), _full_input(), priced_at=PRICED_AT)
    assert costs_by_horizon(first, 1) is not None
    # 第二轮：日历没走到（entry_day 丢了）→ 若直接覆盖，t1 的 ok 会变成 no_data
    again, rejected = price_row_monotone(
        first, PriceInput(entry_px=None), priced_at="2026-09-25T08:00:00+08:00"
    )
    assert 1 in rejected
    assert costs_by_horizon(again, 1) == pytest.approx(costs_by_horizon(first, 1))
    assert again.priced_at == "2026-09-25T08:00:00+08:00", "定价时间必须刷新"


def test_price_row_monotone_still_records_first_time_rows():
    """没定过价的行（prev 为空）不受这条规则影响。"""
    out, rejected = price_row_monotone(_row(), PriceInput(), priced_at=PRICED_AT)
    assert rejected == () and out.fwd is not None
