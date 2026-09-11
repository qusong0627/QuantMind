"""
悬挂订单超时扫描器

每隔 SCAN_INTERVAL_SECONDS 秒扫描一次，将超过 ORDER_TIMEOUT_MINUTES 分钟
仍停留在 SUBMITTED 状态的实盘订单标记为 EXPIRED，并推送用户通知。

由外部通道托管的委托（通达信桥 ``通达信桥委托``、QMT 执行端镜像单 ``mirror:``/``mir-``、
止损执行器 ``sltp:``/``sltp-``、按清单平仓 ``flat-``）不适用这条本地启发式：它们的真实
状态由桥/QMT 轮询器回报（桥 30s、QMT 2s），本地判死只会造成「柜台还挂着、本地已终态」
的错位（止损单挂跌停价排队正是超过 30 分钟的典型场景）。这类单改为只提醒不改状态。

环境变量：
  ORDER_TIMEOUT_MINUTES    超时分钟数，默认 30
  ORDER_SCAN_INTERVAL_SEC  扫描间隔秒数，默认 300 (5分钟)
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta

from sqlalchemy import and_, or_, select

from backend.services.trade_shared.models.enums import OrderStatus
from backend.services.trade_shared.models.order import Order, TradingMode
from backend.shared.database_manager_v2 import get_session
from backend.shared.notification_publisher import publish_notification_async

logger = logging.getLogger(__name__)

_TIMEOUT_MINUTES = int(os.getenv("ORDER_TIMEOUT_MINUTES", "30"))
_SCAN_INTERVAL = int(os.getenv("ORDER_SCAN_INTERVAL_SEC", "300"))
_BRIDGE_ACK_TIMEOUT_SECONDS = int(os.getenv("BRIDGE_ACK_TIMEOUT_SECONDS", "120"))
_BRIDGE_ACK_SCAN_INTERVAL = int(os.getenv("BRIDGE_ACK_SCAN_INTERVAL_SEC", "5"))
_AWAITING_BRIDGE_ACK_MARKER = "[AWAITING_BRIDGE_ACK]"
_BRIDGE_ACK_TIMEOUT_MARKER = "[BRIDGE_ACK_TIMEOUT_PENDING_REVIEW]"

# 外部通道托管的委托（备注前缀/特征）——真实状态以桥的回报为权威：
#   通达信桥委托：桥每 30s 同步真实状态（已报/部成/已成/废单）
#   mirror:%    ：QMT 执行端镜像真单，qmt_exec_poller 每 2s 回写柜台状态
# 本地超时启发式不得越权覆盖这两类，否则镜像单在柜台仍挂着（甚至随时可能成交），
# 本地却已 EXPIRED 进入终态，委托列表与柜台长期错位、成交回报也被终态守卫吞掉。
_BROKER_MANAGED_REMARK_PREFIXES = ("mirror:", "sltp:", "通达信桥委托")
_BROKER_MANAGED_REMARK_CONTAINS = ("通达信桥委托",)
# 备注会被成交回报覆盖（qmt_exec_reconciler: ``order.remarks = msg``），
# client_order_id 不会 —— 只能靠 cid 前缀兜底识别：
#   mir-     ：SIM→真单镜像（qmt_exec_poller 2s 回写）
#   sltp-    ：止损/止盈执行器真单（挂跌停价排队可能超过本地超时阈值）
#   flat- / flatten- ：按清单平仓脚本真单
_BROKER_MANAGED_CID_PREFIXES = ("mir-", "sltp-", "flat-", "flatten-")
# 兼容既有引用口径：LIKE 模式列表（前缀式 + 包含式，两者都保留：
# 「通达信桥委托」既可作前缀也可作备注中段标记，如 [AWAITING_BRIDGE_ACK] 通达信桥委托）
_BROKER_MANAGED_REMARK_PATTERNS = tuple(
    dict.fromkeys(
        [f"{p}%" for p in _BROKER_MANAGED_REMARK_PREFIXES]
        + [f"%{c}%" for c in _BROKER_MANAGED_REMARK_CONTAINS]
    )
)
_STALE_PENDING_MARKER = "[STALE_PENDING_REVIEW]"


def is_broker_managed(remarks: str | None, client_order_id: str | None) -> bool:
    """Python 口径的托管判断（与 :func:`_broker_managed_clause` 同规则）。

    供测试与日志使用，避免两处规则漂移。
    """
    text = str(remarks or "")
    if any(text.startswith(p) for p in _BROKER_MANAGED_REMARK_PREFIXES):
        return True
    if any(c in text for c in _BROKER_MANAGED_REMARK_CONTAINS):
        return True
    cid = str(client_order_id or "")
    return any(cid.startswith(p) for p in _BROKER_MANAGED_CID_PREFIXES)


def _broker_managed_clause():
    """SQL 条件：外部通道托管（备注特征 或 镜像 client_order_id 前缀）。"""
    return or_(
        *[Order.remarks.like(pattern) for pattern in _BROKER_MANAGED_REMARK_PATTERNS],
        *[
            Order.client_order_id.like(f"{prefix}%")
            for prefix in _BROKER_MANAGED_CID_PREFIXES
        ],
    )


def _not_broker_managed_clause():
    """SQL 条件：排除由外部通道托管、状态以桥/轮询器为准的委托。"""
    remark_clean = [
        or_(Order.remarks.is_(None), ~Order.remarks.like(pattern))
        for pattern in _BROKER_MANAGED_REMARK_PATTERNS
    ]
    cid_clean = or_(
        Order.client_order_id.is_(None),
        *[
            ~Order.client_order_id.like(f"{prefix}%")
            for prefix in _BROKER_MANAGED_CID_PREFIXES
        ],
    )
    return [*remark_clean, cid_clean]


async def _scan_once() -> int:
    """扫描一次，返回本次过期的订单数量。"""
    cutoff = datetime.now() - timedelta(minutes=_TIMEOUT_MINUTES)
    expired_count = 0

    async with get_session() as db:
        stmt = (
            select(Order)
            .where(
                and_(
                    Order.status == OrderStatus.SUBMITTED,
                    Order.trading_mode == TradingMode.REAL,
                    Order.submitted_at <= cutoff,
                    *_not_broker_managed_clause(),
                )
            )
            .limit(200)
        )
        result = await db.execute(stmt)
        orders = list(result.scalars().all())

        for order in orders:
            try:
                order.status = OrderStatus.EXPIRED
                order.remarks = (
                    order.remarks or ""
                ) + f" [EXPIRED: submitted_at={order.submitted_at}, timeout={_TIMEOUT_MINUTES}m]"
                expired_count += 1
                logger.warning(
                    "order %s expired after %d min (submitted_at=%s)",
                    order.order_id,
                    _TIMEOUT_MINUTES,
                    order.submitted_at,
                )
                # 推送通知（fire-and-forget）
                try:
                    await publish_notification_async(
                        user_id=str(order.user_id),
                        tenant_id=str(order.tenant_id or "default"),
                        title="订单超时过期",
                        content=(
                            f"{order.symbol} 订单 {str(order.order_id)[:8]}... "
                            f"已超过 {_TIMEOUT_MINUTES} 分钟未收到成交回报，已标记为过期。"
                        ),
                        type="trading",
                        level="warning",
                        action_url="/trading",
                    )
                except Exception as notify_exc:
                    logger.warning("notify failed for expired order %s: %s", order.order_id, notify_exc)
            except Exception as exc:
                logger.error("failed to expire order %s: %s", order.order_id, exc)

        if orders:
            await db.commit()

    return expired_count


async def _scan_bridge_ack_timeout_once() -> int:
    """
    扫描 bridge 派发后未收到 ACK/回报的订单，短超时后标记待人工核查。
    仅处理：
    - REAL + SUBMITTED
    - 备注含 [AWAITING_BRIDGE_ACK]
    - 尚未写入 [BRIDGE_ACK_TIMEOUT_PENDING_REVIEW]
    - 无 exchange_order_id
    - submitted_at 超过 BRIDGE_ACK_TIMEOUT_SECONDS
    """
    if _BRIDGE_ACK_TIMEOUT_SECONDS <= 0:
        return 0

    cutoff = datetime.now() - timedelta(seconds=_BRIDGE_ACK_TIMEOUT_SECONDS)
    flagged_count = 0

    async with get_session() as db:
        stmt = (
            select(Order)
            .where(
                and_(
                    Order.status == OrderStatus.SUBMITTED,
                    Order.trading_mode == TradingMode.REAL,
                    Order.submitted_at <= cutoff,
                    Order.exchange_order_id.is_(None),
                    Order.remarks.is_not(None),
                    Order.remarks.like(f"%{_AWAITING_BRIDGE_ACK_MARKER}%"),
                    ~Order.remarks.like(f"%{_BRIDGE_ACK_TIMEOUT_MARKER}%"),
                )
            )
            .limit(500)
        )
        result = await db.execute(stmt)
        orders = list(result.scalars().all())

        for order in orders:
            try:
                client_order_id = getattr(order, "client_order_id", None)
                symbol = getattr(order, "symbol", "")
                side = getattr(order, "side", "")
                suffix = (
                    f"{_BRIDGE_ACK_TIMEOUT_MARKER} "
                    f"[PENDING_REVIEW: bridge_ack_timeout={_BRIDGE_ACK_TIMEOUT_SECONDS}s, "
                    f"submitted_at={order.submitted_at}]"
                )
                order.remarks = f"{(order.remarks or '').strip()} {suffix}".strip()
                flagged_count += 1
                logger.warning(
                    "order %s flagged for bridge ack timeout review=%ss (submitted_at=%s client_order_id=%s symbol=%s side=%s)",
                    order.order_id,
                    _BRIDGE_ACK_TIMEOUT_SECONDS,
                    order.submitted_at,
                    str(client_order_id or ""),
                    str(symbol or ""),
                    str(side or ""),
                )
                try:
                    await publish_notification_async(
                        user_id=str(order.user_id),
                        tenant_id=str(order.tenant_id or "default"),
                        title="桥接回报超时待核查",
                        content=(
                            f"{symbol} 订单 {str(order.order_id)[:8]}... "
                            f"桥接 {_BRIDGE_ACK_TIMEOUT_SECONDS} 秒未回报，已标记待核查，暂未判定拒单。"
                        ),
                        type="trading",
                        level="warning",
                        action_url="/trading",
                    )
                except Exception as notify_exc:
                    logger.warning("notify failed for bridge timeout order %s: %s", order.order_id, notify_exc)
            except Exception as exc:
                logger.error("failed to flag bridge timeout order %s: %s", order.order_id, exc)

        if orders:
            await db.commit()

    return flagged_count


async def _flag_stale_broker_managed_once() -> int:
    """对长时间未成交的托管委托（桥/QMT 镜像单）只提醒、不改状态。

    这类委托的终态由桥/轮询器回写（撤单、废单、成交），本地判死会造成
    「柜台还挂着、本地已 EXPIRED」的错位：委托列表看不到真实在途单，
    后续成交回报撞上终态守卫。超时后仅标记一次并推送提醒，交人工决定
    是否撤单（QMT 单可用控制面/委托页撤单；桥单由桥侧处理）。
    """
    cutoff = datetime.now() - timedelta(minutes=_TIMEOUT_MINUTES)
    flagged_count = 0

    async with get_session() as db:
        stmt = (
            select(Order)
            .where(
                and_(
                    Order.status == OrderStatus.SUBMITTED,
                    Order.trading_mode == TradingMode.REAL,
                    Order.submitted_at <= cutoff,
                    _broker_managed_clause(),
                    or_(
                        Order.remarks.is_(None),
                        ~Order.remarks.like(f"%{_STALE_PENDING_MARKER}%"),
                    ),
                )
            )
            .limit(200)
        )
        result = await db.execute(stmt)
        orders = list(result.scalars().all())

        for order in orders:
            try:
                order.remarks = (
                    f"{(order.remarks or '').strip()} "
                    f"{_STALE_PENDING_MARKER} [pending_over={_TIMEOUT_MINUTES}m "
                    f"submitted_at={order.submitted_at}]"
                ).strip()
                flagged_count += 1
                logger.warning(
                    "broker-managed order %s still pending after %dm "
                    "(submitted_at=%s symbol=%s remarks=%s)",
                    order.order_id,
                    _TIMEOUT_MINUTES,
                    order.submitted_at,
                    order.symbol,
                    order.remarks,
                )
                try:
                    await publish_notification_async(
                        user_id=str(order.user_id),
                        tenant_id=str(order.tenant_id or "default"),
                        title="委托长时间未成交",
                        content=(
                            f"{order.symbol} 订单 {str(order.order_id)[:8]}... "
                            f"已在柜台挂 {_TIMEOUT_MINUTES} 分钟未成交（状态以柜台为准，"
                            "本次不判过期）。如需撤单请在委托页面操作。"
                        ),
                        type="trading",
                        level="warning",
                        action_url="/trading",
                    )
                except Exception as notify_exc:  # noqa: BLE001
                    logger.warning(
                        "notify failed for stale broker-managed order %s: %s",
                        order.order_id,
                        notify_exc,
                    )
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "failed to flag stale broker-managed order %s: %s", order.order_id, exc
                )

        if orders:
            await db.commit()

    return flagged_count


async def run_order_timeout_scanner() -> None:
    """后台无限循环，定期扫描悬挂订单。"""
    logger.info(
        "Order timeout scanner started: timeout=%dm, interval=%ds, bridge_ack_timeout=%ss, bridge_scan_interval=%ss",
        _TIMEOUT_MINUTES,
        _SCAN_INTERVAL,
        _BRIDGE_ACK_TIMEOUT_SECONDS,
        _BRIDGE_ACK_SCAN_INTERVAL,
    )
    next_long_scan = datetime.now()
    while True:
        await asyncio.sleep(max(1, _BRIDGE_ACK_SCAN_INTERVAL))
        try:
            bridge_count = await _scan_bridge_ack_timeout_once()
            if bridge_count:
                logger.info("Order timeout scanner: flagged %d bridge-timeout order(s) for review", bridge_count)

            now = datetime.now()
            if now >= next_long_scan:
                count = await _scan_once()
                if count:
                    logger.info("Order timeout scanner: expired %d order(s)", count)
                stale_count = await _flag_stale_broker_managed_once()
                if stale_count:
                    logger.info(
                        "Order timeout scanner: flagged %d stale broker-managed order(s)",
                        stale_count,
                    )
                next_long_scan = now + timedelta(seconds=max(1, _SCAN_INTERVAL))
        except Exception as exc:
            logger.error("Order timeout scanner error: %s", exc)
