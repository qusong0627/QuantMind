"""QMT/TDX 执行回报落库（共享内核）。

抽自 ``trade/routers/internal_strategy_bridge.py::report_qmt_execution``：
桥上报（HTTP）与 QMT 执行端轮询（``qmt_exec_poller``）走**同一套**订单匹配与
成交落库逻辑，避免两条回报通道口径漂移。

匹配优先级（与原实现一致）：
  1. ``client_order_id`` 精确匹配（全局唯一，最可靠）
  2. 兼容：回传的是 ``order_id``(UUID)
  3. ``exchange_order_id`` 匹配
  4. 兜底：最近 15 分钟同标的同方向、仍处于 ``[AWAITING_BRIDGE_ACK]`` 的订单

落库规则：
  * 只有带 ``exchange_trade_id`` 的回报才累计成交数量/金额（防止状态回调与
    成交回调双计）；``FILLED`` 但成交量为 0 时降级为 ``SUBMITTED``。
  * 成交行按 ``exchange_trade_id`` 去重（同一笔成交重复上报不重复入账）。
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.trade_shared.models.enums import OrderSide, OrderStatus
from backend.services.trade_shared.models.order import Order
from backend.services.trade_shared.models.trade import Trade

logger = logging.getLogger(__name__)

# 桥/轮询上报的标准英文状态 → OrderStatus
STATUS_MAP: dict[str, OrderStatus] = {
    "SUBMITTED": OrderStatus.SUBMITTED,
    "PARTIALLY_FILLED": OrderStatus.PARTIALLY_FILLED,
    "FILLED": OrderStatus.FILLED,
    "REJECTED": OrderStatus.REJECTED,
    "CANCELLED": OrderStatus.CANCELLED,
    "PARTIALLY_CANCELLED": OrderStatus.CANCELLED,  # 部撤 → 视为已撤
    "EXPIRED": OrderStatus.EXPIRED,
    "PENDING": OrderStatus.PENDING,
}

# QMT 原始数字状态码兜底映射（Agent/轮询未转换时直接上报数字）
# 51/52 是「撤单请求已发出但未确认」，委托仍可能成交，不能当终态；
# 55 是部分成交（旧映射误标为废单，会漏记成交）。
QMT_CODE_MAP: dict[str, OrderStatus] = {
    "48": OrderStatus.PENDING,  # 未报
    "49": OrderStatus.SUBMITTED,  # 待报
    "50": OrderStatus.SUBMITTED,  # 已报
    "51": OrderStatus.SUBMITTED,  # 报撤中（未确认）
    "52": OrderStatus.PARTIALLY_FILLED,  # 部成待撤
    "53": OrderStatus.CANCELLED,  # 部撤
    "54": OrderStatus.CANCELLED,  # 已撤
    "55": OrderStatus.PARTIALLY_FILLED,  # 部分成交
    "56": OrderStatus.FILLED,  # 已成
    "57": OrderStatus.REJECTED,  # 废单
    "58": OrderStatus.FILLED,
    "255": OrderStatus.SUBMITTED,  # 未知
}

_INVALID_EXCHANGE_ORDER_IDS = {"", "-1", "0", "none", "null", "nan"}
_FALLBACK_WINDOW_MINUTES = 15
_ACK_WAITING_MARKER = "[AWAITING_BRIDGE_ACK]"

# 不覆盖为 remarks 的噪声消息（异步受理回执，无信息量）
_IGNORED_MESSAGES = ("async order accepted",)


def valid_exchange_order_id(value: Any) -> str:
    candidate = str(value or "").strip()
    return "" if candidate.lower() in _INVALID_EXCHANGE_ORDER_IDS else candidate


def normalize_status(raw_status: Any) -> OrderStatus:
    """标准字符串优先，其次 QMT 数字码，最后保守判为 SUBMITTED。"""
    text = str(raw_status or "").strip()
    if not text:
        return OrderStatus.SUBMITTED
    return STATUS_MAP.get(text.upper()) or QMT_CODE_MAP.get(text, OrderStatus.SUBMITTED)


async def resolve_order(
    db: AsyncSession,
    *,
    client_order_id: str = "",
    exchange_order_id: str = "",
    symbol: str = "",
    side: str = "",
    tenant_id: str | None = None,
    user_id: int | None = None,
) -> tuple[Order | None, str]:
    """按 4 级优先级定位订单。返回 ``(order, matched_by)``。"""
    cid = str(client_order_id or "").strip()
    filters = []
    if tenant_id:
        filters.append(Order.tenant_id == tenant_id)
    if user_id is not None:
        filters.append(Order.user_id == str(user_id))

    if cid:
        result = await db.execute(
            select(Order).where(and_(Order.client_order_id == cid, *filters))
        )
        order = result.scalar_one_or_none()
        if order is not None:
            return order, "client_order_id"
        # 兼容：某些 Agent 会把 order_id(UUID) 填进 client_order_id 回传
        try:
            oid = uuid.UUID(cid)
        except (ValueError, AttributeError, TypeError):
            oid = None
        if oid is not None:
            result = await db.execute(
                select(Order).where(and_(Order.order_id == oid, *filters))
            )
            order = result.scalar_one_or_none()
            if order is not None:
                return order, "order_id"

    ex_oid = valid_exchange_order_id(exchange_order_id)
    if ex_oid:
        result = await db.execute(
            select(Order).where(and_(Order.exchange_order_id == ex_oid, *filters))
        )
        order = result.scalar_one_or_none()
        if order is not None:
            return order, "exchange_order_id"

    symbol_norm = str(symbol or "").strip().upper()
    order_side: OrderSide | None = None
    if str(side or "").strip().upper() in {"BUY", "SELL"}:
        try:
            order_side = OrderSide(str(side).strip().upper())
        except ValueError:
            order_side = None
    if symbol_norm and order_side is not None:
        recent_cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
            minutes=_FALLBACK_WINDOW_MINUTES
        )
        result = await db.execute(
            select(Order)
            .where(
                and_(
                    Order.symbol == symbol_norm,
                    Order.side == order_side,
                    Order.submitted_at.is_not(None),
                    Order.submitted_at >= recent_cutoff,
                    Order.exchange_order_id.is_(None),
                    Order.remarks.is_not(None),
                    Order.remarks.like(f"%{_ACK_WAITING_MARKER}%"),
                    *filters,
                )
            )
            .order_by(Order.submitted_at.desc())
            .limit(2)
        )
        candidates = list(result.scalars().all())
        if len(candidates) == 1:
            return candidates[0], "symbol_side_recent_ack_waiting"
        if len(candidates) > 1:
            logger.warning(
                "[ExecReport] 兜底匹配歧义 symbol=%s side=%s candidates=%s",
                symbol_norm,
                side,
                [str(item.order_id) for item in candidates],
            )
    return None, ""


async def apply_execution_report(
    db: AsyncSession,
    *,
    order: Order,
    status_raw: Any,
    filled_quantity: Any = None,
    filled_price: Any = None,
    exchange_order_id: str = "",
    exchange_trade_id: str = "",
    message: str = "",
    error_code: str = "",
    report_symbol: str = "",
    report_side: str = "",
) -> OrderStatus:
    """把一条回报应用到订单（含成交去重与累计），返回归一化后的状态。

    调用方负责 ``db.commit()`` 与事件推送。
    """
    normalized = normalize_status(status_raw)
    ex_oid = valid_exchange_order_id(exchange_order_id)
    trade_id = str(exchange_trade_id or "").strip()
    try:
        filled_qty = float(filled_quantity) if filled_quantity is not None else 0.0
    except (TypeError, ValueError):
        filled_qty = 0.0

    # 防御：FILLED 但成交量为 0 → 降级，避免状态误报产生虚假成交
    if normalized == OrderStatus.FILLED and filled_qty <= 0:
        normalized = OrderStatus.SUBMITTED

    order.status = normalized
    if ex_oid:
        order.exchange_order_id = ex_oid
    msg = str(message or "").strip()
    if error_code:
        msg = f"[{error_code}] {msg}".strip()
    if msg and not (
        not ex_oid
        and not str(report_symbol or "").strip()
        and not str(report_side or "").strip()
        and any(token in msg.lower() for token in _IGNORED_MESSAGES)
    ):
        order.remarks = msg

    if normalized in {OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED}:
        if not trade_id:
            # 状态回调可能带累计成交量但没有唯一成交 ID：
            # 为避免与后续成交回调双计，这里只更新状态，不累计金额/数量。
            pass
        elif filled_qty > 0:
            price = (
                float(filled_price)
                if filled_price
                else float(getattr(order, "average_price", None) or order.price or 0.0)
            )
            trade_value = filled_qty * price
            existing = await db.execute(
                select(Trade).where(
                    and_(
                        Trade.tenant_id == order.tenant_id,
                        Trade.user_id == order.user_id,
                        Trade.exchange_trade_id == trade_id,
                    )
                )
            )
            if existing.scalar_one_or_none() is None:
                db.add(
                    Trade(
                        tenant_id=order.tenant_id,
                        user_id=order.user_id,
                        portfolio_id=order.portfolio_id,
                        order_id=order.order_id,
                        symbol=order.symbol,
                        symbol_name=getattr(order, "symbol_name", None),
                        side=order.side,
                        trade_action=getattr(order, "trade_action", None),
                        position_side=getattr(order, "position_side", None),
                        is_margin_trade=bool(getattr(order, "is_margin_trade", False)),
                        trading_mode=order.trading_mode,
                        quantity=filled_qty,
                        price=price,
                        trade_value=trade_value,
                        commission=0.0,
                        exchange_trade_id=trade_id,
                        executed_at=datetime.now(),
                        remarks=(
                            (f"[{error_code}] " if error_code else "")
                            + str(message or "")
                        ).strip()
                        or None,
                    )
                )
                order.filled_quantity = (
                    float(getattr(order, "filled_quantity", 0.0) or 0.0) + filled_qty
                )
                order.filled_value = (
                    float(getattr(order, "filled_value", 0.0) or 0.0) + trade_value
                )
                if order.filled_quantity > 0:
                    order.average_price = order.filled_value / order.filled_quantity

        total_quantity = float(order.quantity or 0.0)
        filled_total = float(getattr(order, "filled_quantity", 0.0) or 0.0)
        if total_quantity > 0 and filled_total >= total_quantity:
            order.status = OrderStatus.FILLED
            if getattr(order, "filled_at", None) is None:
                order.filled_at = datetime.now()
        elif filled_total > 0:
            order.status = OrderStatus.PARTIALLY_FILLED

    if (
        order.status == OrderStatus.CANCELLED
        and getattr(order, "cancelled_at", None) is None
    ):
        order.cancelled_at = datetime.now()

    return order.status


def publish_order_event(redis: Any, order: Order, status: OrderStatus) -> None:
    """推送交易事件给前端（Event-Driven 刷新）。失败不影响落库。"""
    if redis is None:
        return
    try:
        event_data = {
            "event_type": "TRADE_CREATED"
            if status == OrderStatus.FILLED
            else "ORDER_UPDATED",
            "order_id": str(order.order_id),
            "user_id": str(order.user_id),
            "tenant_id": order.tenant_id,
            "status": status.value,
            "symbol": order.symbol,
            "filled_quantity": float(order.filled_quantity or 0),
            "timestamp": datetime.now().isoformat(),
        }
        redis.publish_event("trading_events", event_data)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[ExecReport] 交易事件推送失败 order_id=%s: %s", order.order_id, exc
        )
