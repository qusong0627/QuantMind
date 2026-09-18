"""
Pending simulation order worker.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone

from sqlalchemy import select, update

from backend.services.trade_shared.redis_client import redis_client
from backend.services.simulation.models.order import OrderStatus, SimOrder
from backend.services.simulation.models.order_v2 import SimulationOrderV2
from backend.services.simulation.services.execution_engine import (
    SimulationExecutionEngine,
)
from backend.services.simulation.services.order_service import SimOrderService
from backend.services.simulation.services.simulation_manager import (
    SimulationAccountManager,
)
from backend.shared.database_manager_v2 import get_session

logger = logging.getLogger(__name__)


class SimulationPendingOrderWorker:
    def __init__(self, interval_seconds: int = 15, batch_size: int = 50):
        self.interval_seconds = max(3, int(interval_seconds or 15))
        self.batch_size = max(1, int(batch_size or 50))

    async def run_once(self, tenant_id: str | None = None) -> int:
        """扫描并处理 pending 挂单。

        tenant_id 非空时只扫该租户（测试隔离/运维按租户处置用；缺省全量，保持既有语义）。
        """
        processed = 0
        async with get_session(read_only=False) as session:
            stmt = (
                select(SimulationOrderV2)
                .where(
                    SimulationOrderV2.status == OrderStatus.PENDING.value,
                )
                .order_by(
                    SimulationOrderV2.created_at.asc(),
                    SimulationOrderV2.id.asc(),
                )
                .limit(self.batch_size)
            )
            if tenant_id:
                stmt = stmt.where(
                    SimulationOrderV2.tenant_id == str(tenant_id)
                )
            rows = list((await session.execute(stmt)).scalars().all())
            if not rows:
                return 0

            manager = SimulationAccountManager(redis_client)
            order_service = SimOrderService(session)
            engine = SimulationExecutionEngine(session, manager)

            for projection_order in rows:
                runtime_order = (
                    await session.execute(
                        select(SimOrder)
                        .where(SimOrder.order_id == projection_order.order_id)
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if runtime_order is None:
                    logger.error(
                        "pending projection has no legacy order: %s",
                        projection_order.order_id,
                    )
                    projection_order.status = OrderStatus.REJECTED.value
                    projection_order.rejected_reason = "legacy order missing"
                    await session.commit()
                    processed += 1
                    continue
                legacy_status = str(
                    getattr(runtime_order.status, "value", runtime_order.status) or ""
                )
                if legacy_status in {
                    OrderStatus.REJECTED.value,
                    OrderStatus.CANCELLED.value,
                    OrderStatus.FILLED.value,
                }:
                    projection_order.status = legacy_status
                    await session.commit()
                    processed += 1
                    continue
                expires_at = engine._normalize_runtime_datetime(
                    getattr(projection_order, "expires_at", None)
                )
                now_utc_naive = datetime.now(timezone.utc).replace(tzinfo=None)
                if expires_at is not None and expires_at <= now_utc_naive:
                    await engine.mark_expired(
                        runtime_order,
                        "Order expired before execution",
                    )
                    projection_order.status = "expired"
                    projection_order.rejected_reason = "Order expired before execution"
                    await session.commit()
                    processed += 1
                    continue

                session_decision = await engine.assess_execution_window(runtime_order)
                if session_decision.target_trade_date is not None:
                    runtime_order.trading_session_date = (
                        session_decision.target_trade_date
                    )
                if not session_decision.can_execute:
                    if session_decision.final_state == "expired":
                        await engine.mark_expired(
                            runtime_order, session_decision.message
                        )
                        await order_service.sync_order_projection(
                            runtime_order,
                            rejected_reason=str(session_decision.message or "")[:500],
                        )
                        processed += 1
                        continue
                    if not session_decision.retryable:
                        await engine.mark_rejected(
                            runtime_order, session_decision.message
                        )
                        await order_service.sync_order_projection(
                            runtime_order,
                            rejected_reason=str(session_decision.message or "")[:500],
                        )
                        processed += 1
                    else:
                        await order_service.queue_order(
                            runtime_order,
                            session_decision.message,
                            trading_session_date=session_decision.target_trade_date,
                        )
                        processed += 1
                    continue

                # P0-2：派发环节 fresh 风控复检（入队时刻的时段/时效约束已按语义降级为
                # 告警，此处才是真正的"申报前闸"——行情按当前 fresh 口径判定）。
                # 判定 passed=False（含闸内部 fail-closed）→ 拒单；闸 plumbing 异常 →
                # 本轮推迟（订单保持 pending，下轮重扫，不因基础设施抖动误杀用户单）。
                try:
                    from types import SimpleNamespace as _SNS

                    from backend.services.trade.services.risk_gate_service import (
                        check_order as _risk_check,
                    )

                    if getattr(redis_client, "client", None) is None:
                        redis_client.connect()
                    _side = getattr(runtime_order.side, "value", runtime_order.side)
                    _otype = getattr(
                        runtime_order.order_type, "value", runtime_order.order_type
                    )
                    _risk = await _risk_check(
                        _SNS(
                            tenant_id=str(getattr(runtime_order, "tenant_id", "default")),
                            user_id=int(getattr(runtime_order, "user_id", 0) or 0),
                            symbol=str(getattr(runtime_order, "symbol", "")),
                            side=str(_side or "").lower(),
                            quantity=float(projection_order.quantity or 0.0),
                            price=(
                                float(runtime_order.price)
                                if getattr(runtime_order, "price", None)
                                else None
                            ),
                            order_type=str(_otype or "market").lower(),
                            trading_mode="SIMULATION",
                            source="pending_dispatch",
                            remarks=getattr(runtime_order, "remarks", None),
                            client_order_id=str(
                                getattr(runtime_order, "client_order_id", "") or ""
                            ),
                            strategy_id=str(getattr(runtime_order, "strategy_id", "") or ""),
                        ),
                        db=session,
                        redis=redis_client,
                    )
                    if not _risk.passed:
                        _msg = (
                            f"风控拒单[{_risk.rule_id or 'risk'}]（派发复检）: "
                            f"{_risk.reason}"
                        )
                        await engine.mark_rejected(runtime_order, _msg)
                        await order_service.sync_order_projection(
                            runtime_order, rejected_reason=_msg[:500]
                        )
                        processed += 1
                        continue
                except Exception as exc:  # noqa: BLE001 - plumbing 抖动=推迟本轮
                    logger.warning(
                        "pending dispatch risk recheck deferred: %s", exc
                    )
                    continue

                claim = await session.execute(
                    update(SimulationOrderV2)
                    .where(
                        SimulationOrderV2.id == projection_order.id,
                        SimulationOrderV2.status == OrderStatus.PENDING.value,
                    )
                    .values(
                        status=OrderStatus.SUBMITTED.value,
                        rejected_reason=None,
                    )
                )
                if claim.rowcount != 1:
                    await session.rollback()
                    continue
                await session.commit()

                # P0-1：执行+落库临界区持同用户锁，与在线下单链路互斥。
                # 锁忙则本轮跳过（订单仍pending，下轮再扫），不静默放行。
                try:
                    _lock_cm = SimulationAccountManager.locked_execution(
                        int(getattr(runtime_order, "user_id", 0) or 0),
                        str(getattr(runtime_order, "tenant_id", "default")),
                    )
                except Exception:
                    _lock_cm = None
                if _lock_cm is None:
                    projection_order.status = OrderStatus.PENDING.value
                    await session.commit()
                    continue
                try:
                    async with _lock_cm:
                        runtime_order.status = OrderStatus.SUBMITTED
                        runtime_order.submitted_at = datetime.now(timezone.utc)
                        await order_service.sync_order_projection(
                            runtime_order,
                            rejected_reason=None,
                        )
                        await session.commit()

                        execution_result = await engine.execute_order(
                            runtime_order,
                            requested_quantity=float(projection_order.quantity or 0.0),
                        )
                        if not execution_result.success:
                            if (
                                str(execution_result.message or "")
                                == "Order expired before execution"
                            ):
                                await engine.mark_expired(
                                    runtime_order, execution_result.message
                                )
                            elif "queued for next valid session" in str(
                                execution_result.message or ""
                            ):
                                await order_service.queue_order(
                                    runtime_order,
                                    str(execution_result.message or ""),
                                )
                                processed += 1
                                continue
                            else:
                                await engine.mark_rejected(
                                    runtime_order, execution_result.message
                                )
                            # 终态镜像 V2 投影（否则订单卡 submitted、拒因丢失；
                            # 拒因为 None 时 sync 保留既有值，故成交/过期分支同样无害）
                            await order_service.sync_order_projection(
                                runtime_order,
                                rejected_reason=str(
                                    execution_result.message or ""
                                )[:500]
                                or None,
                            )
                            processed += 1
                            continue

                        await engine.apply_filled(runtime_order, execution_result)
                        # 成交镜像 V2 投影（v1 行由 apply_filled 落库，v2 此前永远卡
                        # submitted——用户可见订单列表读 v1，V2 供幂等/审计）
                        await order_service.sync_order_projection(runtime_order)
                        processed += 1
                except RuntimeError:
                    projection_order.status = OrderStatus.PENDING.value
                    await session.commit()
                    continue
                except Exception:
                    await session.rollback()
                    await session.execute(
                        update(SimulationOrderV2)
                        .where(
                            SimulationOrderV2.id == projection_order.id,
                            SimulationOrderV2.status
                            == OrderStatus.SUBMITTED.value,
                        )
                        .values(status=OrderStatus.PENDING.value)
                    )
                    await session.commit()
                    logger.error(
                        "pending order execution failed: %s",
                        projection_order.order_id,
                        exc_info=True,
                    )
                    continue
        return processed


async def run_simulation_pending_order_worker() -> None:
    interval = int(
        str(os.getenv("SIM_PENDING_ORDER_WORKER_INTERVAL_SECONDS", "15")).strip()
        or "15"
    )
    batch_size = int(
        str(os.getenv("SIM_PENDING_ORDER_WORKER_BATCH_SIZE", "50")).strip() or "50"
    )
    worker = SimulationPendingOrderWorker(
        interval_seconds=interval,
        batch_size=batch_size,
    )
    logger.info(
        "simulation pending order worker started interval=%ss batch_size=%s",
        worker.interval_seconds,
        worker.batch_size,
    )
    while True:
        from backend.shared.scheduler_registry import heartbeat as _sched_heartbeat

        _sched_heartbeat("pending_order")
        try:
            count = await worker.run_once()
            if count:
                logger.info(
                    "simulation pending order worker processed %s order(s)", count
                )
        except asyncio.CancelledError:
            logger.info("simulation pending order worker cancelled")
            raise
        except Exception as exc:
            logger.error(
                "simulation pending order worker failed: %s", exc, exc_info=True
            )
        await asyncio.sleep(worker.interval_seconds)
