"""T-P2-02 测试：撮合规则单实现——**规则平价测试**。

验收口径（细案）：同一 (symbol, 价格, 数量, 方向) 下
matcher / market_rules / 回测引擎 三处费用**逐分相等**（含最低佣金与卖方印花场景）；
申报数量归一一致（科创板 200 起 1 股递增，其余 100 整数倍）；涨跌停阈值同源。
"""

from pathlib import Path

import pandas as pd
import pytest

from backend.services.simulation.services.ashare_matcher import (
    MatchConfig,
    compute_fees,
)
from backend.services.simulation.services.market_rules import (
    CN_RULES,
    normalize_order_quantity,
)

_BACKEND = Path(__file__).resolve().parents[1]

_FEE_CASES = [
    # (数量, 价格, 方向, 备注)
    (1000, 10.0, "buy", "常规买入"),
    (1000, 10.0, "sell", "常规卖出（含印花）"),
    (100, 10.0, "buy", "小额触发最低佣金"),
    (100000, 50.02, "sell", "大额卖出"),
    (200, 3.33, "buy", "科创板最小申报价位"),
]


def test_fee_parity_matcher_vs_market_rules():
    cfg = MatchConfig()  # 默认值来自 CN_RULES（单实现）
    for qty, price, side, note in _FEE_CASES:
        commission, stamp, transfer, total = compute_fees(qty, price, side, cfg)
        c2, s2, t2 = CN_RULES.compute_fee_breakdown(qty, price, side)
        assert (commission, stamp, transfer) == (c2, s2, t2), f"matcher≠rules: {note}"
        assert total == round(c2 + s2 + t2, 2), f"total 不平: {note}"


def test_fee_parity_backtest_engine():
    """回测引擎成交记录与 market_rules 逐分相等；买入按 lot 归一。"""
    from backend.shared.backtest_engine.core.engine import BacktestEngine
    from backend.shared.backtest_engine.core.order import Order, OrderSide, OrderType

    engine = BacktestEngine(initial_cash=10_000_000.0, enable_risk_management=False)
    bar = pd.Series({"volume": 1_000_000, "close": 10.0, "high": 10.2, "low": 9.8})  # fidelity: allow-limit-threshold — 非阈值：日线夹具的 low

    # 买入 150 股 → 应归一为 100（主板 100 整数倍）
    engine._execute_order(
        Order(symbol="SH600036", side=OrderSide.BUY, order_type=OrderType.MARKET, quantity=150),
        bar,
    )
    buy = engine.trades[-1]
    exec_price = 10.0 * (1 + engine.slippage_rate)
    c, s, t = CN_RULES.compute_fee_breakdown(int(buy["quantity"]), exec_price, "buy")
    assert buy["quantity"] == 100, f"买入应归一为 100，实际 {buy['quantity']}"
    assert (buy["commission"], buy["stamp_duty"], buy["transfer_fee"]) == (c, s, t)
    assert buy["total_fee"] == round(c + s + t, 2)

    # T+1：卖出前先解锁（模拟"次日"；直调 _execute_order 绕过了日循环的 unlock_t1）
    engine.portfolio.unlock_t1()
    # 卖出 100 股 → 含印花税，逐分相等
    engine._execute_order(
        Order(symbol="SH600036", side=OrderSide.SELL, order_type=OrderType.MARKET, quantity=100),
        bar,
    )
    sell = engine.trades[-1]
    c2, s2, t2 = CN_RULES.compute_fee_breakdown(100, exec_price * (1 - engine.slippage_rate), "sell")
    assert (sell["commission"], sell["stamp_duty"], sell["transfer_fee"]) == (c2, s2, t2)
    assert sell["stamp_duty"] > 0

    # 低于最小申报 → 拒（状态原地标记在订单对象上）
    from backend.shared.backtest_engine.core.order import OrderStatus

    below = Order(symbol="SH600036", side=OrderSide.BUY, order_type=OrderType.MARKET, quantity=50)
    engine._execute_order(below, bar)
    assert below.status == OrderStatus.REJECTED


def test_star_market_quantity_semantics():
    """科创板：≥200 可 1 股递增（201 合法）；低于 200 拒单；主板 150→100。"""
    assert normalize_order_quantity(201, "688981", "CN") == 201
    assert normalize_order_quantity(200, "SH688981", "CN") == 200
    assert normalize_order_quantity(150, "688981", "CN") == 0
    assert normalize_order_quantity(150, "600036", "CN") == 100
    assert normalize_order_quantity(250, "300750", "CN") == 200  # 创业板 100 整数倍
    # 回测引擎同语义
    from backend.shared.backtest_engine.core.engine import BacktestEngine
    from backend.shared.backtest_engine.core.order import Order, OrderSide, OrderType, OrderStatus

    engine = BacktestEngine(initial_cash=1_000_000.0, enable_risk_management=False)
    bar = pd.Series({"volume": 1_000_000, "close": 60.0, "high": 61.0, "low": 59.0})
    engine._execute_order(
        Order(symbol="SH688981", side=OrderSide.BUY, order_type=OrderType.MARKET, quantity=201),
        bar,
    )
    assert engine.trades[-1]["quantity"] == 201
    below = Order(symbol="SH688981", side=OrderSide.BUY, order_type=OrderType.MARKET, quantity=150)
    engine._execute_order(below, bar)
    assert below.status == OrderStatus.REJECTED


def test_price_limit_threshold_single_source():
    """涨跌停阈值：回测引擎与 canonical 同源（含 ST 2026-07-06 切换两侧）。"""
    from datetime import date

    from backend.services.simulation.services.local_market_data import limit_pct
    from backend.shared.backtest_engine.core.engine import get_price_limit_threshold

    assert get_price_limit_threshold("SH600036") == float(
        limit_pct("600036", is_st=False, trade_date=date(2026, 9, 16))  # fidelity: allow-limit-threshold — 显式传参：与权威同传非 ST，保证两侧可比
    )
    assert get_price_limit_threshold("SZ300750") == 0.20
    assert get_price_limit_threshold("SH688981") == 0.20
    # ST 主板：2026-07-06 新规前 5%，后 10%（两侧都断言）
    before = get_price_limit_threshold("SH600036", is_st=True, trade_date=date(2026, 6, 30))
    after = get_price_limit_threshold("SH600036", is_st=True, trade_date=date(2026, 9, 16))
    assert before == 0.05 and after == 0.10


def test_source_single_implementation():
    """源断言：费用/手数不再有第二实现。"""
    matcher_src = (
        _BACKEND / "services/simulation/services/ashare_matcher.py"
    ).read_text(encoding="utf-8")
    assert "_COMMISSION_RATE = CN_RULES.commission_rate" in matcher_src, "费用常量须来自 CN_RULES"
    assert "def _floor_to_lot" not in matcher_src, "matcher 不得再有本地手数实现"

    engine_src = (
        _BACKEND / "shared/backtest_engine/core/engine.py"
    ).read_text(encoding="utf-8")
    assert "compute_fee_breakdown" in engine_src and "normalize_order_quantity" in engine_src
    assert "* self.commission_rate" not in engine_src, "回测引擎不得再有 flat 佣金计算"

    exec_src = (
        _BACKEND / "services/simulation/services/execution_engine.py"
    ).read_text(encoding="utf-8")
    assert "compute_fee_breakdown" in exec_src and "normalize_order_quantity" in exec_src

    rebalance_src = (
        _BACKEND / "services/simulation/services/rebalance_calculator.py"
    ).read_text(encoding="utf-8")
    assert "normalize_order_quantity" in rebalance_src
    assert "def _floor_to_lot" not in rebalance_src


def test_fee_parity_eval_cost_model():
    """费率平价网必须覆盖**评估/研究侧**的 `CostModel` —— 此前正是网外的缺口。

    上面两张网（matcher↔rules、回测引擎↔rules）都对着 `CN_RULES` 比，唯独
    `trading_cost.CostModel`（eval / strategy_card / backtest_service 的费率唯一
    出处）在网外：它的印花税静静漂到 **0.001**（2 倍法定值，2023-08-28 起是 0.05%），
    而另外三处（`CN_RULES` / `CnExchange` / `trade_config`）都是 0.0005。
    没人发现，因为它不和任何人比 —— 平价网的**成员资格**就是这条契约。

    佣金**不**要求与 `CN_RULES` 相同：评估/回测的历史默认 0.00025 是券商成本假设，
    模拟盘默认 0.0003 更保守，这是有意的差异。但 `trading_cost` 的 docstring 明确
    承诺「与 cn_exchange 保持一致」，故佣金与最低佣金只对 `CnExchange` 断言。

    `CnExchange` 需要 qlib 全局配置才能实例化（`Exchange.__init__` 读
    `C.trade_unit`），故取**签名默认值**而非实例 —— 结论等价且不依赖运行环境。
    """
    import inspect

    from backend.services.engine.inference.trading_cost import CostModel
    from backend.services.engine.qlib_app.utils.cn_exchange import CnExchange

    model = CostModel()
    ex = {
        name: param.default
        for name, param in inspect.signature(CnExchange.__init__).parameters.items()
        if param.default is not inspect.Parameter.empty
    }

    # ① 法定费率：政策数字，没有「口径差异」的余地，三侧必须逐位相同
    assert model.stamp_duty == CN_RULES.stamp_duty_rate == ex["stamp_duty"], (
        f"印花税三侧不一致：cost={model.stamp_duty} "
        f"rules={CN_RULES.stamp_duty_rate} cn_exchange={ex['stamp_duty']}"
    )
    assert model.transfer_fee == CN_RULES.transfer_fee_rate == ex["transfer_fee"], (
        f"过户费三侧不一致：cost={model.transfer_fee} "
        f"rules={CN_RULES.transfer_fee_rate} cn_exchange={ex['transfer_fee']}"
    )

    # ② docstring 承诺「与 cn_exchange 口径保持一致」：佣金与最低佣金同值
    assert model.commission_rate == ex["commission"], (
        f"佣金与 cn_exchange 不一致：cost={model.commission_rate} "
        f"cn_exchange={ex['commission']}"
    )
    assert model.min_commission == ex["min_commission"]


def test_trade_config_commission_derives_from_single_source():
    """`trade_config` 的买卖佣金默认值必须**派生自 `CN_RULES`**，不得再手写数字。

    此处曾硬编码 `COMMISSION_RATE_BUY = 0.0003` / `COMMISSION_RATE_SELL = 0.0013`，
    后者是「0.03% 佣金 + 0.1% 印花税 + 0.001% 过户费」的旧合计。印花税 2023-08-28
    减半到 0.05% 之后这个合计没跟着动，且 `COMMISSION_RATE_SELL` 全仓**零消费者**
    （唯一读 `COMMISSION_RATE_*` 的 `risk_service` 只读 BUY 侧），所以两处失真
    都没有症状 —— 又一个「不在平价网里就没人比」的实例（同 `CostModel` 印花税）。

    断言取**派生常量**而非 `settings.*`：后者可被 env 覆盖（覆盖是有意的，
    本用例不该因部署设了 env 而红），前者是默认值本身。
    """
    import backend.services.trade_shared.trade_config as tc

    assert tc.CN_COMMISSION_DEFAULT == pytest.approx(CN_RULES.commission_rate)
    assert tc.CN_SELL_ALLIN_DEFAULT == pytest.approx(
        CN_RULES.commission_rate
        + CN_RULES.stamp_duty_rate
        + CN_RULES.transfer_fee_rate
    ), "卖出合计须 = 佣金 + 印花税 + 过户费（派生，非手写）"

    # 反向钉子：这几个键不得再手写数字默认值（精确到键，不误伤别处的同值字面量——
    # 初版写成 `'"0.0003"' not in src` 就误伤了 `SIMULATION_COMMISSION_RATE`，实际它
    # 同样该派生，只是键不同）。
    src = (_BACKEND / "services/trade_shared/trade_config.py").read_text(encoding="utf-8")
    for key in (
        "COMMISSION_RATE_BUY",
        "COMMISSION_RATE_SELL",
        "SIMULATION_COMMISSION_RATE",
        "SIMULATION_COMMISSION_MIN",
        "SIMULATION_STAMP_DUTY_RATE",
    ):
        assert f'os.getenv("{key}", "' not in src, (
            f"{key} 的默认值须派生自 CN_RULES（传常量名），不得手写数字"
        )
    assert '"0.0013"' not in src, "0.0013 是印花税减半前的旧合计，不得再出现"
