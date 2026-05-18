"""
Simulation Scheduler - 模拟盘定时调度器
每日固定时间自动执行调仓，支持多用户多策略并行调度
"""

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.trade.redis_client import RedisClient
from backend.services.trade.simulation.engine import SimulationEngine, simulation_engine
from backend.shared.database_manager_v2 import get_db_manager

logger = logging.getLogger(__name__)

_SH_TZ = ZoneInfo("Asia/Shanghai")


@dataclass
class ActiveSimulationAccount:
    """激活的模拟盘账户"""
    tenant_id: str
    user_id: str
    strategy_id: str
    account_id: int


class SimulationScheduler:
    """
    模拟盘定时调度器：
    - 每日固定时间（如 9:35）自动执行调仓
    - 支持多用户、多策略并行调度
    - 仅在交易日运行
    """

    def __init__(
        self,
        engine: SimulationEngine | None = None,
        redis: RedisClient | None = None,
    ):
        self.engine = engine or simulation_engine
        self.redis = redis or RedisClient()
        self.is_running = False
        self._task: asyncio.Task | None = None

        # 调度时间配置（上海时区）
        self.schedule_time = self._parse_schedule_time()
        self.poll_interval = 60  # 每分钟检查一次

    def _parse_schedule_time(self) -> time:
        """解析调度时间配置"""
        time_str = os.getenv("SIMULATION_SCHEDULE_TIME", "09:35")
        try:
            parts = time_str.split(":")
            return time(int(parts[0]), int(parts[1]) if len(parts) > 1 else 0)
        except Exception:
            return time(9, 35)

    async def start(self) -> None:
        """启动调度器"""
        if self.is_running:
            logger.warning("SimulationScheduler: 已在运行中")
            return

        self.is_running = True
        self._task = asyncio.create_task(self._run_loop(), name="simulation-scheduler")
        logger.info(
            "SimulationScheduler: 已启动, 调度时间=%s",
            self.schedule_time.strftime("%H:%M"),
        )

    async def stop(self) -> None:
        """停止调度器"""
        self.is_running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("SimulationScheduler: 已停止")

    async def _run_loop(self) -> None:
        """调度循环"""
        last_executed_date: str | None = None

        while self.is_running:
            try:
                now = datetime.now(_SH_TZ)
                today = now.strftime("%Y-%m-%d")
                current_time = now.time()

                # 检查是否到达调度时间
                if (
                    self._is_trading_day(now)
                    and today != last_executed_date
                    and current_time.hour == self.schedule_time.hour
                    and current_time.minute == self.schedule_time.minute
                ):
                    logger.info(
                        "SimulationScheduler: 到达调度时间 %s, 开始执行",
                        self.schedule_time.strftime("%H:%M"),
                    )
                    await self.run_all_users()
                    last_executed_date = today

                await asyncio.sleep(self.poll_interval)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("SimulationScheduler: 调度循环异常 %s", e, exc_info=True)
                await asyncio.sleep(self.poll_interval)

    def _is_trading_day(self, dt: datetime) -> bool:
        """检查是否为交易日"""
        # 简单判断：周一到周五
        # TODO: 接入交易日历
        return dt.weekday() < 5

    async def run_all_users(self) -> dict[str, Any]:
        """
        遍历所有激活的模拟盘账户，执行调仓。

        Returns:
            执行统计
        """
        start_time = datetime.now()
        stats = {
            "total": 0,
            "success": 0,
            "failed": 0,
            "skipped": 0,
            "errors": [],
        }

        try:
            # 加载所有激活的模拟盘账户
            active_accounts = await self._load_active_accounts()
            stats["total"] = len(active_accounts)

            if not active_accounts:
                logger.info("SimulationScheduler: 无激活的模拟盘账户")
                return stats

            logger.info(
                "SimulationScheduler: 开始执行 %d 个账户的调仓",
                len(active_accounts),
            )

            # 并行执行（限制并发数）
            semaphore = asyncio.Semaphore(10)  # 最多 10 个并发

            async def run_with_limit(account: ActiveSimulationAccount) -> bool:
                async with semaphore:
                    return await self._run_single_account(account)

            results = await asyncio.gather(
                *[run_with_limit(acc) for acc in active_accounts],
                return_exceptions=True,
            )

            for _, result in enumerate(results):
                if isinstance(result, Exception):
                    stats["failed"] += 1
                    stats["errors"].append(str(result))
                elif result is True:
                    stats["success"] += 1
                else:
                    stats["skipped"] += 1

            elapsed = (datetime.now() - start_time).total_seconds()
            logger.info(
                "SimulationScheduler: 执行完成, total=%d success=%d failed=%d skipped=%d elapsed=%.2fs",
                stats["total"],
                stats["success"],
                stats["failed"],
                stats["skipped"],
                elapsed,
            )

        except Exception as e:
            logger.error("SimulationScheduler: run_all_users 失败 %s", e, exc_info=True)
            stats["errors"].append(str(e))

        return stats

    async def _load_active_accounts(self) -> list[ActiveSimulationAccount]:
        """加载所有激活的模拟盘账户"""
        accounts = []

        try:
            if self.redis.client:
                keys = list(self.redis.client.scan_iter(match="simulation:account:*", count=500))
                for key in keys:
                    try:
                        # 解析 key: simulation:account:{tenant_id}:{user_id}
                        parts = str(key).split(":")
                        if len(parts) >= 4:
                            tenant_id = parts[2]
                            user_id = parts[3]

                            # 获取账户数据，检查是否有绑定策略
                            raw = self.redis.client.get(key)
                            if raw:
                                import json
                                data = json.loads(raw)
                                # 检查是否有策略绑定
                                strategy_id = data.get("strategy_id")
                                if strategy_id:
                                    accounts.append(ActiveSimulationAccount(
                                        tenant_id=tenant_id,
                                        user_id=user_id,
                                        strategy_id=str(strategy_id),
                                        account_id=int(user_id) if user_id.isdigit() else 0,
                                    ))
                    except Exception as e:
                        logger.debug("SimulationScheduler: 解析账户 key 失败 %s: %s", key, e)

        except Exception as e:
            logger.error("SimulationScheduler: 加载激活账户失败 %s", e, exc_info=True)

        return accounts

    async def _run_single_account(self, account: ActiveSimulationAccount) -> bool:
        """执行单个账户的调仓"""
        try:
            report = await self.engine.run_cycle(
                tenant_id=account.tenant_id,
                user_id=account.user_id,
                strategy_id=account.strategy_id,
            )
            return report.error is None
        except Exception as e:
            logger.error(
                "SimulationScheduler: 账户执行失败 tenant=%s user=%s error=%s",
                account.tenant_id,
                account.user_id,
                e,
            )
            return False


simulation_scheduler = SimulationScheduler()
