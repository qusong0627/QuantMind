from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, Optional

from fastapi import HTTPException
from sqlalchemy import and_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.trade_shared.models.enums import OrderSide, OrderStatus, OrderType, PositionSide, TradeAction, TradingMode
from backend.services.trade_shared.models.order import Order
from backend.services.trade_shared.portfolio.models import Portfolio
from backend.services.trade_shared.redis_client import RedisClient
from backend.services.trade_shared.schemas.order import OrderCreate
from backend.services.trade_shared.services.order_service import OrderService
from backend.services.trade_shared.simulation_manager import SimulationAccountManager
from backend.services.live_trading.services.trading_engine import TradingEngine
from backend.services.live_trading.routers.real_trading_utils import (
    _fetch_active_portfolio_snapshot,
    normalize_db_user_id,
)
from backend.services.live_trading.services.real_mirror_service import (
    mirror_virtual_fill,
)

logger = logging.getLogger(__name__)

_TRADE_ACTION_ALIAS = {
    "open": "buy_to_open",
    "buy_open": "buy_to_open",
    "buy_to_open": "buy_to_open",
    "close": "sell_to_close",
    "sell_close": "sell_to_close",
    "sell_to_close": "sell_to_close",
    "short": "sell_to_open",
    "sell_open": "sell_to_open",
    "sell_to_open": "sell_to_open",
    "cover": "buy_to_close",
    "buy_close": "buy_to_close",
    "buy_to_close": "buy_to_close",
}


def _normalize_trade_action(raw: Any) -> str | None:
    value = str(getattr(raw, "value", raw) or "").strip().lower()
    return _TRADE_ACTION_ALIAS.get(value, value) or None


def _normalize_strategy_id(raw: Any) -> int | None:
    """strategy_id → 正整数或 None（0/负数/非数字一律 None）。

    ``OrderCreate.strategy_id`` 是 ``gt=0``：内部策略/镜像真单没挂策略时
    传的是 ``"0"``，``isdigit()`` 为真会直接构造出 ``strategy_id=0`` 触发
    422/500（压测实测：镜像单全部被 OrderCreate 校验打回）。DB 列本身
    nullable 且无外键，None 是「无策略」的正确表达。
    """
    value = str(raw or "").strip()
    if not value.isdigit():
        return None
    parsed = int(value)
    return parsed if parsed > 0 else None


# 强平/止损类来源标记：这些订单"一定要成交"，镜像时豁免 2% 偏离闸门
# （限价改用盘口价基准，见 real_mirror_service._submit_payload）。
_FORCED_EXIT_REMARK_PREFIXES = ("sltp:", "flatten:", "forced-exit:")

_MIRROR_REASON_CN = {
    "price_drift": "价格偏离昨收超过镜像闸门（±2%），疑似行情脱钩",
    "no_reference_price": "取不到参考价（QuantDB 昨收缺失）",
    "insufficient_cash": "真账户可用资金不足",
    "insufficient_position": "真账户可用持仓不足（T+1 或已被占用）",
    "mirror_disabled": "镜像开关未开启",
    "kill_switch": "镜像急停生效中",
    "whitelist": "不在镜像白名单",
    "blacklist": "标的在黑名单",
    "outside_trading_hours": "非交易时段且未开启排队",
    "invalid_quantity_or_price": "数量或价格非法",
    "max_order_value": "超过单笔金额上限",
    "max_daily_value": "超过单日累计金额上限",
    "max_daily_orders": "超过单日笔数上限",
    "max_daily_symbols": "超过单日标的数上限",
    "price_sanity": "价格偏离昨收超过 20%，按脏数据 fail-closed",
}


def _is_forced_exit(remarks: Any) -> bool:
    """备注前缀识别强平/止损来源（``sltp:`` / ``flatten:`` / ``forced-exit:``）。"""
    text = str(remarks or "").strip().lower()
    return text.startswith(_FORCED_EXIT_REMARK_PREFIXES)


def _mirror_reason_cn(reason: str) -> str:
    key = str(reason or "").strip()
    if key in _MIRROR_REASON_CN:
        return _MIRROR_REASON_CN[key]
    if key.startswith("account_unavailable"):
        return "真账户查询失败"
    if key.startswith("market_not_supported"):
        return f"镜像未支持该市场（{key.split(':', 1)[-1]}）"
    return key or "未知原因"


def _mirror_notice_allowed(redis: Any, symbol: str, reason: str, ttl: int = 1800) -> bool:
    """同一标的同一原因 30 分钟内只通知一次（急跌日避免刷屏）。"""
    client = getattr(redis, "client", None)
    if client is None:
        return True
    try:
        key = f"mirror:notify:{symbol}:{reason}"
        return bool(client.set(key, "1", nx=True, ex=ttl))
    except Exception:  # noqa: BLE001 - 通知节流失效不阻断主流程
        return True


async def _notify_mirror_outcome(
    mirror_result: dict[str, Any] | None,
    *,
    redis: Any,
    tenant: str,
    user_id: str,
    symbol: str,
    side: str,
    quantity: float,
) -> None:
    """把镜像 ``skipped/failed/error`` 上抛给用户（原来只写日志，用户无从知晓）。"""
    status = str((mirror_result or {}).get("status") or "")
    if status not in {"skipped", "failed", "error"}:
        return
    reason = str((mirror_result or {}).get("reason") or "")
    if not _mirror_notice_allowed(redis, symbol, f"{status}:{reason}"):
        return
    try:
        from backend.shared.notification_publisher import publish_notification_async

        await publish_notification_async(
            user_id=str(user_id),
            tenant_id=str(tenant or "default"),
            title=f"{symbol} 真单镜像未下单",
            content=(
                f"模拟{('买入' if str(side).upper() == 'BUY' else '卖出')} {quantity:g} 股已记账，"
                f"但真单镜像未下发：{_mirror_reason_cn(reason)}。实盘账户没有这笔委托。"
            ),
            type="trading",
            level="warning",
            action_url="/trading",
        )
    except Exception as exc:  # noqa: BLE001 - 通知失败不影响主流程
        logger.warning("[Shadow/Sim] 镜像结果通知失败: %s", exc)


async def _fetch_latest_real_account_snapshot(
    db: AsyncSession, *, tenant_id: str, user_id: str
) -> dict[str, Any] | None:
    """**当日**最近一笔真账户快照（``real_account_snapshots``，按 tenant/user 过滤）。

    user_id 两种存量口径都试（补零 ``00000001`` 与裸 ``1``），快照写入方
    （bridge ``ctx.user_id``）与调度链路的用户口径未必一致。

    只取当日快照：隔日快照的可用量（T+1 解锁、当日买卖）已经过期，拿它做整手
    预检会把合法的全量卖出当成碎股拦掉。取不到（含写入方用 UTC 日期导致对不上）
    就返回 ``None``——调用方拿不准时放行，柜台 ``251150`` 仍是最终闸门。
    """
    from datetime import datetime

    from backend.services.live_trading.services.trading_session import TZ
    from backend.services.trade_shared.models.real_account_snapshot import (
        RealAccountSnapshot,
    )

    raw_uid = str(user_id or "").strip()
    uid_candidates = {raw_uid, normalize_db_user_id(raw_uid)}
    row = (
        await db.execute(
            select(RealAccountSnapshot)
            .where(
                RealAccountSnapshot.tenant_id == str(tenant_id or "default"),
                RealAccountSnapshot.user_id.in_(sorted(uid_candidates)),
                RealAccountSnapshot.snapshot_date == datetime.now(TZ).date(),
            )
            .order_by(RealAccountSnapshot.snapshot_at.desc())
            .limit(1)
        )
    ).scalars().first()
    if row is None:
        return None
    return {"payload_json": getattr(row, "payload_json", None) or {}}


# 快照可用量与实际可用量的容差：差异在 1% 以内视为同一持仓，按全量卖出放行
_FULL_EXIT_TOLERANCE = 0.99


async def _sell_lot_violation(
    db: AsyncSession,
    *,
    tenant: str,
    user_id: str,
    symbol: str,
    side: str,
    quantity: float,
) -> str | None:
    """卖单整手预检（人类可读原因；``None`` = 放行）。

    只有能从**当日**真账户快照确认「这不是全量卖出」时才拦——全量卖出允许碎股，
    且快照可能滞后，拿不准时不拦（柜台 ``251150`` 仍是最终闸门）。
    整手规则是 A 股口径，非 A 股标的（港股/美股等）不做预检。
    """
    from backend.services.live_trading.services import lot_rules
    from backend.shared.stock_utils import StockCodeUtil

    qty = float(quantity or 0)
    if qty != int(qty) or qty <= 0:
        return f"数量必须为正整数股，got {quantity}"
    if str(side or "").upper() != "SELL":
        return None
    target = StockCodeUtil.to_suffix(str(symbol or ""))
    if not str(target or "").endswith((".SH", ".SZ", ".BJ")):
        return None
    available: float | None = None
    try:
        snapshot = await _fetch_latest_real_account_snapshot(
            db, tenant_id=tenant, user_id=user_id
        )
        payload = (snapshot or {}).get("payload_json") or {}
        for item in payload.get("positions") or []:
            code = StockCodeUtil.to_suffix(
                str(item.get("stock_code") or item.get("symbol") or "")
            )
            if code and code == target:
                available = float(
                    item.get("available_volume")
                    or item.get("can_use_volume")
                    or item.get("volume")
                    or 0
                )
                break
    except Exception as exc:  # noqa: BLE001 - 快照不可用则只做绝对非法检查
        logger.debug("[Order] 真账户快照读取失败，跳过整手预检: %s", exc)
    if available is None or available <= 0:
        return None
    if qty >= available * _FULL_EXIT_TOLERANCE:
        return None  # 全量卖出：允许碎股
    return lot_rules.describe_violation(str(symbol or ""), "SELL", qty)


async def dispatch_internal_strategy_order(
    *,
    order_data: dict[str, Any],
    user_id: str,
    tenant_id: str,
    redis: RedisClient,
    db: AsyncSession,
) -> dict[str, Any]:
    """复用内部策略下单逻辑：实盘走真实风控/柜台，影子/模拟走虚拟成交。"""
    # user_id 口径与模拟盘接口（simulation.py _require_user_id）对齐：
    # 非数字 JWT sub 统一映射为 0，保证命中同一模拟账户 Redis 键。
    _uid_raw = str(user_id or "").strip()
    uid = int(_uid_raw) if _uid_raw.isdigit() else 0
    # DB 口径：orders/portfolios 的 user_id 列是 VARCHAR，库里存 8 位补零字符串
    # （"1" 与 "00000001" 是同一个用户，但 SQL 里对不上）。面向 DB 的查询一律用它。
    db_uid = normalize_db_user_id(_uid_raw)
    tenant = (tenant_id or "").strip() or "default"
    trading_mode_raw = str(order_data.get("trading_mode", "REAL")).upper()
    try:
        trading_mode = TradingMode(trading_mode_raw)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"invalid trading_mode: {trading_mode_raw}")

    symbol = str(order_data.get("symbol") or "").strip().upper()
    side_raw = str(order_data.get("side") or "").strip().upper()
    quantity = float(order_data.get("quantity") or 0)
    price = float(order_data.get("price") or 0)
    order_type_raw = str(order_data.get("order_type") or "LIMIT").strip().upper()
    trade_action_raw = _normalize_trade_action(order_data.get("trade_action"))
    position_side_raw = str(order_data.get("position_side") or "long").strip().lower()
    is_margin_trade = bool(order_data.get("is_margin_trade", False))
    client_order_id = str(order_data.get("client_order_id") or "").strip() or None
    remarks = order_data.get("remarks")

    if client_order_id is None:
        client_order_id = f"auto-{uuid.uuid4().hex}"

    if not symbol:
        raise HTTPException(status_code=400, detail="missing symbol")
    if side_raw not in {"BUY", "SELL"}:
        raise HTTPException(status_code=400, detail=f"invalid side: {side_raw}")
    if quantity <= 0:
        raise HTTPException(status_code=400, detail="quantity must be > 0")

    logger.info(
        "[Order] 收到信号 | 租户=%s 模式=%s | %s %s @ %s | trade_action=%s position_side=%s margin=%s",
        tenant,
        trading_mode.value,
        side_raw,
        symbol,
        price,
        trade_action_raw,
        position_side_raw,
        is_margin_trade,
    )

    if trading_mode in {TradingMode.SHADOW, TradingMode.SIMULATION}:
        sim_manager = SimulationAccountManager(redis)
        try:
            from datetime import datetime, timezone

            from backend.services.simulation.models.order import (
                OrderSide as SimOrderSide,
                OrderStatus as SimOrderStatus,
                OrderType as SimOrderType,
                SimOrder,
            )
            from backend.services.simulation.models.trade import SimTrade

            # 幂等：同一 client_order_id 已落账则跳过，防止任务重试导致重复扣款/加仓。
            # sim_orders 无 client_order_id 列，以 remarks 标记作为幂等键。
            dup_marker = f"client_order_id={client_order_id}"
            dup_stmt = (
                select(SimOrder.order_id)
                .where(
                    and_(
                        SimOrder.tenant_id == tenant,
                        SimOrder.user_id == uid,
                        SimOrder.remarks == dup_marker,
                    )
                )
                .limit(1)
            )
            if (await db.execute(dup_stmt)).scalar_one_or_none() is not None:
                return {
                    "status": "success",
                    "execution": "duplicate_skipped",
                    "result": {
                        "success": True,
                        "message": f"duplicate client_order_id skipped: {client_order_id}",
                    },
                }

            side = 1 if side_raw == "BUY" else -1
            gross = price * quantity
            # A 股虚拟成交费用：佣金（双向、最低 5 元）+ 印花税（卖出单边），
            # 与 PaperTradingBroker / SimulationExecutionEngine 口径一致，
            # 否则手动/托管任务的虚拟成交不扣费，账户现金与真实券商口径背离。
            commission = 0.0
            stamp_duty = 0.0
            total_fee = 0.0
            if gross > 0:
                try:
                    from backend.services.trade_shared.trade_config import settings

                    commission = max(
                        round(gross * float(settings.SIMULATION_COMMISSION_RATE), 2),
                        float(settings.SIMULATION_COMMISSION_MIN),
                    )
                    try:
                        from backend.services.simulation.services.market_rules import (
                            infer_market,
                        )

                        cn_market = str(infer_market(symbol).value).upper() == "CN"
                    except Exception:  # noqa: BLE001
                        cn_market = True
                    stamp_duty = (
                        round(gross * float(settings.SIMULATION_STAMP_DUTY_RATE), 2)
                        if cn_market and side < 0
                        else 0.0
                    )
                except Exception:  # noqa: BLE001
                    commission = max(round(gross * 0.0003, 2), 5.0)
                    stamp_duty = round(gross * 0.0005, 2) if side < 0 else 0.0
                total_fee = round(commission + stamp_duty, 2)
            delta_cash = -(gross + total_fee) if side > 0 else gross - total_fee
            result = await sim_manager.update_balance(
                user_id=uid,
                symbol=symbol,
                delta_cash=delta_cash,
                delta_volume=quantity if side > 0 else -quantity,
                price=price,
                tenant_id=tenant,
                trade_action=trade_action_raw,
                position_side=position_side_raw,
                is_margin_trade=is_margin_trade,
            )
            if not result.get("success"):
                logger.warning(
                    "[Shadow/Sim] 虚拟成交被账户拒绝: %s %s reason=%s",
                    symbol,
                    side_raw,
                    result.get("reason"),
                )
                return {"status": "failed", "execution": "virtual", "detail": result}

            # 补写模拟订单/成交台账（sim_orders + sim_trades），让仪表盘交易记录、
            # 成交统计等读取侧能查到手动/托管任务的虚拟成交。
            # DB 落账失败不回滚 Redis 账户（账户资金为准），仅记录错误。
            now = datetime.now(timezone.utc)
            strategy_id_raw = str(order_data.get("strategy_id") or "").strip()
            sim_side = SimOrderSide.BUY if side > 0 else SimOrderSide.SELL
            sim_order = SimOrder(
                tenant_id=tenant,
                user_id=uid,
                portfolio_id=0,
                strategy_id=_normalize_strategy_id(strategy_id_raw),
                symbol=symbol,
                side=sim_side,
                order_type=SimOrderType.MARKET if order_type_raw == "MARKET" else SimOrderType.LIMIT,
                status=SimOrderStatus.FILLED,
                quantity=quantity,
                filled_quantity=quantity,
                price=price if price > 0 else None,
                average_price=price,
                order_value=gross,
                filled_value=gross,
                commission=commission,
                total_fee=total_fee,
                submitted_at=now,
                filled_at=now,
                execution_model="virtual_fill",
                price_source="internal_dispatcher",
                remarks=dup_marker,
            )
            db.add(sim_order)
            await db.flush()
            db.add(
                SimTrade(
                    order_id=sim_order.order_id,
                    tenant_id=tenant,
                    user_id=uid,
                    portfolio_id=0,
                    symbol=symbol,
                    side=sim_side,
                    quantity=quantity,
                    price=price,
                    trade_value=gross,
                    commission=commission,
                    stamp_duty=stamp_duty,
                    total_fee=total_fee,
                    # 时区BUG修复：naive utcnow 经会话时区(Asia/Shanghai)会被存成 -8h
                    # 的错误 instant，必须用 aware UTC。
                    executed_at=datetime.now(timezone.utc),
                    price_source="internal_dispatcher",
                )
            )
            try:
                await db.commit()
            except Exception:  # noqa: BLE001
                await db.rollback()
                logger.error(
                    "[Shadow/Sim] 成交台账落库失败（账户已生效）: %s %s",
                    symbol,
                    side_raw,
                    exc_info=True,
                )
            logger.info(
                "[Shadow/Sim] 虚拟成交完成: %s %s qty=%s fee=%.2f",
                symbol,
                side_raw,
                quantity,
                total_fee,
            )
            # 双轨镜像：虚拟成交已生效，按开关/白名单/限额向大 QMT 补一笔真单。
            # mirror_virtual_fill 自身吞掉全部异常，不影响上面的虚拟账本。
            # 强平/止损来源（remarks 前缀）豁免镜像价格闸门：这类单"一定要卖"，
            # 急跌日恰恰偏离最大，闸门放行由保护价兜底。
            remarks_raw = str(order_data.get("remarks") or "")
            mirror_result = await mirror_virtual_fill(
                db=db,
                redis=redis,
                tenant_id=tenant,
                user_id=str(uid),
                symbol=symbol,
                side=side_raw,
                quantity=quantity,
                price=price,
                client_order_id=client_order_id or "",
                strategy_id=strategy_id_raw,
                source=f"internal_dispatcher:{trading_mode.value}",
                bypass_price_gate=bool(order_data.get("bypass_price_gate"))
                or _is_forced_exit(remarks_raw),
            )
            # 镜像结果上抛 + skip/failed 通知（原来被丢弃，用户只看到 success）
            await _notify_mirror_outcome(
                mirror_result,
                redis=redis,
                tenant=tenant,
                user_id=str(uid),
                symbol=symbol,
                side=side_raw,
                quantity=quantity,
            )
            return {
                "status": "success",
                "execution": "virtual",
                "order_id": str(sim_order.order_id),
                "detail": result,
                "mirror": mirror_result,
            }
        except Exception as exc:
            logger.error("[Shadow/Sim] 虚拟成交失败: %s", exc, exc_info=True)
            raise HTTPException(status_code=500, detail=str(exc))

    try:
        strategy_id = order_data.get("strategy_id")
        strategy_id_str = str(strategy_id or "").strip()
        strategy_id_val = _normalize_strategy_id(strategy_id_str)
        portfolio_id = int(order_data.get("portfolio_id") or 0)
        if portfolio_id <= 0:
            snapshot = await _fetch_active_portfolio_snapshot(
                db,
                tenant_id=tenant,
                user_id=db_uid,
                strategy_id=str(strategy_id or ""),
            )
            if snapshot:
                portfolio_id = int(snapshot.get("portfolio_id") or 0)

        if portfolio_id <= 0:
            stmt = (
                select(Portfolio.id)
                .where(
                    and_(
                        Portfolio.tenant_id == tenant,
                        Portfolio.user_id == db_uid,
                        Portfolio.status == "active",
                    )
                )
                .order_by(Portfolio.updated_at.desc())
                .limit(1)
            )
            result = await db.execute(stmt)
            portfolio_id = int(result.scalar_one_or_none() or 0)

        if portfolio_id <= 0:
            # 无组合兜底：本部署的实盘链路不依赖 portfolios 表（通达信桥的真单同样
            # 以 portfolio_id=0 落账），镜像/内部策略真单按 0 归档，不阻断下单。
            logger.info(
                "[Order] REAL 下单无可用组合，按 portfolio_id=0 落账 user=%s symbol=%s",
                db_uid,
                symbol,
            )
            portfolio_id = 0

        try:
            order_type = OrderType(order_type_raw)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"invalid order_type: {order_type_raw}")
        try:
            position_side = PositionSide(position_side_raw)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"invalid position_side: {position_side_raw}")
        trade_action = None
        if trade_action_raw:
            try:
                trade_action = TradeAction(trade_action_raw)
            except ValueError:
                raise HTTPException(status_code=400, detail=f"invalid trade_action: {trade_action_raw}")

        order_service = OrderService(db, redis)
        engine = TradingEngine(db, redis)

        if client_order_id:
            existed_stmt = (
                select(Order)
                .where(
                    and_(
                        Order.tenant_id == tenant,
                        Order.user_id == db_uid,
                        Order.client_order_id == client_order_id,
                    )
                )
                .limit(1)
            )
            existed_result = await db.execute(existed_stmt)
            existed_order = existed_result.scalar_one_or_none()
            if existed_order is not None:
                return {
                    "status": "success",
                    "execution": "duplicate_skipped",
                    "order_id": str(existed_order.order_id),
                    "result": {
                        "success": True,
                        "message": "duplicate client_order_id skipped",
                        "client_order_id": client_order_id,
                    },
                }

        order = await order_service.create_order(
            user_id=db_uid,
            tenant_id=tenant,
            order_data=OrderCreate(
                portfolio_id=portfolio_id,
                strategy_id=strategy_id_val,
                symbol=symbol,
                symbol_name=order_data.get("symbol_name"),
                side=OrderSide(side_raw),
                order_type=order_type,
                quantity=quantity,
                price=price if price > 0 else None,
                trade_action=trade_action,
                position_side=position_side,
                is_margin_trade=is_margin_trade,
                trading_mode=trading_mode,
                client_order_id=client_order_id,
                remarks=remarks,
            ),
        )
    except IntegrityError:
        if not client_order_id:
            raise
        dup_stmt = (
            select(Order)
            .where(
                and_(
                    Order.tenant_id == tenant,
                    Order.user_id == db_uid,
                    Order.client_order_id == client_order_id,
                )
            )
            .limit(1)
        )
        dup_result = await db.execute(dup_stmt)
        dup_order = dup_result.scalar_one_or_none()
        if dup_order is None:
            raise
        return {
            "status": "success",
            "execution": "duplicate_skipped",
            "order_id": str(dup_order.order_id),
            "result": {
                "success": True,
                "message": "duplicate client_order_id skipped",
                "client_order_id": client_order_id,
            },
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Internal order dispatch failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))

    lot_violation = await _sell_lot_violation(
        db, tenant=tenant, user_id=db_uid, symbol=symbol, side=side_raw, quantity=quantity
    )
    if lot_violation:
        await order_service.transition_order_status(
            order, OrderStatus.REJECTED, remarks=f"Lot check failed: {lot_violation}"
        )
        logger.warning(
            "[Order] 数量预检拒绝 %s %s qty=%s: %s", symbol, side_raw, quantity, lot_violation
        )
        return {
            "status": "rejected",
            "execution": "lot_blocked",
            "order_id": str(order.order_id),
            "violations": [{"rule": "lot_size", "message": lot_violation}],
        }

    risk_result = await engine.check_order_risk(uid, order)
    if not risk_result.get("passed"):
        await order_service.transition_order_status(
            order,
            OrderStatus.REJECTED,
            remarks=f"Risk check failed: {risk_result.get('violations')}",
        )
        return {
            "status": "rejected",
            "execution": "risk_blocked",
            "order_id": str(order.order_id),
            "violations": risk_result.get("violations", []),
        }

    submit_result = await engine.submit_order(order, tenant_id=tenant)
    return {
        "status": "success" if submit_result.get("success") else "failed",
        "execution": "direct",
        "order_id": str(order.order_id),
        "result": submit_result,
    }
