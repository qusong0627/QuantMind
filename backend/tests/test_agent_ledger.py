"""P2.7 分账账本纯核心单测（TDD 先红后绿）。

语义基线是隔壁第一手读码（见 `docs/local/quant-trader-migration-plan.md` 的
「P2.7 语义基线」表），本文件把那条基线钉成可执行的断言。**三处刻意与隔壁不同**，
每处都有单测点名（下列 `test_divergence_*`）：
① 卖出量按持有量夹取后再记现金（隔壁用未夹取的量记现金，量不足时多记现金）；
② 超额卖出 / 卖非持仓**必须留痕**（隔壁 `return ledger` 静默原样返回）；
③ 金额非有限/非正一律拒（隔壁只对成交价做了 40% 坏价闸）。
"""

from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

import pytest

from backend.shared.decision.agent_ledger import (
    DEFAULT_AGENT_QUOTA,
    LedgerChange,
    agent_cash,
    agent_positions,
    agent_remaining,
    agent_used,
    ensure_agent,
    fill_delta,
    holding_days,
    mine_of,
    position_cost,
    recorded_baseline,
    record_buy,
    record_sell,
    sane_fill_price,
)

TS1 = datetime(2026, 9, 11, 1, 37, 26, tzinfo=timezone.utc)
TS2 = datetime(2026, 9, 14, 2, 23, 2, tzinfo=timezone.utc)


def _empty():
    return {"version": 1, "agents": {}}


def _buy(led, agent="m-a", code="600036.SH", vol=100, px=30.0, ts=TS1, quota=100_000.0):
    """便捷买入（返回新账本）；记账被拒时直接断言失败，免得测试在脏账上继续跑。"""
    ch = record_buy(led, agent, code, vol, px, ts, quota=quota)
    assert not ch.note, ch.note
    return ch.ledger


def _held(agent="m-a", vol=400, px=30.0, quota=100_000.0):
    return _buy(ensure_agent(_empty(), agent, quota=quota), agent, vol=vol, px=px)


# --- 账户读侧 ---------------------------------------------------------------


def test_ensure_agent_creates_with_full_quota():
    led = ensure_agent(_empty(), "m-a", quota=1000.0)
    assert agent_cash(led, "m-a", quota=1000.0) == 1000.0
    assert agent_positions(led, "m-a") == {}


def test_ensure_agent_does_not_reset_existing_cash():
    led = _buy(
        ensure_agent(_empty(), "m-a", quota=1000.0), vol=100, px=10.0, quota=1000.0
    )
    again = ensure_agent(led, "m-a", quota=1000.0)
    assert agent_cash(again, "m-a", quota=1000.0) == 0.0


def test_missing_cash_key_falls_back_to_quota_but_zero_is_kept():
    """键缺失回退 quota；**恰好为 0 也是合法现金**（隔壁 2026-09-18 评审 L-5 的同款坑）。"""
    led = {"version": 1, "agents": {"m-a": {"positions": {}}}}
    assert agent_cash(led, "m-a", quota=500.0) == 500.0
    led = {"version": 1, "agents": {"m-a": {"positions": {}, "virtual_cash": 0.0}}}
    assert agent_cash(led, "m-a", quota=500.0) == 0.0


def test_positions_tolerates_broken_top_level():
    for bad in (None, [], "x", {"agents": None}, {"agents": {"m-a": None}}):
        assert agent_positions(bad, "m-a") == {}


def test_used_is_current_cost_not_cumulative_buy_amount():
    """`used` = **现持仓成本**：卖光即归零（不是累计买入额）。"""
    led = _held(vol=100)
    assert agent_used(led, "m-a") == 3000.0
    led = record_sell(led, "m-a", "600036.SH", 100, 31.0, TS2, quota=100_000.0).ledger
    assert agent_used(led, "m-a") == 0.0
    assert agent_remaining(led, "m-a", quota=100_000.0) == 100_000.0


def test_remaining_can_go_negative_and_is_not_clamped():
    """额度超用要如实为负——夹成 0 会把「这条线已透支」读成「刚好用满」。"""
    led = _buy(
        ensure_agent(_empty(), "m-a", quota=1000.0), vol=100, px=30.0, quota=1000.0
    )
    assert agent_remaining(led, "m-a", quota=1000.0) == pytest.approx(-2000.0)


def test_position_cost_of_single_holding():
    led = _held(vol=400, px=30.0)
    assert position_cost(led, "m-a", "600036.SH") == pytest.approx(12_000.0)
    assert position_cost(led, "m-a", "000001.SZ") == 0.0


# --- 买入 -------------------------------------------------------------------


def test_record_buy_sets_cost_timestamps_and_decrements_cash():
    ch = record_buy(
        ensure_agent(_empty(), "m-a"),
        "m-a",
        "600036.SH",
        100,
        30.0,
        TS1,
        quota=100_000.0,
    )
    assert ch.applied == 100 and not ch.note
    pos = agent_positions(ch.ledger, "m-a")["600036.SH"]
    assert pos["volume"] == 100
    assert pos["cost_price"] == 30.0
    assert pos["buy_ts"] == "2026-09-11T01:37:26Z"
    assert pos["last_ts"] == "2026-09-11T01:37:26Z"
    assert agent_cash(ch.ledger, "m-a", quota=100_000.0) == pytest.approx(97_000.0)


def test_record_buy_add_is_weighted_average_and_keeps_buy_ts():
    led = _buy(ensure_agent(_empty(), "m-a"), vol=100, px=30.0)
    led = _buy(led, vol=300, px=34.0, ts=TS2)
    pos = agent_positions(led, "m-a")["600036.SH"]
    assert pos["volume"] == 400
    assert pos["cost_price"] == pytest.approx(33.0)  # (100*30+300*34)/400
    assert pos["buy_ts"] == "2026-09-11T01:37:26Z"  # 首次买入时间不动
    assert pos["last_ts"] == "2026-09-14T02:23:02Z"
    assert agent_cash(led, "m-a", quota=100_000.0) == pytest.approx(
        100_000.0 - 3_000.0 - 10_200.0
    )


def test_record_buy_is_immutable():
    led = ensure_agent(_empty(), "m-a", quota=100_000.0)
    before = repr(led)
    record_buy(led, "m-a", "600036.SH", 100, 30.0, TS1, quota=100_000.0)
    assert repr(led) == before


# --- 卖出 -------------------------------------------------------------------


def test_record_sell_partial_keeps_position_and_credits_cash():
    ch = record_sell(_held(), "m-a", "600036.SH", 100, 31.0, TS2, quota=100_000.0)
    assert ch.applied == 100 and not ch.note
    pos = agent_positions(ch.ledger, "m-a")["600036.SH"]
    assert pos["volume"] == 300
    assert pos["cost_price"] == 30.0  # 减仓不动余仓成本
    assert pos["last_ts"] == "2026-09-14T02:23:02Z"
    assert pos["buy_ts"] == "2026-09-11T01:37:26Z"
    assert agent_cash(ch.ledger, "m-a", quota=100_000.0) == pytest.approx(
        100_000.0 - 12_000.0 + 3_100.0
    )


def test_record_sell_full_closes_position():
    ch = record_sell(_held(), "m-a", "600036.SH", 400, 31.0, TS2, quota=100_000.0)
    assert ch.applied == 400
    assert "600036.SH" not in agent_positions(ch.ledger, "m-a")


def test_record_sell_roundtrip_row_is_produced():
    """回合台账（影子账户/行为归因的底座）：闭合标记与已实现盈亏。"""
    ch = record_sell(_held(), "m-a", "600036.SH", 100, 31.0, TS2, quota=100_000.0)
    rt = ch.ledger["roundtrips"][-1]
    assert rt["agent"] == "m-a" and rt["code"] == "600036.SH"
    assert rt["volume"] == 100 and rt["closed"] is False
    assert rt["market"] == "CN"
    assert rt["realized_pnl"] == pytest.approx(100.0)
    assert rt["pnl_pct"] == pytest.approx(3.333, abs=1e-3)
    assert rt["buy_ts"] == "2026-09-11T01:37:26Z"
    assert rt["sell_ts"] == "2026-09-14T02:23:02Z"
    assert rt["holding_days"] == pytest.approx(3.031, abs=1e-2)


def test_divergence_sell_not_held_leaves_note_not_silence():
    """**与隔壁不同②**：卖非持仓不静默。

    隔壁 `record_sell` 在 `code not in pos` 时 `return ledger`——调用方拿到的是
    一本**看起来正常的账**，「这次卖出没记上」这件事在账本里没有任何痕迹。
    分账账本漂移（柜台卖了、台账没扣）正是最难查的一类事故，故本仓必须留痕。
    """
    led = ensure_agent(_empty(), "m-a", quota=100_000.0)
    ch = record_sell(led, "m-a", "600036.SH", 100, 31.0, TS2, quota=100_000.0)
    assert ch.applied == 0
    assert "600036.SH" in ch.note and "无持仓" in ch.note
    assert agent_positions(ch.ledger, "m-a") == {}
    assert agent_cash(ch.ledger, "m-a", quota=100_000.0) == 100_000.0  # 现金不动
    assert "roundtrips" not in ch.ledger  # 没记上就没有回合记录


def test_divergence_oversell_clamps_cash_to_held_volume():
    """**与隔壁不同①**：超额卖出按持有量夹取后记现金。

    隔壁 `record_sell` 用**未夹取**的 `volume` 记现金、却按夹取量删持仓
    （`scripts/account_protocol.py:169-196`）——持有 100 卖 500 时，持仓删掉、
    现金却多进 400 股的钱。隔壁在 `live_fills.py:163` 的注释里承认了这点
    （「record_sell 量不足时直接删持仓 + 多记现金」），但选择在**调用点**绕开。
    分账账本是资金口径的账，函数自己必须夹。
    """
    ch = record_sell(_held(), "m-a", "600036.SH", 500, 31.0, TS2, quota=100_000.0)
    assert ch.applied == 400  # 只记实际持有的
    assert "500" in ch.note and "400" in ch.note  # 缺口点名
    assert "600036.SH" not in agent_positions(ch.ledger, "m-a")
    assert agent_cash(ch.ledger, "m-a", quota=100_000.0) == pytest.approx(
        100_000.0 - 12_000.0 + 400 * 31.0
    )


def test_divergence_negative_or_nan_amounts_are_rejected():
    """**与隔壁不同③**：金额/数量非有限或非正一律拒，不许进账本。"""
    led = _held()
    for bad in (float("nan"), float("inf"), -1.0, 0.0):
        ch = record_buy(led, "m-a", "000001.SZ", 100, bad, TS1, quota=100_000.0)
        assert ch.applied == 0 and "价" in ch.note, bad
        ch = record_buy(led, "m-a", "000001.SZ", bad, 10.0, TS1, quota=100_000.0)
        assert ch.applied == 0 and "量" in ch.note, bad
        ch = record_sell(led, "m-a", "600036.SH", 100, bad, TS2, quota=100_000.0)
        assert ch.applied == 0 and "价" in ch.note, bad


def test_rejected_buy_returns_ledger_unchanged():
    led = _held()
    ch = record_buy(led, "m-a", "000001.SZ", 100, float("nan"), TS1, quota=1.0)
    assert ch.ledger is led and ch.applied == 0


def test_rejected_code_is_empty():
    ch = record_buy(_held(), "m-a", "  ", 100, 10.0, TS1, quota=100_000.0)
    assert ch.applied == 0 and "代码" in ch.note


# --- 坏价闸（逐字移植隔壁实测口径） -----------------------------------------


def test_sane_fill_price_keeps_normal_tick():
    assert sane_fill_price(10.05, 10.0) == (10.05, False)


def test_sane_fill_price_flags_wild_tick_and_returns_ref():
    """2026-09-08 实录：001312 桥报成交价 4.789 vs 实时 17.5（虚增 pro 净值 ~1.4 万）。"""
    assert sane_fill_price(4.789, 17.5) == (17.5, True)


def test_sane_fill_price_boundary_is_exclusive():
    """±40% 本身不算坏 tick（隔壁是 `0.6 <= ratio <= 1.4`）。"""
    assert sane_fill_price(14.0, 10.0) == (14.0, False)
    assert sane_fill_price(6.0, 10.0) == (6.0, False)
    assert sane_fill_price(6.0 - 1e-9, 10.0)[1] is True


def test_sane_fill_price_non_positive_ref_is_not_judged():
    assert sane_fill_price(10.0, 0.0) == (10.0, False)


# --- 互卖防线（P2.7 的核心那一行） ------------------------------------------


def test_mine_of_is_exactly_the_agents_positions():
    led = _buy(_held(), agent="m-b", code="000001.SZ", vol=200, px=12.0)
    assert mine_of(led, "m-a") == frozenset({"600036.SH"})
    assert mine_of(led, "m-b") == frozenset({"000001.SZ"})
    assert mine_of(led, "m-c") == frozenset()


def test_one_agent_selling_another_agents_stock_does_not_touch_the_ledger():
    """2026-09-08 隔壁事故的可执行复现：pro 卖 flash 的生益电子。

    提示词层（`mine` 裁剪）先让它看不见；真发生到这里时，**钱不能动**。
    """
    led = _buy(_held(), agent="m-b", code="002074.SZ", vol=200, px=25.44)
    ch = record_sell(led, "m-b", "600036.SH", 100, 31.0, TS2, quota=100_000.0)
    assert ch.applied == 0 and "无持仓" in ch.note
    assert agent_positions(ch.ledger, "m-a")["600036.SH"]["volume"] == 400
    assert mine_of(ch.ledger, "m-b") == frozenset({"002074.SZ"})
    assert agent_cash(ch.ledger, "m-b", quota=100_000.0) == pytest.approx(
        100_000.0 - 200 * 25.44
    )


# --- 成交补记的幂等算术（唯一键在 store 层，算术在这里） ---------------------


def test_recorded_baseline_takes_max_of_both_sides():
    """两侧取大：save 崩溃后 pending 落后也不会把同一笔成交再记一次。"""
    assert recorded_baseline(pending_recorded=100, applied_filled=200) == 200
    assert recorded_baseline(pending_recorded=200, applied_filled=100) == 200


def test_recorded_baseline_ignores_other_days_marker():
    """**只认当日标记**：委托号每日重排，昨天的同号会把今天的新单吞掉。"""
    assert (
        recorded_baseline(
            pending_recorded=0,
            applied_filled=500,
            marker_ts="2026-09-11",
            today="2026-09-14",
        )
        == 0
    )
    assert (
        recorded_baseline(
            pending_recorded=0,
            applied_filled=500,
            marker_ts="2026-09-14",
            today="2026-09-14",
        )
        == 500
    )


def test_fill_delta_never_goes_negative():
    assert fill_delta(filled=300, recorded=100) == 200
    assert fill_delta(filled=100, recorded=300) == 0
    assert fill_delta(filled=100, recorded=100) == 0


def test_recorded_baseline_tolerates_garbage():
    assert recorded_baseline(pending_recorded=None, applied_filled="x") == 0
    assert recorded_baseline(pending_recorded="12", applied_filled=None) == 12


def test_holding_days_parses_z_suffix_on_py310():
    """`to_utc_iso` 写的是 `Z` 结尾，而 `fromisoformat` 认识 `Z` 是 3.11 的事。

    本仓主栈是 3.10——不特殊处理的话，本仓自己写出来的时间戳全部解析失败，
    持仓天数恒为 None 且**毫无报错**（正好是回合台账最需要的一个字段）。
    """
    assert holding_days("2026-09-11T01:37:26Z", TS2) == pytest.approx(3.031, abs=1e-2)
    assert holding_days("2026-09-11T09:37:26+08:00", TS2) == pytest.approx(
        3.031, abs=1e-2
    )
    assert holding_days("不是时间", TS2) is None
    assert holding_days(None, TS2) is None


# --- 常量与返回类型 ---------------------------------------------------------


def test_default_quota_matches_neighbor():
    """¥10 万/agent 是隔壁 `AGENT_QUOTA` 的取值（`scripts/live_ledger.py:33`）。"""
    assert DEFAULT_AGENT_QUOTA == 100_000.0


def test_ledger_change_is_frozen():
    ch = LedgerChange(ledger=_empty())
    with pytest.raises(FrozenInstanceError):
        ch.applied = 1  # type: ignore[misc]
