"""P2.3b 执行段：**决策 → 腿计划 + 否决记录**（纯核心）。

这一层回答的问题与 ``gates`` 不同：``gates`` 判「该不该买」，本层判「能不能变成一张
单、多少股、什么价」。因此大部分用例不是「算出多少钱」，而是**下不出去的分支要留痕、
且不许静默消失**——真线上一天只碰一次的路径（卖出非持仓、T+1 可卖量为 0、跌停不接、
在途重复），在这里是构造几个对象的事。

六类断言：

1. **卖出的死路必须有回执**：非持仓 / 可卖量 0 / 脏比例 / 跌停 —— 每条都有稳定 rule id，
   且**都不出腿**（「模型让卖、系统静默不卖」是本层要消灭的事故）；
2. **缺比例的买卖侧方向相反**：买入缺比例 = 不可执行；卖出缺比例 = **按清仓**
   （方向已由模型给出，比例只是幅度）；
3. **数量口径不复制**：整手/碎股/科创递增一律走 ``lot_rules`` / ``push_plan``，
   本文件只钉**调用结果**，不重算板别；
4. **在途与同轮重复**：两类来源给出**不同理由**（排查方向不同）；
5. **过闸即计数**：本轮新开仓上限按「过闸」计，不按「出腿」计（与隔壁同口径）；
6. **纯函数**：同入参同结果，且不改写入参（holdings/quotes 是调用点的账）。
"""

from __future__ import annotations

import pytest

from backend.shared.decision import execution as ex
from backend.shared.decision.contract import (
    BUY,
    HOLD,
    PCT_DIRTY,
    PCT_GIVEN,
    SELL,
    STATUS_OK,
    WATCH,
    Decision,
    DecisionBatch,
    Pct,
)
from backend.shared.decision.execution import (
    LIMIT_SLIP,
    Holding,
    Quote,
    at_limit_down,
    inflight_dup,
    no_quote,
    plan_orders,
    sell_not_held,
)
from backend.shared.decision.gates import BuyGate

# ---------------------------------------------------------------------------
# 构造器（用例读起来要像在说交易场景，不是像在摆数据结构）
# ---------------------------------------------------------------------------

_KLINE_PRICE = 100.0


def _batch(*decisions: Decision) -> DecisionBatch:
    return DecisionBatch(status=STATUS_OK, schema="intraday", decisions=decisions)


def _buy(code: str, pct: float | None = 0.2, **kw: object) -> Decision:
    return Decision(
        action=BUY,
        code=code,
        pct=Pct(pct or 0.0, PCT_GIVEN if pct else "missing"),
        reason=str(kw.pop("reason", "补仓")),
        **kw,  # type: ignore[arg-type]
    )


def _sell(code: str, pct: float | None = 0.5, **kw: object) -> Decision:
    return Decision(
        action=SELL,
        code=code,
        pct=Pct(pct or 0.0, PCT_GIVEN if pct else "missing"),
        reason=str(kw.pop("reason", "减仓")),
        **kw,  # type: ignore[arg-type]
    )


def _held(code: str, available: float = 1000.0) -> tuple[str, Holding]:
    return code, Holding(symbol=code, available=available, name="测试")


def _quote(code: str, price: float = _KLINE_PRICE, **kw: object) -> tuple[str, Quote]:
    return code, Quote(symbol=code, price=price, **kw)  # type: ignore[arg-type]


def _gate(*codes: str, **kw: object) -> BuyGate:
    return BuyGate(pool_codes=frozenset(codes), **kw)  # type: ignore[arg-type]


def _rule_ids(plan: ex.ExecutionPlan) -> list[str]:
    return [v.rule for v in plan.vetoes]


# ---------------------------------------------------------------------------
# 1. 四条判据本体（登记表的 `where` 就指到这里）
# ---------------------------------------------------------------------------


def test_sell_not_held_distinguishes_missing_account_from_wrong_book() -> None:
    book = dict([_held("600036.SH")])
    assert sell_not_held("600036.SH", book) is False
    assert sell_not_held("600519.SH", book) is True
    # 空码（模型没给标的）也算「本账里没有它」——它连问题都问不出来
    assert sell_not_held("", book) is True


@pytest.mark.parametrize(
    "price", [None, 0, -1.0, float("nan"), float("inf"), "x", True]
)
def test_no_quote_treats_every_unusable_price_as_missing(price: object) -> None:
    """缺失/0/负/NaN/Inf/非数/**布尔**一律算「没有价」。

    ``True`` 必须归「没有价」：``float(True) == 1.0`` 会让 ``"price": true``
    静默变成「现价 1 元」，于是按 1 元一路算股数与限价。
    """
    assert no_quote(price) is True  # type: ignore[arg-type]


def test_no_quote_accepts_a_real_price() -> None:
    assert no_quote(10.0) is False
    assert no_quote("10.0") is False  # type: ignore[arg-type]


def test_inflight_dup_covers_both_the_round_and_the_open_orders() -> None:
    placed = {("600036.SH", SELL)}
    assert inflight_dup("600036.SH", SELL, inflight=frozenset(), placed=placed) is True
    assert (
        inflight_dup(
            "600036.SH", SELL, inflight=frozenset({("600036.SH", SELL)}), placed=set()
        )
        is True
    )
    # 方向不同不算重复：同一只票先卖出再买回是合法的调仓
    assert (
        inflight_dup(
            "600036.SH", BUY, inflight=frozenset({("600036.SH", SELL)}), placed=set()
        )
        is False
    )


def test_at_limit_down_uses_inclusive_comparison() -> None:
    assert at_limit_down(-0.10, 0.10) is True  # 恰好到板
    assert at_limit_down(-0.11, 0.10) is True
    assert at_limit_down(-0.05, 0.10) is False


@pytest.mark.parametrize(
    ("chg", "thr"),
    [
        (None, 0.10),  # 跌幅未知
        (-0.10, None),  # 阈值未知
        (-0.10, 0),  # 阈值 0 = 配置缺失，不是「不设涨跌停」
        (-0.10, -0.10),  # 负阈值同理
        (float("nan"), 0.10),
        (-0.10, float("inf")),
    ],
)
def test_at_limit_down_does_not_guess_when_a_number_is_missing(
    chg: float | None, thr: float | None
) -> None:
    """缺一**不判**：猜一个阈值拦下的是一整批本该卖出的单（不可逆的方向）。"""
    assert at_limit_down(chg, thr) is False


def test_at_limit_down_refuses_percentage_points_instead_of_going_silent() -> None:
    """阈值写成百分点（9.8）→ **报错**，不静默。

    A 股最大跌幅 20%：``thr > 1`` 时 ``chg <= -thr`` 恒假 ⇒ 跌停腿一条都不拦，
    而账面（否决数、影子账）看不出任何异常——静默失效的闸门比报错的闸门危险得多。
    与 ``gates.check_limit_reach`` 同口径（那里也 raise）。
    """
    with pytest.raises(ValueError, match="比例区间"):
        at_limit_down(-0.10, 9.8)


def test_sell_limit_down_unit_error_is_not_swallowed_into_a_pass() -> None:
    """计划层不吞这个异常：整轮不出腿（看得见的停）好过静默把跌停单卖出去。"""
    with pytest.raises(ValueError, match="比例区间"):
        plan_orders(
            _batch(_sell("600036.SH", 1.0)),
            holdings=dict([_held("600036.SH", available=1000)]),
            quotes=dict(
                [_quote("600036.SH", day_chg_ratio=-0.10, limit_threshold_ratio=9.8)]
            ),
        )


# ---------------------------------------------------------------------------
# 2. 卖出：每条死路都要有回执
# ---------------------------------------------------------------------------


def test_sell_of_a_symbol_outside_this_book_is_vetoed() -> None:
    """多模型分账下这条是**跨 agent 卖仓**的防线——不是「没持仓就算了」。"""
    plan = plan_orders(
        _batch(_sell("600519.SH")),
        holdings=dict([_held("600036.SH")]),
        quotes=dict([_quote("600519.SH")]),
    )
    assert plan.legs == ()
    (v,) = plan.vetoes
    assert v.rule == ex.RULE_SELL_NOT_HELD
    assert v.symbol == "600519.SH"
    assert "不在本账持仓内" in v.reason


def test_sell_without_a_code_keeps_the_raw_value_as_evidence() -> None:
    """模型没给代码：记在 ``sell_not_held`` 下，但**原始值要留痕**（否则无从排查）。"""
    plan = plan_orders(_batch(Decision(action=SELL, code="", reason="跑了")))
    (v,) = plan.vetoes
    assert v.rule == ex.RULE_SELL_NOT_HELD
    assert dict(v.evidence)["code"] == ""


def test_sell_holdings_keys_are_normalised_both_ways() -> None:
    """库里存前缀式、模型吐裸码（或反过来）都不许漏判——否则就是「有持仓却说没持仓」。"""
    for book_key, code in (("SH600036", "600036.SH"), ("600036.SH", "SH600036")):
        plan = plan_orders(
            _batch(_sell(code, 1.0)),
            holdings=dict([_held(book_key)]),
            quotes=dict([_quote("600036.SH")]),
        )
        assert plan.vetoes == (), f"{book_key} / {code} 形态归一失败"
        assert plan.sells[0].symbol == "600036.SH"


def test_zero_available_is_vetoed_under_t1_not_silently_skipped() -> None:
    """T+1 锁定 / 已被挂单占用：这一轮下不出去，但**必须留一条可聚合的记录**。"""
    plan = plan_orders(
        _batch(_sell("600036.SH", 1.0)),
        holdings=dict([_held("600036.SH", available=0)]),
        quotes=dict([_quote("600036.SH")]),
    )
    assert plan.legs == ()
    (v,) = plan.vetoes
    assert v.rule == "l1.t1_sellable"
    assert dict(v.evidence)["available"] == 0.0


def test_dirty_sell_pct_stops_instead_of_clearing_the_position() -> None:
    """**本层最重要的一个分支**：把「想减 30%」执行成清仓是不可逆的方向放大。

    模型给了读不出的值（``"0.3股"``）时：停手 + 留痕，下一轮再决策。
    """
    plan = plan_orders(
        _batch(
            Decision(
                action=SELL, code="600036.SH", pct=Pct(0.0, PCT_DIRTY, repr("0.3股"))
            )
        ),
        holdings=dict([_held("600036.SH", available=1000)]),
        quotes=dict([_quote("600036.SH")]),
    )
    assert plan.legs == ()
    (v,) = plan.vetoes
    assert v.rule == "l2.pct_invalid"
    assert dict(v.evidence)["pct_raw"] == "'0.3股'"
    assert "停手留痕" in v.reason


def test_missing_sell_pct_is_read_as_clear_out() -> None:
    """模型明说卖、只是漏了比例 → **按清仓**（与 contract 同口径）。

    与买入侧刻意相反：卖出方向已由模型给出，比例只是幅度。缺幅度时按 0 股处理，
    等于「模型让卖、系统静默不卖」。
    """
    plan = plan_orders(
        _batch(_sell("600036.SH", pct=None)),
        holdings=dict([_held("600036.SH", available=1000)]),
        quotes=dict([_quote("600036.SH")]),
    )
    (leg,) = plan.sells
    assert leg.quantity == 1000
    assert any("未给比例，按清仓" in n for n in plan.notes)


def test_sell_pct_zero_is_a_noop_not_a_veto() -> None:
    """明说 0 / 负值 = **模型说了不做**：进 ``noops``，不进代价账（那会给规则记假成本）。"""
    for pct in (0.0, -1.0):
        plan = plan_orders(
            _batch(Decision(action=SELL, code="600036.SH", pct=Pct(pct, PCT_GIVEN))),
            holdings=dict([_held("600036.SH", available=1000)]),
            quotes=dict([_quote("600036.SH")]),
        )
        assert plan.legs == () and plan.vetoes == ()
        assert plan.noops == ("600036.SH",)


def test_partial_sell_aligns_to_the_nearest_lot() -> None:
    """600 股 × 33% = 199 股 → 200（**最近整手**，不是地板 100——隔壁 09-08 实录）。"""
    plan = plan_orders(
        _batch(_sell("600036.SH", 0.33)),
        holdings=dict([_held("600036.SH", available=600)]),
        quotes=dict([_quote("600036.SH")]),
    )
    (leg,) = plan.sells
    assert leg.quantity == 200
    assert "整手对齐" in leg.note


def test_sell_leaving_an_odd_lot_clears_the_whole_position() -> None:
    """卖完剩碎股（< 1 手）→ 一次性全清：否则那几十股之后卖不掉。"""
    plan = plan_orders(
        _batch(_sell("600036.SH", 0.9)),
        holdings=dict([_held("600036.SH", available=650)]),
        quotes=dict([_quote("600036.SH")]),
    )
    (leg,) = plan.sells
    assert leg.quantity == 650
    assert "碎股" in leg.note


def test_star_board_sell_increments_by_one_share_not_by_a_lot() -> None:
    """科创板 1 股递增：333 股原样报出去，不被凑成 300/400。"""
    plan = plan_orders(
        _batch(_sell("688111.SH", 0.333)),
        holdings=dict([_held("688111.SH", available=1000)]),
        quotes=dict([_quote("688111.SH")]),
    )
    (leg,) = plan.sells
    assert leg.quantity == 333


def test_sell_at_limit_down_is_vetoed_with_its_numbers() -> None:
    plan = plan_orders(
        _batch(_sell("600036.SH", 1.0)),
        holdings=dict([_held("600036.SH", available=1000)]),
        quotes=dict(
            [_quote("600036.SH", day_chg_ratio=-0.10, limit_threshold_ratio=0.10)]
        ),
    )
    assert plan.legs == ()
    (v,) = plan.vetoes
    assert v.rule == ex.RULE_SELL_LIMIT_DOWN
    assert dict(v.evidence)["quantity"] == 1000


def test_sell_that_is_merely_down_but_not_at_the_limit_still_executes() -> None:
    """**卖出永不因「看空」被拦**：跌 5%（阈值 10%）照卖，本层不复制任何该不该卖的判断。"""
    plan = plan_orders(
        _batch(_sell("600036.SH", 0.5)),
        holdings=dict([_held("600036.SH", available=600)]),
        quotes=dict(
            [_quote("600036.SH", day_chg_ratio=-0.05, limit_threshold_ratio=0.10)]
        ),
    )
    assert [leg.quantity for leg in plan.sells] == [300]


def test_limit_down_not_judged_when_the_threshold_is_unavailable() -> None:
    """阈值取不到 → **不判**（放行）但必须留痕：拦下的一批单无法追责，放行的要能解释。"""
    plan = plan_orders(
        _batch(_sell("600036.SH", 1.0)),
        holdings=dict([_held("600036.SH", available=1000)]),
        quotes=dict(
            [_quote("600036.SH", day_chg_ratio=-0.10, limit_threshold_ratio=None)]
        ),
    )
    assert len(plan.sells) == 1
    assert any("跌停未判" in n for n in plan.notes)


def test_sell_without_a_quote_goes_out_without_a_limit_price() -> None:
    """没有现价：**绝不臆造一个价**，腿照出（带量不带价），问题留在 note 里。"""
    plan = plan_orders(
        _batch(_sell("600036.SH", 1.0)),
        holdings=dict([_held("600036.SH", available=1000)]),
    )
    (leg,) = plan.sells
    assert leg.limit_price is None
    assert any("no_reference_price" in n for n in plan.notes)


# ---------------------------------------------------------------------------
# 3. 限价：单边带（买入贴着上、卖出贴着下）
# ---------------------------------------------------------------------------


def test_sell_limit_price_sits_one_percent_below_the_reference() -> None:
    plan = plan_orders(
        _batch(_sell("600036.SH", 0.5)),
        holdings=dict([_held("600036.SH", available=600)]),
        quotes=dict([_quote("600036.SH", price=10.0)]),
    )
    (leg,) = plan.sells
    assert leg.limit_price == pytest.approx(10.0 * (1 - LIMIT_SLIP))


def test_buy_limit_price_sits_one_percent_above_the_reference() -> None:
    plan = plan_orders(
        _batch(_buy("600519.SH", 0.2)),
        gate=_gate("600519.SH"),
        quota=200_000.0,
        quotes=dict([_quote("600519.SH", price=10.0)]),
    )
    (leg,) = plan.buys
    assert leg.limit_price == pytest.approx(10.0 * (1 + LIMIT_SLIP))
    assert leg.quantity == 4000  # 200000×0.2 / 10 = 4000，整手原样


def test_the_slip_band_is_one_percent_not_the_two_percent_default() -> None:
    """决策腿是「贴着打保成交」：挂远 2% 等于把这一轮意图作废。"""
    assert LIMIT_SLIP == 0.01


# ---------------------------------------------------------------------------
# 4. 买入：闸在别处，本层只做「能不能下出去」
# ---------------------------------------------------------------------------


def test_buy_without_a_gate_is_a_noop_not_a_veto() -> None:
    """``gate=None`` = 调用点自己决定本轮不做买入（用户冻结/时段外）。

    记成「被闸拦下」会**凭空给某条规则记一笔并不存在的成本**——影子账的分母就脏了。
    """
    plan = plan_orders(
        _batch(_buy("600519.SH", 0.2)),
        gate=None,
        quota=200_000.0,
        quotes=dict([_quote("600519.SH")]),
    )
    assert plan.legs == () and plan.vetoes == ()
    assert plan.noops == ("600519.SH",)


def test_buy_outside_the_pool_keeps_the_gates_own_rule_id() -> None:
    """本层**不复制** gates 的判据：否决带着 ``check_buy`` 给的 rule id 原样出来。"""
    plan = plan_orders(
        _batch(_buy("600519.SH", 0.2)),
        gate=_gate("600036.SH"),
        quota=200_000.0,
        quotes=dict([_quote("600519.SH")]),
    )
    assert plan.legs == ()
    (v,) = plan.vetoes
    assert v.rule == "l2.pool_not_member"


def test_buy_is_vetoed_when_the_price_is_usable_only_after_the_gate() -> None:
    """闸先过、价后判：无有效现价 → ``l3.no_quote``（算不出股数，也报不出限价）。"""
    plan = plan_orders(
        _batch(_buy("600519.SH", 0.2)),
        gate=_gate("600519.SH"),
        quota=200_000.0,
        quotes={"600519.SH": Quote(symbol="600519.SH", price=None)},
    )
    assert plan.legs == ()
    assert _rule_ids(plan) == [ex.RULE_NO_QUOTE]


def test_buy_with_unknown_quota_neither_spends_nor_blames_a_gate() -> None:
    """额度未知 = 不知道能花多少就不花；归 ``l1.available_cash``（资金闸），不是池闸。"""
    plan = plan_orders(
        _batch(_buy("600519.SH", 0.2)),
        gate=_gate("600519.SH"),
        quota=None,
        quotes=dict([_quote("600519.SH")]),
    )
    assert plan.legs == ()
    assert _rule_ids(plan) == ["l1.available_cash"]


def test_buy_that_cannot_afford_one_lot_is_blamed_on_the_lot_rule() -> None:
    """额度够最低一手（100×1500=15 万）却只肯花 4 万 → 整手归一到 0，归 ``l3.below_min_lot``。"""
    plan = plan_orders(
        _batch(_buy("600519.SH", 0.2)),
        gate=_gate("600519.SH"),
        quota=200_000.0,
        quotes=dict([_quote("600519.SH", price=1500.0)]),
    )
    assert plan.legs == ()
    (v,) = plan.vetoes
    assert v.rule == "l3.below_min_lot"
    assert dict(v.evidence)["quota"] == 200_000.0


def test_zero_quota_is_attributed_to_the_gates_lot_check_not_a_second_rule() -> None:
    """额度 ≤ 0 **不另设一条规则**：``check_buy`` 收到 ``budget=quota`` 已按
    ``l3.below_min_lot`` 拦下（那里能算出差多少钱，归因更准）。本层的职责是别再加
    一条永不触发的 id —— 影子账按 id 分组，烂条目会在报告里冒充「从未触发的规则」。
    """
    plan = plan_orders(
        _batch(_buy("600519.SH", 0.2)),
        gate=_gate("600519.SH"),
        quota=0.0,
        quotes=dict([_quote("600519.SH", price=1500.0)]),
    )
    assert plan.legs == ()
    assert _rule_ids(plan) == ["l3.below_min_lot"]


def test_buy_halted_unknown_does_not_block_but_is_noted() -> None:
    """停牌状态未知 ≠ 没停牌：不拦，但 note 里要能看见「没判」（判据在 gates）。"""
    plan = plan_orders(
        _batch(_buy("600519.SH", 0.2)),
        gate=_gate("600519.SH"),
        quota=200_000.0,
        quotes={"600519.SH": Quote(symbol="600519.SH", price=10.0, halted=None)},
    )
    assert len(plan.buys) == 1
    assert any("停牌" in n for n in plan.notes)


def test_buy_halted_is_vetoed_by_the_gates_rule() -> None:
    plan = plan_orders(
        _batch(_buy("600519.SH", 0.2)),
        gate=_gate("600519.SH"),
        quota=200_000.0,
        quotes={"600519.SH": Quote(symbol="600519.SH", price=10.0, halted=True)},
    )
    assert plan.legs == ()
    assert _rule_ids(plan) == ["l4.halted"]


# ---------------------------------------------------------------------------
# 5. 重复：同轮 vs 在途（理由不同，排查方向不同）
# ---------------------------------------------------------------------------


def test_second_sell_of_the_same_symbol_in_one_batch_is_dropped() -> None:
    plan = plan_orders(
        _batch(_sell("600036.SH", 0.5), _sell("600036.SH", 0.5)),
        holdings=dict([_held("600036.SH", available=1000)]),
        quotes=dict([_quote("600036.SH")]),
    )
    assert len(plan.sells) == 1
    (v,) = plan.vetoes
    assert v.rule == ex.RULE_INFLIGHT_DUP
    assert "本轮已有同向腿" in v.reason


def test_inflight_order_across_rounds_says_something_different() -> None:
    """跨轮重复 = **在途委托没回执**，与「模型同一批说两遍」是两回事。"""
    plan = plan_orders(
        _batch(_sell("600036.SH", 0.5)),
        holdings=dict([_held("600036.SH", available=1000)]),
        quotes=dict([_quote("600036.SH")]),
        inflight=frozenset({("600036.SH", SELL)}),
    )
    assert plan.legs == ()
    (v,) = plan.vetoes
    assert v.rule == ex.RULE_INFLIGHT_DUP
    assert "在途未确认" in v.reason


def test_inflight_keys_are_normalised_too() -> None:
    """在途账存前缀式、决策吐后缀式：不许因为形态不同而漏判。"""
    plan = plan_orders(
        _batch(_sell("600036.SH", 0.5)),
        holdings=dict([_held("600036.SH", available=1000)]),
        quotes=dict([_quote("600036.SH")]),
        inflight=frozenset({("SH600036", "SELL")}),
    )
    assert plan.legs == ()
    assert _rule_ids(plan) == [ex.RULE_INFLIGHT_DUP]


def test_inflight_sell_does_not_block_a_buy_of_the_same_symbol() -> None:
    """同一只票先卖出（在途）再买回是合法调仓——方向必须参与判据。"""
    plan = plan_orders(
        _batch(_buy("600036.SH", 0.2)),
        holdings=dict([_held("600036.SH", available=1000)]),
        quotes=dict([_quote("600036.SH", price=10.0)]),
        gate=_gate("600036.SH"),
        quota=200_000.0,
        inflight=frozenset({("600036.SH", SELL)}),
    )
    assert [leg.side for leg in plan.legs] == [BUY]


def test_duplicate_check_runs_before_the_gate_so_one_symbol_costs_one_ticket() -> None:
    """重复判定在闸**之前**：同一只票说了两遍只应占一个决策名额。"""
    plan = plan_orders(
        _batch(_buy("600519.SH", 0.2), _buy("600519.SH", 0.2)),
        gate=_gate("600519.SH"),
        quota=200_000.0,
        quotes=dict([_quote("600519.SH", price=10.0)]),
    )
    assert len(plan.buys) == 1


# ---------------------------------------------------------------------------
# 6. 计数与顺序（本轮上限按「过闸」计；先卖后买）
# ---------------------------------------------------------------------------


def test_new_buy_is_counted_when_it_passes_the_gate_not_when_it_gets_a_leg() -> None:
    """**过闸即计数**（与隔壁同口径）：本轮上限是「最多新开几只」，不是「最多成交几只」。

    这里第一只票过了闸却在下一关（无现价）被拦，第二只票仍应吃到本轮名额——
    若按「出腿才计数」，这一轮就会突破上限多开一只。
    """
    plan = plan_orders(
        _batch(_buy("600036.SH", 0.2), _buy("600519.SH", 0.2)),
        gate=_gate("600036.SH", "600519.SH", max_new_buys_round=1),
        quota=200_000.0,
        quotes=dict([_quote("600519.SH", price=10.0)]),
    )
    assert _rule_ids(plan) == [ex.RULE_NO_QUOTE, "l2.round_new_buys"]
    assert plan.buys == ()


def test_adding_to_an_existing_position_does_not_consume_the_new_buy_quota() -> None:
    """加仓不占「新开仓」名额（``held=True`` 时 ``check_buy`` 跳过池/名额判定）。"""
    plan = plan_orders(
        _batch(_buy("600036.SH", 0.2)),
        holdings=dict([_held("600036.SH", available=100)]),
        gate=_gate(max_new_buys_round=0),
        quota=200_000.0,
        quotes=dict([_quote("600036.SH", price=10.0)]),
    )
    assert len(plan.buys) == 1


def test_sell_legs_are_ordered_before_buy_legs() -> None:
    """先回收资金再买（隔壁执行段顺序）；买入额度由调用点在成交后重取。"""
    plan = plan_orders(
        _batch(_buy("600519.SH", 0.2), _sell("600036.SH", 0.5)),
        holdings=dict([_held("600036.SH", available=1000)]),
        quotes=dict([_quote("600036.SH", price=10.0), _quote("600519.SH", price=10.0)]),
        gate=_gate("600519.SH"),
        quota=200_000.0,
    )
    assert [leg.side for leg in plan.legs] == [SELL, BUY]
    assert [leg.side for leg in plan.sells] == [SELL]
    assert [leg.side for leg in plan.buys] == [BUY]


def test_hold_and_watch_are_recorded_not_executed() -> None:
    """``watch`` 的规则落库是 P2.4 的事，本层只把它**点出来**（不碰 sltp 表）。"""
    plan = plan_orders(
        _batch(
            Decision(action=HOLD, code="600036.SH"),
            Decision(action=WATCH, code="600519.SH", stop_loss=1480.0),
        ),
        holdings=dict([_held("600036.SH")]),
    )
    assert plan.legs == () and plan.vetoes == ()
    assert plan.noops == ("600036.SH",)
    assert plan.watches == ("600519.SH",)


def test_decisions_without_a_code_do_not_create_empty_records() -> None:
    """没有代码的 hold/watch 直接丢：记一条空码的 no-op 只会在账上冒充一笔决策。"""
    plan = plan_orders(
        _batch(Decision(action=HOLD, code=""), Decision(action=WATCH, code=""))
    )
    assert plan.noops == () and plan.watches == ()


def test_unknown_actions_are_ignored_by_the_contract_layer_not_executed_here() -> None:
    """契约层已把未知 action 剔掉（``ignored_actions``），本层不兜底——兜底会把
    「多出一个没见过的动作」这件本该报警的事变成静默跳过。"""
    plan = plan_orders(_batch(Decision(action="hedge", code="600036.SH")))
    assert plan.legs == () and plan.vetoes == () and plan.noops == ()


# ---------------------------------------------------------------------------
# 7. 纯函数性与留痕形状
# ---------------------------------------------------------------------------


def test_plan_orders_is_pure_and_does_not_mutate_its_inputs() -> None:
    """holdings/quotes 是调用点的账（桥快照 + 账本），执行段只许读。"""
    holdings = dict([_held("600036.SH", available=1000)])
    quotes = dict([_quote("600036.SH")])
    snapshot = (dict(holdings), dict(quotes))
    batch = _batch(_sell("600036.SH", 0.5))

    first = plan_orders(batch, holdings=holdings, quotes=quotes)
    second = plan_orders(batch, holdings=holdings, quotes=quotes)

    assert first == second, "同一入参必须给出同一计划（否则回放不可复现）"
    assert (holdings, quotes) == (snapshot[0], snapshot[1])


def test_every_outcome_carries_the_index_of_the_decision_it_came_from() -> None:
    """``index`` 是审计表回填执行结果的**连接键**（``build_records`` 按序号取 outcomes）。

    同一批里出现两个同标的时（第二个被去重拦掉）按代码回填会挂错行——这是这条断言
    存在的唯一理由，也是它必须由纯核心给出、不许调用点自己猜的原因。
    """
    plan = plan_orders(
        _batch(
            Decision(action=HOLD, code="600036.SH"),
            _sell("600519.SH"),
            _sell("600036.SH", 0.5),
            _buy("601398.SH", 0.2),
        ),
        holdings=dict([_held("600036.SH", available=1000)]),
        quotes=dict([_quote("600036.SH", price=10.0), _quote("601398.SH", price=10.0)]),
    )
    (veto,) = plan.vetoes  # 600519.SH 不在本账（第 1 条）
    assert veto.index == 1
    (sell_leg,) = plan.sells  # 600036.SH（第 2 条）
    assert sell_leg.index == 2
    # 第 0 条是 hold、第 3 条是「gate=None 的买入」：两者都进 noops，都**不是**否决
    assert plan.noops == ("600036.SH", "601398.SH")
    assert plan.watches == ()


def test_duplicate_legs_in_one_batch_are_distinguishable_by_index() -> None:
    """两条同标的同方向的决策：留下的那条带着自己的序号，被拦的那条也带着自己的。"""
    plan = plan_orders(
        _batch(_sell("600036.SH", 0.5), _sell("600036.SH", 0.5)),
        holdings=dict([_held("600036.SH", available=1000)]),
        quotes=dict([_quote("600036.SH")]),
    )
    assert [leg.index for leg in plan.sells] == [0]
    assert [v.index for v in plan.vetoes] == [1]


def test_veto_serialises_with_a_stable_rule_id_key() -> None:
    """影子代价账按 ``rule_id`` 分组：字典键名就是契约，改名会让历史样本断档。"""
    plan = plan_orders(_batch(_sell("600519.SH")), holdings=dict([_held("600036.SH")]))
    (v,) = plan.vetoes
    payload = v.as_dict()
    assert payload["rule_id"] == ex.RULE_SELL_NOT_HELD
    assert set(payload) == {"symbol", "side", "rule_id", "reason", "evidence"}


def test_leg_serialises_the_price_that_will_be_reported() -> None:
    plan = plan_orders(
        _batch(_sell("600036.SH", 0.5)),
        holdings=dict([_held("600036.SH", available=600)]),
        quotes=dict([_quote("600036.SH", price=10.0)]),
    )
    payload = plan.sells[0].as_dict()
    assert payload == {
        "symbol": "600036.SH",
        "side": SELL,
        "quantity": 300.0,
        "limit_price": pytest.approx(9.9),
        "reason": "减仓",
        "note": "",
    }


def test_every_rule_id_this_layer_can_emit_is_declared_somewhere() -> None:
    """本层能emit的 id 必须来自「本族常量」或「复用他族的既有 id」二选一。

    这条防的是**拼写**：``l3.noquote`` 这类手滑会静默产生一条谁也没登记的规则，
    影子账里它看起来就像一条新规则。
    """
    owned = set(ex.ALL_RULE_IDS)
    borrowed = {
        "l1.t1_sellable",
        "l2.pct_invalid",
        "l1.available_cash",
        "l3.below_min_lot",
        "l2.pool_not_member",
        "l2.round_new_buys",
        "l4.halted",
    }
    emitted: set[str] = set()
    for plan in (
        plan_orders(_batch(_sell("600519.SH"))),
        plan_orders(_batch(Decision(action=SELL, code="", reason="x"))),
        plan_orders(
            _batch(_sell("600036.SH", 1.0)),
            holdings=dict([_held("600036.SH", available=0)]),
        ),
        plan_orders(
            _batch(
                Decision(action=SELL, code="600036.SH", pct=Pct(0.0, PCT_DIRTY, "x"))
            ),
            holdings=dict([_held("600036.SH")]),
        ),
        plan_orders(
            _batch(_buy("600519.SH", 0.2)),
            gate=_gate("600519.SH"),
            quota=200_000.0,
            quotes={"600519.SH": Quote(symbol="600519.SH", price=None)},
        ),
        plan_orders(
            _batch(_sell("600036.SH", 1.0), _sell("600036.SH", 1.0)),
            holdings=dict([_held("600036.SH", available=1000)]),
            quotes=dict([_quote("600036.SH")]),
        ),
        plan_orders(
            _batch(_sell("600036.SH", 1.0)),
            holdings=dict([_held("600036.SH", available=1000)]),
            quotes=dict(
                [_quote("600036.SH", day_chg_ratio=-0.1, limit_threshold_ratio=0.1)]
            ),
        ),
        plan_orders(
            _batch(_buy("600519.SH", 0.2)),
            gate=_gate("600036.SH"),
            quota=200_000.0,
            quotes=dict([_quote("600519.SH")]),
        ),
        plan_orders(
            _batch(_buy("600519.SH", 0.2)),
            gate=_gate("600519.SH", max_new_buys_round=1),
            quota=200_000.0,
            new_buys_round=1,
            quotes=dict([_quote("600519.SH")]),
        ),
        plan_orders(
            _batch(_buy("600519.SH", 0.2)),
            gate=_gate("600519.SH"),
            quota=200_000.0,
            quotes={"600519.SH": Quote(symbol="600519.SH", price=10.0, halted=True)},
        ),
        plan_orders(
            _batch(_buy("600519.SH", 0.2)),
            gate=_gate("600519.SH"),
            quota=None,
            quotes=dict([_quote("600519.SH")]),
        ),
        plan_orders(
            _batch(_buy("600519.SH", 0.2)),
            gate=_gate("600519.SH"),
            quota=200_000.0,
            quotes=dict([_quote("600519.SH", price=1500.0)]),
        ),
    ):
        emitted |= set(_rule_ids(plan))
    unknown = emitted - owned - borrowed
    assert not unknown, (
        f"这些 id 由本层产出，却既不在 ALL_RULE_IDS 也不是复用他族的 id：{unknown}"
    )
    # **零项参与 = 通过**的防线：上面那个集合若被谁改空，`unknown` 永远为空，
    # 这条测试就成了摆设。故要求每个自有的 id 都**真的**在上面的场景里被产出过。
    missing = owned - emitted
    assert not missing, (
        f"本族的这些 id 在任何场景里都没被产出（用例退化了？）：{missing}"
    )
