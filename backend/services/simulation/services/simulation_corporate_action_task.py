"""模拟盘公司行为每日任务（QuantDB 数据源, 开盘前同步并应用）。

QuantDB 每日凌晨自动更新 dividend_factors；本任务在每交易日 08:30 后：
  1. 同步窗口内公司行为 → simulation_corporate_actions (status=pending)
  2. apply_due_actions() 把到期事件应用到持仓/现金流/账户投影

时序: 08:30 (数据已更新、开盘前) → 09:16 T+1 解锁 → 09:30 开盘,
除权事件在开盘前完成入账, 盘中调仓看到的就是除权后口径。
进程重启会补跑当日任务; 同步与 apply 均幂等。
"""

import asyncio
import logging
from datetime import datetime

from backend.services.simulation.services.corporate_action_quantdb_sync import (
    sync_corporate_actions_from_quantdb,
)
from backend.services.simulation.services.corporate_action_service import (
    SimulationCorporateActionService,
)

logger = logging.getLogger(__name__)

_SYNC_HOUR = 8
_SYNC_MINUTE = 30
_CHECK_INTERVAL_SECONDS = 60


async def run_simulation_corporate_action_task(
    interval_seconds: int = _CHECK_INTERVAL_SECONDS,
) -> None:
    """每交易日 08:30 后同步 QuantDB 公司行为并应用到模拟盘账户（幂等）。"""
    last_date = ""
    while True:
        from backend.shared.scheduler_registry import heartbeat as _sched_heartbeat

        _sched_heartbeat("corp_action")
        try:
            now = datetime.now()
            today = now.strftime("%Y%m%d")
            if today != last_date and (now.hour, now.minute) >= (
                _SYNC_HOUR,
                _SYNC_MINUTE,
            ):
                last_date = today
                if now.weekday() >= 5:
                    continue  # 周末无除权, 周一 08:30 自然补齐
                inserted = await sync_corporate_actions_from_quantdb()
                applied = await SimulationCorporateActionService.apply_due_actions()
                if inserted or applied:
                    logger.info(
                        "模拟盘公司行为: 同步 %d 条, 应用 %d 条", inserted, applied
                    )
        except Exception as exc:
            logger.warning("模拟盘公司行为任务异常: %s", exc, exc_info=True)
        await asyncio.sleep(interval_seconds)
