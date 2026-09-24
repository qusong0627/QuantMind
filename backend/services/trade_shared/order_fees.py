"""成交费用落库口径：按标的选市场 → 拆成 ``trades`` 表的四个费用列。

费率**不在这里**（``simulation.services.market_rules`` 是唯一实现，见 T-P2-02）；
这里只回答一个交易侧的问题：**这笔成交该收多少钱、记到哪几列**。

**为什么需要它**：``trades`` 表有四个费用列（``commission`` / ``stamp_duty`` /
``transfer_fee`` / ``total_fee``），而两个真单写入点只写了 ``commission=0.0``、
其余三列吃默认 0 —— 于是 ``get_trade_statistics`` 求和恒为 0，UI 上「总佣金 ¥0.00」。
真单的成本口径不能是 0：复盘、风控与对外披露都读它。

市场按标的推断（``infer_market``：CN/HK/US/期货/加密），与模拟撮合同一个函数。

**口径是真单侧**（``compute_real_order_breakdown``）：本模块只服务**真单成交**的
两个写入点，记的是券商实收的估计（CN 万2.5），不是撮合的计划费率（CN 万3）——
见 ``CN_BROKER_COMMISSION_RATE``。模拟成交不经这里（撮合引擎直接用
``compute_fee_breakdown``，保持保守口径），两条路径的差异是有意的。
"""

from __future__ import annotations

import logging
from typing import Any

from backend.services.simulation.services.market_rules import infer_market, rules_for

logger = logging.getLogger(__name__)

#: ``trades`` 表的四个费用列（写侧与读侧共用同一份名字，防止只写一半）。
FEE_COLUMNS = ("commission", "stamp_duty", "transfer_fee", "total_fee")


def fee_columns(
    symbol: str,
    quantity: Any,
    price: Any,
    side: Any,
    *,
    market: Any = None,
) -> dict[str, float]:
    """一笔成交的四列费用（各分项 round(2)，``total_fee`` 为三者之和）。

    ``market`` 显式给出时优先（跨市场账户的调用方知道自己在哪个市场）；否则按
    标的推断。方向吃字符串也吃枚举（见 ``market_rules.side_text``）。
    """
    rules = rules_for(market) if market is not None else rules_for(infer_market(symbol))
    try:
        commission, stamp_duty, transfer_fee = rules.compute_real_order_breakdown(
            quantity, price, side
        )
    except (TypeError, ValueError) as exc:
        # 数量/价格取不到数（桥回执缺字段）时按「费用未知」记 0：**成交本身必须落库**
        # ——费用是可补算的派生量，成交行不是。但留一行日志：记 0 与「0 费用市场」
        # 在表里长得一样，不吭声就没人能分辨。
        logger.warning(
            "[OrderFees] 费用无法计算（symbol=%s qty=%r price=%r side=%r）: %s",
            symbol,
            quantity,
            price,
            side,
            exc,
        )
        return dict.fromkeys(FEE_COLUMNS, 0.0)
    return {
        "commission": commission,
        "stamp_duty": stamp_duty,
        "transfer_fee": transfer_fee,
        "total_fee": round(commission + stamp_duty + transfer_fee, 2),
    }
