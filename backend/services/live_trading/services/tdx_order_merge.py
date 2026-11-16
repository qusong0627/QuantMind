"""TDX 桥委托回报的单调合并判定（纯函数）。

背景（2026-11-16 审计）
-----------------------
``tdx_push_service._sync_orders_to_pg`` 用 ``exchange_order_id`` 预载本地订单后
无条件 UPDATE：桥重复投递旧状态时，已 ``FILLED`` 的单会被刷回 ``SUBMITTED``，
累计成交量也会回退。更糟的是预载没有交易日过滤，昨日同号委托会被今日回报更新。

本模块只回答"这次回报该不该覆盖本地状态"：
* 终态不被非终态覆盖；
* 累计成交量/成交额不回退；
* 状态不倒退（pending < submitted < partially_filled < filled）。
"""

from __future__ import annotations

TERMINAL = frozenset({"filled", "cancelled", "rejected", "partial_cancelled"})
_STATUS_RANK = {
    "pending": 0,
    "submitted": 1,
    "partially_filled": 2,
    "partial_cancelled": 3,
    "cancelled": 3,
    "rejected": 3,
    "filled": 3,
}


def normalize_status(status) -> str:
    return str(getattr(status, "value", status) or "").strip().lower()


def status_rank(status) -> int:
    return _STATUS_RANK.get(normalize_status(status), -1)


def should_apply_order_update(
    prior_status,
    prior_filled,
    incoming_status,
    incoming_filled,
) -> bool:
    """True = 允许用 incoming 覆盖 prior；False = 保留本地（更晚/更完整）状态。"""
    prior = normalize_status(prior_status)
    incoming = normalize_status(incoming_status)
    try:
        prior_qty = float(prior_filled or 0.0)
    except (TypeError, ValueError):
        prior_qty = 0.0
    try:
        incoming_qty = float(incoming_filled or 0.0)
    except (TypeError, ValueError):
        incoming_qty = 0.0

    # 成交量回退：任何情况下都不接受。
    if incoming_qty < prior_qty:
        return False

    if prior in TERMINAL:
        if incoming in TERMINAL:
            # 终态之间只允许"成交更多"的修正（如 partial_cancelled → filled）。
            return incoming_qty > prior_qty or incoming == prior
        # 终态不能被 submitted/pending 覆盖；但晚到的成交明细允许补量。
        return incoming_qty > prior_qty and incoming == "partially_filled"

    # 非终态：状态不能倒退。
    if status_rank(incoming) < status_rank(prior):
        return False
    return True
