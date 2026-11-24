"""未归属真单成交吸收（分账幻影仓修复）计划器测试。

背景：``agent_ledger_fill`` 纪律 1——``orders.agent`` 为空的成交（人工在桥里点的
单、风控单）不写分账账本。人工清仓后账本留下幻影仓，决策轮「已用额度」、实况页
agent 净值、arena 台账仍按幻影算。修复工具把这批成交按**真实成交价**吸收进账本。

计划器只做判定与分配（不碰 DB），本测试钉死四条保守判据：

1. 真实账户持仓 == 账本持仓 → 没有差额可吸收（``real_account_matches_ledger``）；
   有底仓但账本多出（部分人工卖出）按差额吸收，不再要求清仓；
2. 未归属卖出总量不足以覆盖**差额** → 不动（``unattributed_sells_insufficient``）；
3. 账本行的 ``buy_ts`` 不早于可分配卖出（含未知 buy_ts）→ 不动，卖出解释不了新仓；
4. 真实持仓读不到 → 整批不动（``real_positions_unavailable``）。

分配按 ``buy_ts`` FIFO：同一只票多家 agent 分摊一笔人工卖单时，先买的先出；
账本的 fill 唯一键是 ``(tenant,user,trade_date,fill_key)``（**不含 agent**），
所以键里必须带 agent——两家同持一只票时第二家的流水才不会被幂等守卫吞掉。
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from backend.shared.decision.manual_sell_absorption import (
    MANUAL_AGENT,
    REASON_BOUGHT_AFTER_SELL,
    REASON_BUY_TS_UNKNOWN,
    REASON_INSUFFICIENT,
    REASON_NO_GAP,
    REASON_NO_SELL_EVIDENCE,
    REASON_REAL_UNAVAILABLE,
    plan_absorption,
    plan_manual_flows,
    model_agent_names,
)


def _dt(day: int, hour: int, minute: int = 0, month: int = 10) -> datetime:
    """2026-<month>-<day> HH:MM UTC（测试里只用 UTC 时刻）。"""
    return datetime(2026, month, day, hour, minute, tzinfo=timezone.utc)


def _row(
    agent: str,
    code: str,
    volume: float,
    cost_price: float,
    buy_ts: datetime | None,
) -> dict:
    return {
        "agent": agent,
        "code": code,
        "volume": volume,
        "cost_price": cost_price,
        "buy_ts": buy_ts,
    }


def _sell(
    order_id: str,
    code: str,
    volume: float,
    price: float,
    executed_at: datetime | None = None,
    *,
    agent: str = "",
) -> dict:
    executed_at = executed_at or _dt(20, 5, 15)
    return {
        "order_id": order_id,
        "code": code,
        "volume": volume,
        "price": price,
        "executed_at": executed_at,
        "trade_date": executed_at.date(),
        "agent": agent,
    }


@pytest.mark.unit
def test_shares_one_manual_sell_between_two_agents_with_distinct_keys():
    """601668 实况：一笔人工卖单 200 股，flash/pro 各 100，键必须按 agent 区分。"""
    rows = [
        _row("deepseek-v4-flash", "601668.SH", 100, 4.54, _dt(19, 1, 35)),
        _row("deepseek-v4-pro", "601668.SH", 100, 4.54, _dt(19, 9, 0)),
    ]
    plan = plan_absorption(
        rows,
        [_sell("b8eb003d", "601668.SH", 200, 4.49)],
        {"601668.SH": 0},
    )

    assert not plan.skipped
    assert len(plan.fills) == 2
    assert {f.agent for f in plan.fills} == {"deepseek-v4-flash", "deepseek-v4-pro"}
    keys = [f.fill_key for f in plan.fills]
    assert keys == [
        "manual-absorb:b8eb003d:deepseek-v4-flash",
        "manual-absorb:b8eb003d:deepseek-v4-pro",
    ]
    assert len(set(keys)) == 2
    assert sum(f.volume for f in plan.fills) == pytest.approx(200)
    assert all(f.price == pytest.approx(4.49) for f in plan.fills)
    assert plan.proceeds_by_agent == {
        "deepseek-v4-flash": pytest.approx(449.00),
        "deepseek-v4-pro": pytest.approx(449.00),
    }
    # 已实现盈亏按账本成本价算（4.49 - 4.54）× 100
    assert plan.pnl_by_agent == {
        "deepseek-v4-flash": pytest.approx(-5.00),
        "deepseek-v4-pro": pytest.approx(-5.00),
    }


@pytest.mark.unit
def test_fifo_allocation_across_multiple_sell_orders():
    """一笔持仓由两笔人工卖单清掉：按成交时间先后拆成两条流水。"""
    rows = [_row("agent-a", "601000.SH", 300, 4.54, _dt(20, 3, 1))]
    sells = [
        _sell("ord-late", "601000.SH", 100, 4.80, _dt(20, 5, 17)),
        _sell("ord-early", "601000.SH", 200, 4.70, _dt(20, 5, 15)),
    ]
    plan = plan_absorption(rows, sells, {"601000.SH": 0})

    assert [f.order_id for f in plan.fills] == ["ord-early", "ord-late"]
    assert [f.volume for f in plan.fills] == [200, 100]
    assert plan.proceeds_by_agent == {"agent-a": pytest.approx(200 * 4.70 + 100 * 4.80)}


@pytest.mark.unit
def test_does_not_absorb_when_real_matches_ledger():
    """真实持仓 == 账本持仓 ⇒ 没有差额可吸收（原来「有底仓就跳过」的收敛形态）。"""
    rows = [_row("agent-a", "600760.SH", 100, 46.97, _dt(16, 3, 0))]
    plan = plan_absorption(
        rows, [_sell("ord-1", "600760.SH", 100, 47.0)], {"600760.SH": 100}
    )

    assert plan.fills == ()
    assert [s.reason for s in plan.skipped] == [REASON_NO_GAP]


@pytest.mark.unit
def test_absorbs_partial_position_difference():
    """有底仓也能吸收：账本 200 / 真实 100 ⇒ 只吸收差额 100（FIFO 先扣最早买入的）。"""
    rows = [
        _row("agent-early", "601825.SH", 120, 9.40, _dt(19, 1, 30)),
        _row("agent-late", "601825.SH", 80, 9.60, _dt(20, 1, 30)),
    ]
    plan = plan_absorption(
        rows,
        [_sell("ord-manual", "601825.SH", 100, 9.64, _dt(23, 5, 20))],
        {"601825.SH": 100},
    )

    assert not plan.skipped, plan.skipped
    assert [(f.agent, f.volume) for f in plan.fills] == [("agent-early", 100)]
    assert plan.fills[0].closes is False
    assert plan.proceeds_by_agent == {"agent-early": pytest.approx(964.00)}


@pytest.mark.unit
def test_partial_absorption_closes_exhausted_rows():
    """差额覆盖某家全部持仓时，该行 closes=True；未覆盖的行不动。"""
    rows = [
        _row("agent-early", "601825.SH", 100, 9.40, _dt(19, 1, 30)),
        _row("agent-late", "601825.SH", 100, 9.60, _dt(20, 1, 30)),
    ]
    plan = plan_absorption(
        rows,
        [_sell("ord-manual", "601825.SH", 100, 9.64, _dt(23, 5, 20))],
        {"601825.SH": 100},
    )

    assert [(f.agent, f.volume, f.closes) for f in plan.fills] == [
        ("agent-early", 100, True)
    ]


@pytest.mark.unit
def test_partial_absorption_needs_sells_covering_the_gap():
    """差额 100 但未归属卖出只有 60 ⇒ 卖据不足以解释差额，整只票不动。"""
    rows = [_row("agent-a", "601857.SH", 200, 12.33, _dt(19, 5, 0))]
    plan = plan_absorption(
        rows, [_sell("ord-1", "601857.SH", 60, 12.7)], {"601857.SH": 100}
    )

    assert plan.fills == ()
    assert [s.reason for s in plan.skipped] == [REASON_INSUFFICIENT]


@pytest.mark.unit
def test_skips_when_unattributed_sells_cannot_cover_ledger():
    rows = [_row("agent-a", "601857.SH", 200, 12.33, _dt(19, 5, 0))]
    plan = plan_absorption(
        rows, [_sell("ord-1", "601857.SH", 100, 12.7)], {"601857.SH": 0}
    )

    assert plan.fills == ()
    assert [s.reason for s in plan.skipped] == [REASON_INSUFFICIENT]
    assert "200" in plan.skipped[0].detail and "100" in plan.skipped[0].detail


@pytest.mark.unit
def test_skips_when_no_unattributed_sell_evidence():
    rows = [_row("agent-a", "601857.SH", 200, 12.33, _dt(19, 5, 0))]
    plan = plan_absorption(rows, [], {"601857.SH": 0})

    assert plan.fills == ()
    assert [s.reason for s in plan.skipped] == [REASON_NO_SELL_EVIDENCE]


@pytest.mark.unit
def test_skips_position_bought_after_the_manual_sell():
    """卖出之后才买的仓不由这笔卖出解释——整只票不动。"""
    rows = [
        _row("agent-a", "601985.SH", 100, 9.398, _dt(20, 3, 1)),
        _row("agent-b", "601985.SH", 100, 9.40, _dt(21, 1, 0)),  # 卖出后新买
    ]
    plan = plan_absorption(
        rows, [_sell("ord-1", "601985.SH", 200, 9.45)], {"601985.SH": 0}
    )

    assert plan.fills == ()
    assert [s.reason for s in plan.skipped] == [REASON_BOUGHT_AFTER_SELL]


@pytest.mark.unit
def test_skips_rows_without_buy_ts():
    rows = [_row("agent-a", "601985.SH", 100, 9.398, None)]
    plan = plan_absorption(
        rows, [_sell("ord-1", "601985.SH", 100, 9.45)], {"601985.SH": 0}
    )

    assert plan.fills == ()
    assert [s.reason for s in plan.skipped] == [REASON_BUY_TS_UNKNOWN]


@pytest.mark.unit
def test_real_positions_unavailable_blocks_everything():
    rows = [
        _row("agent-a", "601985.SH", 100, 9.398, _dt(20, 3, 1)),
        _row("agent-b", "601000.SH", 300, 4.54, _dt(20, 3, 1)),
    ]
    plan = plan_absorption(rows, [_sell("ord-1", "601985.SH", 100, 9.45)], None)

    assert plan.fills == ()
    assert {s.code for s in plan.skipped} == {"601985.SH", "601000.SH"}
    assert all(s.reason == REASON_REAL_UNAVAILABLE for s in plan.skipped)


@pytest.mark.unit
def test_attributed_sells_are_not_absorbed():
    """带 agent 的卖单是真单落账路径的活，吸收器不许碰。"""
    rows = [_row("agent-a", "601985.SH", 100, 9.398, _dt(20, 3, 1))]
    plan = plan_absorption(
        rows,
        [_sell("ord-1", "601985.SH", 100, 9.45, agent="deepseek-v4-flash")],
        {"601985.SH": 0},
    )

    assert plan.fills == ()
    assert [s.reason for s in plan.skipped] == [REASON_NO_SELL_EVIDENCE]


@pytest.mark.unit
def test_plan_is_independent_of_input_row_order():
    rows_a = [
        _row("agent-b", "601668.SH", 100, 4.54, _dt(19, 9, 0)),
        _row("agent-a", "601668.SH", 100, 4.54, _dt(19, 1, 35)),
    ]
    rows_b = list(reversed(rows_a))
    sells = [_sell("ord-1", "601668.SH", 200, 4.49)]

    plan_a = plan_absorption(rows_a, sells, {"601668.SH": 0})
    plan_b = plan_absorption(rows_b, sells, {"601668.SH": 0})

    assert [(f.agent, f.order_id, f.volume) for f in plan_a.fills] == [
        (f.agent, f.order_id, f.volume) for f in plan_b.fills
    ]
    # 先买先出：agent-a（10-19 01:35）拿第一段
    assert plan_a.fills[0].agent == "agent-a"


@pytest.mark.unit
def test_zero_volume_and_invalid_rows_are_ignored():
    rows = [
        _row("agent-a", "601985.SH", 0, 9.398, _dt(20, 3, 1)),
        _row("agent-a", "601000.SH", 200, 4.54, _dt(20, 3, 1)),
    ]
    plan = plan_absorption(
        rows,
        [
            _sell("ord-1", "601985.SH", 100, 9.45),
            _sell("ord-2", "601000.SH", 200, 0),  # 价格非法 → 不算证据
        ],
        {"601985.SH": 0, "601000.SH": 0},
    )

    assert plan.fills == ()
    # 0 股行不产生任何报告（它本就不该在账本里）；价格非法的卖出不算证据
    assert {s.code: s.reason for s in plan.skipped} == {
        "601000.SH": REASON_NO_SELL_EVIDENCE,
    }


@pytest.mark.unit
def test_trade_date_comes_from_sell_not_processing_day():
    rows = [_row("agent-a", "601985.SH", 100, 9.398, _dt(20, 3, 1))]
    sell = _sell("ord-1", "601985.SH", 100, 9.45, _dt(20, 5, 15))
    plan = plan_absorption(rows, [sell], {"601985.SH": 0})

    assert plan.fills[0].trade_date == date(2026, 10, 20)
    assert plan.fills[0].filled_at == _dt(20, 5, 15)


# --- plan_manual_flows：人工买入 + 先卖后买 + 期初差异 ---------------------


def _buy(
    order_id: str,
    code: str,
    volume: float,
    price: float,
    executed_at: datetime | None = None,
) -> dict:
    executed_at = executed_at or _dt(23, 1, 21)
    return {
        "order_id": order_id,
        "code": code,
        "volume": volume,
        "price": price,
        "executed_at": executed_at,
        "trade_date": executed_at.date(),
        "agent": "",
    }


@pytest.mark.unit
def test_manual_flow_handles_sell_then_buy_same_day():
    """2026-11-23 601825 实况：人工先卖 4500 再买 93300，期末真实 94300。

    只看期末差额会把卖出吞掉（账本 5500 < 真实 94300）；按事件顺序必须
    先 FIFO 扣模型 4500，再把 93300 记到 manual。
    """
    rows = [
        _row("deepseek-v4-flash", "601825.SH", 3000, 9.40, _dt(18, 1, 30)),
        _row("deepseek-v4-pro", "601825.SH", 1400, 9.50, _dt(18, 2, 30)),
        _row("glm-5.3-flash", "601825.SH", 1100, 9.60, _dt(19, 1, 30)),
    ]
    plan = plan_manual_flows(
        rows,
        [_buy("b1", "601825.SH", 93300, 9.627)],
        [_sell("s1", "601825.SH", 4500, 9.64, _dt(23, 1, 20))],
        {"601825.SH": 94300},
    )

    assert not plan.skipped, plan.skipped
    sells = [f for f in plan.fills if f.side == "sell"]
    buys = [f for f in plan.fills if f.side == "buy"]
    assert [(f.agent, f.volume, f.closes) for f in sells] == [
        ("deepseek-v4-flash", 3000, True),
        ("deepseek-v4-pro", 1400, True),
        ("glm-5.3-flash", 100, False),
    ]
    assert [(f.agent, f.volume) for f in buys] == [(MANUAL_AGENT, 93300)]
    assert buys[0].fill_key == "manual-buy:b1:manual"
    # 期末账本合计 == 真实持仓
    residual = 1100 - 100 + 93300
    assert residual == 94300


@pytest.mark.unit
def test_manual_flow_merges_same_order_multi_leg_sell():
    """2026-11-24 601825 实况：一笔卖出 300 跨多段持仓时，fill_key 必须只出现一次。

    期初补 200 买入后，卖 300 会被 FIFO 切成 100+200 两段（同一 agent/订单）；
    若不合并，apply_fill 在第二段判重复、整批回滚（实测每分钟失败一次）。
    """
    rows = [_row(MANUAL_AGENT, "601825.SH", 100, 9.6, _dt(18, 1, 30, month=11))]
    plan = plan_manual_flows(
        rows,
        [],
        [_sell("s-big", "601825.SH", 300, 9.5, _dt(24, 1, 20, month=11))],
        {"601825.SH": 0},
    )

    assert not plan.skipped, plan.skipped
    buys = [f for f in plan.fills if f.side == "buy"]
    sells = [f for f in plan.fills if f.side == "sell"]
    assert [(f.agent, f.volume) for f in buys] == [(MANUAL_AGENT, 200)]
    assert len(sells) == 1, sells
    assert sells[0].volume == 300
    keys = [f.fill_key for f in plan.fills]
    assert len(keys) == len(set(keys))


@pytest.mark.unit
def test_manual_flow_adjusts_ledger_excess_at_window_start():
    """期初账本比真实多 100 ⇒ 先记一笔卖出调整，再处理窗口内成交。"""
    rows = [_row("agent-a", "600760.SH", 300, 46.97, _dt(19, 3, 0))]
    plan = plan_manual_flows(rows, [], [], {"600760.SH": 200})

    assert not plan.skipped, plan.skipped
    assert [(f.side, f.agent, f.volume) for f in plan.fills] == [
        ("sell", "agent-a", 100)
    ]
    assert plan.fills[0].fill_key.startswith("manual-adjust:")


@pytest.mark.unit
def test_manual_flow_adjusts_ledger_shortfall_at_window_start():
    """期初账本比真实少 100 ⇒ 补一笔 manual 买入（真实账户历史人工买入）。"""
    rows = [_row("agent-a", "600760.SH", 100, 46.97, _dt(19, 3, 0))]
    plan = plan_manual_flows(rows, [], [], {"600760.SH": 200})

    assert not plan.skipped, plan.skipped
    assert [(f.side, f.agent, f.volume) for f in plan.fills] == [
        ("buy", MANUAL_AGENT, 100)
    ]


@pytest.mark.unit
def test_manual_flow_skips_when_start_gap_has_no_reference_price():
    """期初账本比真实少、又没有买入成交/成本价可参考 ⇒ 只报告不写（不臆造价格）。"""
    rows = [_row("agent-a", "600760.SH", 100, 0.0, _dt(19, 3, 0))]
    plan = plan_manual_flows(rows, [], [], {"600760.SH": 200})

    assert plan.fills == ()
    assert [s.reason for s in plan.skipped] == [REASON_INSUFFICIENT]


@pytest.mark.unit
def test_model_agent_names_excludes_manual_bucket():
    """赛马总控/净值曲线只列模型：manual 是对账桶，不参与排名。"""
    names = model_agent_names(
        {"deepseek-v4-flash": {}, "manual": {}, "glm-5.3-flash": {}, " ": {}}
    )

    assert names == ["deepseek-v4-flash", "glm-5.3-flash"]
    assert model_agent_names(None) == []
    assert model_agent_names({}) == []
