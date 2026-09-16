"""
Pending simulation order worker.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone

from sqlalchemy import or_, select

from backend.services.trade_shared.redis_client import redis_client
from backend.services.simulation.models.order import OrderStatus
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

    async def run_once(self) -> int:
        processed = 0
        async with get_session(read_only=False) as session:
            rows = list(
                (
                    await session.execute(
                        select(SimulationOrderV2)
                        .where(
                            SimulationOrderV2.status == OrderStatus.PENDING.value,
                            or_(
                                SimulationOrderV2.expires_at.is_(None),
                                SimulationOrderV2.expires_at > datetime.now(),
                            ),
                        )
                        .order_by(
                            SimulationOrderV2.created_at.asc(),
                            SimulationOrderV2.id.asc(),
                        )
                        .limit(self.batch_size)
                    )
                )
                .scalars()
                .all()
            )
            if not rows:
                return 0

            manager = SimulationAccountManager(redis_client)
            order_service = SimOrderService(session)
            engine = SimulationExecutionEngine(session, manager)

            for projection_order in rows:
                runtime_order = await order_service.load_runtime_order(
                    projection_order
                )
                # 防御：v1 台账行已终态（撤单/成交/拒单）而 V2 投影滞留 pending——
                # 属投影陈旧（历史缺口），以 v1 为准镜像回写，绝不重放执行。
                if getattr(runtime_order, "status", None) in (
                    OrderStatus.FILLED,
                    OrderStatus.CANCELLED,
                    OrderStatus.REJECTED,
                ):
                    await order_service.sync_order_projection(runtime_order)
                    processed += 1
                    continue
                expires_at = engine._normalize_runtime_datetime(
                    getattr(runtime_order, "expires_at", None)
                )
                if expires_at is not None and expires_at <= datetime.now():
                    await engine.mark_expired(
                        runtime_order,
                        "Order expired before execution",
                    )
                    await order_service.sync_order_projection(
                        runtime_order,
                        rejected_reason="Order expired before execution",
                    )
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

                        execution_result = await engine.execute_order(runtime_order)
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
