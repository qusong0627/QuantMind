"""R3 手动模式：validate_confirmed 校验矩阵。

规则见 docs/replay/REPLAY_R3_R5_PLAN.md：
- 必须在提案内、方向一致
- 数量只能调小，不能调大
- 买入向下取整到整手；卖出允许零头（清仓）
- 卖出不超可卖量；买入按提案顺序累计校验现金
- 止损笔强制执行，用户剔除也加回
"""

from backend.services.simulation.replay.proposal import validate_confirmed


def _account(cash=100_000.0, positions=None):
    return {"cash": cash, "positions": positions or {}}


def _buy(symbol, qty, px=10.0):
    return {
        "symbol": symbol,
        "side": "BUY",
        "quantity": qty,
        "est_price": px,
        "origin": "signal",
        "cancellable": True,
        "reason": "调仓买入",
    }


def _sell(symbol, qty, px=10.0, origin="signal", cancellable=True, stop_price=None):
    item = {
        "symbol": symbol,
        "side": "SELL",
        "quantity": qty,
        "est_price": px,
        "origin": origin,
        "cancellable": cancellable,
        "reason": "调仓卖出",
    }
    if stop_price is not None:
        item["stop_price"] = stop_price
    return item


# ---------------------------------------------------------------------------
# 基本通路
# ---------------------------------------------------------------------------


def test_accepts_exact_proposal():
    props = [_buy("600036.SH", 1000)]
    acc, rej = validate_confirmed(
        [{"symbol": "600036.SH", "side": "BUY", "quantity": 1000}],
        props,
        _account(cash=50_000),
    )
    assert rej == []
    assert len(acc) == 1
    assert acc[0]["quantity"] == 1000


def test_unconfirmed_proposal_is_dropped_not_rejected():
    """用户没勾的提案直接不执行，不算 rejected。"""
    props = [_buy("600036.SH", 1000), _buy("000001.SZ", 500)]
    acc, rej = validate_confirmed(
        [{"symbol": "600036.SH", "side": "BUY", "quantity": 1000}],
        props,
        _account(cash=50_000),
    )
    assert [a["symbol"] for a in acc] == ["600036.SH"]
    assert rej == []


# ---------------------------------------------------------------------------
# 数量边界：只能调小
# ---------------------------------------------------------------------------


def test_quantity_can_be_reduced():
    props = [_buy("600036.SH", 1000)]
    acc, rej = validate_confirmed(
        [{"symbol": "600036.SH", "side": "BUY", "quantity": 400}],
        props,
        _account(cash=50_000),
    )
    assert rej == []
    assert acc[0]["quantity"] == 400


def test_quantity_cannot_be_increased():
    props = [_buy("600036.SH", 1000)]
    acc, rej = validate_confirmed(
        [{"symbol": "600036.SH", "side": "BUY", "quantity": 1500}],
        props,
        _account(cash=500_000),
    )
    assert acc == []
    assert len(rej) == 1
    assert rej[0]["reason"].startswith("EXCEED_PROPOSED_QTY")


def test_zero_and_negative_quantity_rejected():
    props = [_buy("600036.SH", 1000)]
    for bad in (0, -100):
        acc, rej = validate_confirmed(
            [{"symbol": "600036.SH", "side": "BUY", "quantity": bad}],
            props,
            _account(),
        )
        assert acc == []
        assert rej[0]["reason"] == "INVALID_QUANTITY"


def test_non_integer_quantity_rejected():
    props = [_buy("600036.SH", 1000)]
    acc, rej = validate_confirmed(
        [{"symbol": "600036.SH", "side": "BUY", "quantity": "abc"}],
        props,
        _account(),
    )
    assert acc == []
    assert rej[0]["reason"] == "INVALID_QUANTITY"


# ---------------------------------------------------------------------------
# 整手规则：买入取整，卖出允许零头
# ---------------------------------------------------------------------------


def test_buy_floors_to_lot_size():
    props = [_buy("600036.SH", 1000)]
    acc, rej = validate_confirmed(
        [{"symbol": "600036.SH", "side": "BUY", "quantity": 137}],
        props,
        _account(cash=50_000),
        lot_size=100,
    )
    assert rej == []
    assert acc[0]["quantity"] == 100


def test_buy_below_one_lot_rejected():
    props = [_buy("600036.SH", 1000)]
    acc, rej = validate_confirmed(
        [{"symbol": "600036.SH", "side": "BUY", "quantity": 37}],
        props,
        _account(cash=50_000),
        lot_size=100,
    )
    assert acc == []
    assert rej[0]["reason"] == "BELOW_LOT_SIZE"


def test_sell_allows_odd_lot_for_full_exit():
    """卖出零头必须允许，否则 137 股的持仓永远清不掉。"""
    props = [_sell("600036.SH", 137)]
    acc, rej = validate_confirmed(
        [{"symbol": "600036.SH", "side": "SELL", "quantity": 137}],
        props,
        _account(positions={"600036.SH": {"volume": 137, "available_volume": 137}}),
        lot_size=100,
    )
    assert rej == []
    assert acc[0]["quantity"] == 137


# ---------------------------------------------------------------------------
# 提案外标的 / 方向不符
# ---------------------------------------------------------------------------


def test_symbol_not_in_proposal_rejected():
    props = [_buy("600036.SH", 1000)]
    acc, rej = validate_confirmed(
        [{"symbol": "999999.SH", "side": "BUY", "quantity": 100}],
        props,
        _account(cash=50_000),
    )
    assert acc == []
    assert rej[0]["reason"] == "NOT_IN_PROPOSAL"


def test_side_mismatch_rejected():
    """提案是买入，用户改成卖出 → 视为不在提案内。"""
    props = [_buy("600036.SH", 1000)]
    acc, rej = validate_confirmed(
        [{"symbol": "600036.SH", "side": "SELL", "quantity": 1000}],
        props,
        _account(positions={"600036.SH": {"volume": 5000, "available_volume": 5000}}),
    )
    assert acc == []
    assert rej[0]["reason"] == "NOT_IN_PROPOSAL"


# ---------------------------------------------------------------------------
# 资金与可卖量
# ---------------------------------------------------------------------------


def test_insufficient_cash_rejected():
    props = [_buy("600036.SH", 1000, px=100.0)]  # 需 100,000
    acc, rej = validate_confirmed(
        [{"symbol": "600036.SH", "side": "BUY", "quantity": 1000}],
        props,
        _account(cash=50_000),
    )
    assert acc == []
    assert rej[0]["reason"].startswith("INSUFFICIENT_CASH")


def test_sell_proceeds_fund_later_buys():
    """先卖后买：卖出所得计入可用现金，让后续买单通过。"""
    props = [
        _sell("000001.SZ", 1000, px=100.0),  # +100,000
        _buy("600036.SH", 1000, px=100.0),  # -100,000
    ]
    acc, rej = validate_confirmed(
        [
            {"symbol": "000001.SZ", "side": "SELL", "quantity": 1000},
            {"symbol": "600036.SH", "side": "BUY", "quantity": 1000},
        ],
        props,
        _account(
            cash=0.0,
            positions={"000001.SZ": {"volume": 1000, "available_volume": 1000}},
        ),
    )
    assert rej == []
    assert len(acc) == 2


def test_sell_exceeds_available_volume_rejected():
    props = [_sell("600036.SH", 1000)]
    acc, rej = validate_confirmed(
        [{"symbol": "600036.SH", "side": "SELL", "quantity": 1000}],
        props,
        # T+1：持有 1000 但仅 300 可卖
        _account(positions={"600036.SH": {"volume": 1000, "available_volume": 300}}),
    )
    assert acc == []
    assert rej[0]["reason"].startswith("INSUFFICIENT_AVAILABLE")


def test_sell_falls_back_to_volume_when_available_is_none():
    """available_volume 缺失时用 volume 兜底（Lua nil 兼容分支）。"""
    props = [_sell("600036.SH", 500)]
    acc, rej = validate_confirmed(
        [{"symbol": "600036.SH", "side": "SELL", "quantity": 500}],
        props,
        _account(positions={"600036.SH": {"volume": 500}}),
    )
    assert rej == []
    assert acc[0]["quantity"] == 500


# ---------------------------------------------------------------------------
# 止损强制执行
# ---------------------------------------------------------------------------


def test_stop_loss_forced_when_user_omits_it():
    """用户没勾止损 → 仍然强制执行。"""
    props = [
        _sell("600036.SH", 900, origin="stop_loss", cancellable=False, stop_price=9.5),  # fidelity: allow-limit-threshold — 非阈值：止损价夹具（stop_price）
        _buy("000001.SZ", 100),
    ]
    acc, rej = validate_confirmed(
        [{"symbol": "000001.SZ", "side": "BUY", "quantity": 100}],
        props,
        _account(
            cash=50_000,
            positions={"600036.SH": {"volume": 900, "available_volume": 900}},
        ),
    )
    assert rej == []
    symbols = [a["symbol"] for a in acc]
    assert "600036.SH" in symbols, "止损笔必须被强制加回"
    stop = next(a for a in acc if a["symbol"] == "600036.SH")
    assert stop["origin"] == "stop_loss"
    assert stop["quantity"] == 900


def test_stop_loss_quantity_cannot_be_reduced():
    """用户试图把止损数量改小 → 仍按提案全量执行。"""
    props = [
        _sell("600036.SH", 900, origin="stop_loss", cancellable=False, stop_price=9.5),  # fidelity: allow-limit-threshold — 非阈值：止损价夹具（stop_price）
    ]
    acc, _rej = validate_confirmed(
        [{"symbol": "600036.SH", "side": "SELL", "quantity": 100}],
        props,
        _account(positions={"600036.SH": {"volume": 900, "available_volume": 900}}),
    )
    stop = next(a for a in acc if a["symbol"] == "600036.SH")
    assert stop["quantity"] == 900, "止损不可被调小"


def test_stop_loss_carries_stop_price_through():
    props = [
        _sell("600036.SH", 900, origin="stop_loss", cancellable=False, stop_price=9.5),  # fidelity: allow-limit-threshold — 非阈值：止损价夹具（stop_price）
    ]
    acc, _ = validate_confirmed(
        [], props,
        _account(positions={"600036.SH": {"volume": 900, "available_volume": 900}}),
    )
    assert acc[0]["stop_price"] == 9.5  # fidelity: allow-limit-threshold — 非阈值：断言止损价原样透传（值即上方夹具）


# ---------------------------------------------------------------------------
# 顺序确定性
# ---------------------------------------------------------------------------


def test_accepted_follows_proposal_order_not_user_order():
    """结果顺序由提案决定（卖在前），不受用户提交顺序影响 —— 保证现金校验可复现。"""
    props = [_sell("000001.SZ", 100), _buy("600036.SH", 100)]
    user_reversed = [
        {"symbol": "600036.SH", "side": "BUY", "quantity": 100},
        {"symbol": "000001.SZ", "side": "SELL", "quantity": 100},
    ]
    acc, rej = validate_confirmed(
        user_reversed,
        props,
        _account(
            cash=10_000,
            positions={"000001.SZ": {"volume": 100, "available_volume": 100}},
        ),
    )
    assert rej == []
    assert [a["side"] for a in acc] == ["SELL", "BUY"]


def test_empty_confirmed_executes_only_stop_loss():
    props = [
        _sell("600036.SH", 900, origin="stop_loss", cancellable=False, stop_price=9.5),  # fidelity: allow-limit-threshold — 非阈值：止损价夹具（stop_price）
        _buy("000001.SZ", 100),
    ]
    acc, rej = validate_confirmed(
        [], props,
        _account(
            cash=50_000,
            positions={"600036.SH": {"volume": 900, "available_volume": 900}},
        ),
    )
    assert rej == []
    assert len(acc) == 1
    assert acc[0]["origin"] == "stop_loss"
