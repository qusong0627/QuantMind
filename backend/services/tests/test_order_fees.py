"""``trade_shared.order_fees``：成交费用四列的落库口径。

费率本身在 ``market_rules``（T-P2-02 单实现，平价由 ``test_rule_parity`` 钉住）；
这里钉的是**按标的选市场**、**拆四列**与**用真单那一支费率**这三件事——两个真单
写入点（execution_stream consumer / qmt_exec_reconciler）共用它，写错市场、漏列
或用错佣金口径都不该靠人眼发现。
"""

from __future__ import annotations

from backend.services.simulation.services.market_rules import CN_RULES, HK_RULES
from backend.services.trade_shared.models.enums import OrderSide
from backend.services.trade_shared.order_fees import FEE_COLUMNS, fee_columns


def test_cn_buy_splits_the_three_components_and_totals_them():
    out = fee_columns("600519.SH", 1000, 10.0, "buy")

    assert set(out) == set(FEE_COLUMNS), "四列缺一不可（漏列吃默认 0 ⇒ 统计恒 0）"
    assert (out["commission"], out["stamp_duty"], out["transfer_fee"]) == (
        *CN_RULES.compute_real_order_breakdown(1000, 10.0, "buy"),
    )
    assert out["stamp_duty"] == 0.0, "买方无印花税"
    assert out["total_fee"] == round(
        out["commission"] + out["stamp_duty"] + out["transfer_fee"], 2
    )


def test_cn_real_fill_pays_the_broker_rate_not_the_matching_rate():
    """真单成交记的是**券商实收的估计**（万2.5），不是撮合的计划费率（万3）。

    用**高于最低佣金**的样本才有区分力：10 万成交下两者差 25 vs 30 元。低额样本
    （≤2 万，最低佣金 5 元生效）两条费率同价，拿它当金样等于没测。
    """
    out = fee_columns("SH600519", 10000, 10.0, "buy")  # 10 万

    assert out["commission"] == 25.0, "万2.5：25 佣金（撮合口径是 30）"
    assert out["commission"] != CN_RULES.compute_fee_breakdown(10000, 10.0, "buy")[0], (
        "本样本须能区分券商假设与撮合默认，否则这条用例是空的"
    )


def test_cn_sell_pays_the_stamp_duty():
    out = fee_columns("SH600519", 1000, 10.0, "sell")  # 前缀式同样要认得

    assert out["stamp_duty"] == 5.0
    assert out["total_fee"] == 10.1  # 5（最低佣金）+ 5 + 0.1


def test_enum_side_is_priced_like_its_string_value():
    """枚举方向与字符串同价（``str(OrderSide.SELL)`` 不是 ``"sell"``）。"""
    assert fee_columns("SH600519", 1000, 10.0, OrderSide.SELL) == fee_columns(
        "SH600519", 1000, 10.0, "sell"
    )


def test_hk_symbol_uses_hk_rules():
    """港股费率与 A 股不同（最低佣金 3 元、无过户费、卖方印花 0.1%）：
    按 A 股口径记会同时错三处。"""
    out = fee_columns("0001.HK", 1000, 10.0, "sell")

    assert out == {
        "commission": 3.0,  # HK 最低佣金 3 元（A 股是 5）
        "stamp_duty": 10.0,  # 卖方 0.1%
        "transfer_fee": 0.0,  # 港股无过户费
        "total_fee": 13.0,
    }
    # 港股没有单独的券商假设（`broker_commission_rate=None`）→ 与撮合同价
    assert (out["commission"], out["stamp_duty"]) == (
        *HK_RULES.compute_real_order_breakdown(1000, 10.0, "sell")[:2],
    )


def test_explicit_market_overrides_symbol_inference():
    """跨市场账户的调用方知道自己在哪个市场，显式入参优先。"""
    out = fee_columns("0001.HK", 1000, 10.0, "buy", market="CN")

    assert out["stamp_duty"] == 0.0
    assert (out["commission"], out["transfer_fee"]) == (5.0, 0.1)


def test_unusable_inputs_record_zero_but_do_not_raise():
    """数量/价格取不到数时记 0 而不是抛：**成交行必须落库**，费用是可补算的派生量。"""
    out = fee_columns("SH600519", None, 10.0, "buy")

    assert out == dict.fromkeys(FEE_COLUMNS, 0.0)
