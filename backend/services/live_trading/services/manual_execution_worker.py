from __future__ import annotations

import asyncio
import logging

from .manual_execution_persistence import manual_execution_persistence
from .manual_execution_service import manual_execution_service

logger = logging.getLogger(__name__)


async def run_manual_execution_worker(poll_interval: float = 1.0) -> None:
    """轮询 trade_manual_execution_tasks，执行 queued 的手动任务。"""
    interval = max(0.2, float(poll_interval or 1.0))
    logger.info("manual execution worker started, poll_interval=%s", interval)
    last_heartbeat = 0.0
    try:
        while True:
            try:
                # T-RC-14：心跳按**轮次**写而非按任务写——空队列期才是最容易悄悄
                # 死掉却无人发现的时段。限流到 30s 一次，避免 1s 轮询空刷 Redis。
                now_monotonic = asyncio.get_running_loop().time()
                if now_monotonic - last_heartbeat >= 30.0:
                    last_heartbeat = now_monotonic
                    try:
                        from backend.shared.scheduler_registry import (
                            heartbeat as _sched_heartbeat,
                        )

                        _sched_heartbeat("manual_execution")
                    except Exception as hb_exc:  # noqa: BLE001 - 心跳失败不影响消费
                        logger.debug("manual_execution heartbeat failed: %s", hb_exc)

                task = await manual_execution_persistence.claim_next_queued_task()
                if task is None:
                    await asyncio.sleep(interval)
                    continue
                await manual_execution_service.process_task(task)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("manual execution worker loop failed: %s", exc, exc_info=True)
                await asyncio.sleep(interval)
    except asyncio.CancelledError:
        logger.info("manual execution worker cancelled")
        raise

