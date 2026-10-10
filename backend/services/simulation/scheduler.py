"""
SimulationScheduler - 模拟盘手动运维触发器（已下线自动循环）。

自动调仓唯一触发源：SimulationHostedScheduler（按 trade:active_strategy 的
live_trade_config 触发，见 services/simulation_hosted_scheduler.py）。
本类仅保留 run_all_users，供运维手动补跑全量账户调仓，不再有定时循环、
整分命中、T+1 解锁（T+1 已解耦到 simulation_t1_unlock_task 独立任务）。
"""

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from backend.services.trade_shared.redis_client import RedisClient
from backend.services.simulation.engine import SimulationEngine, simulation_engine
from backend.services.simulation.services.simulation_manager import (
    canonical_sim_uid,
)
from backend.shared.database_manager_v2 import get_db_manager  # noqa: F401 保留：手动补跑脚本可能用到

logger = logging.getLogger(__name__)


@dataclass
class ActiveSimulationAccount:
    """激活的模拟盘账户"""

    tenant_id: str
    user_id: str
    strategy_id: str
    account_id: int


class SimulationScheduler:
    """
    手动补跑器（无自动循环）：

    - 触发源：遍历 simulation:account:* 中绑定了 strategy_id 的账户，
      逐个调用 SimulationEngine.run_cycle。
    - 与托管链路差异：不读 enabled_sessions/sell-buy 窗口/rebalance_days，
      不写 simulation_rebalance_jobs，不做信号批次严格校验。
      仅用于故障后补跑，日常自动调仓请走托管调度器。
    """

    def __init__(
        self,
        engine: SimulationEngine | None = None,
        redis: RedisClient | None = None,
    ):
        self.engine = engine or simulation_engine
        self.redis = redis or RedisClient()

    def _is_trading_day(self, dt: datetime) -> bool:
        """检查是否为交易日（XSHG 日历；日历不可用时回退周一至周五）"""
        try:
            from backend.services.simulation.services.simulation_hosted_scheduler import (
                _is_trading_day as _calendar_is_trading_day,
            )

            return _calendar_is_trading_day(dt.date())
        except Exception as exc:
            logger.debug("SimulationScheduler: 交易日历不可用, 回退周判断: %s", exc)
            return dt.weekday() < 5

    async def run_all_users(self) -> dict[str, Any]:
        """
        手动补跑：遍历所有绑定了策略的模拟盘账户，执行调仓。

        注意：不做 T+1 解锁（独立任务负责），不做去重锁。
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
                "SimulationScheduler: 手动补跑 %d 个账户的调仓",
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
                "SimulationScheduler: 补跑完成, total=%d success=%d failed=%d skipped=%d elapsed=%.2fs",
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
                                data = json.loads(raw)
                                # 检查是否有策略绑定
                                strategy_id = data.get("strategy_id")
                                if strategy_id:
                                    accounts.append(ActiveSimulationAccount(
                                        tenant_id=tenant_id,
                                        user_id=user_id,
                                        strategy_id=str(strategy_id),
                                        account_id=canonical_sim_uid(user_id),
                                    ))
                    except Exception as e:
                        logger.debug("SimulationScheduler: 解析账户 key 失败 %s: %s", key, e)

        except Exception as e:
            logger.error("SimulationScheduler: 加载激活账户失败 %s", e, exc_info=True)

        return accounts

    def _live_trade_config(self, account: ActiveSimulationAccount) -> dict:
        """读取运行时 active_strategy 的 live_trade_config（与托管链路同一事实源）。"""
        try:
            if not self.redis.client:
                return {}
            from backend.shared.simulation_account_keys import active_strategy_key

            raw = self.redis.client.get(
                active_strategy_key(account.tenant_id, account.user_id)
            )
            if not raw:
                return {}
            data = json.loads(raw)
            if not isinstance(data, dict):
                return {}
            if str(data.get("strategy_id") or "").strip() != str(account.strategy_id):
                return {}
            cfg = data.get("live_trade_config")
            if isinstance(cfg, str):
                cfg = json.loads(cfg)
            return cfg if isinstance(cfg, dict) else {}
        except Exception as e:
            logger.debug(
                "SimulationScheduler: 解析账户托管配置失败 tenant=%s user=%s: %s",
                account.tenant_id,
                account.user_id,
                e,
            )
            return {}

    def _resolve_pool_ref(self, account: ActiveSimulationAccount) -> str | None:
        """从运行时 active_strategy 配置取全局股票池引用。"""
        return str(self._live_trade_config(account).get("pool_id") or "").strip() or None

    def _resolve_max_orders(self, account: ActiveSimulationAccount) -> int | None:
        """从运行时 active_strategy 配置取单轮订单上限。"""
        try:
            value = int(self._live_trade_config(account).get("max_orders_per_cycle") or 0)
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None

    async def _run_single_account(self, account: ActiveSimulationAccount) -> bool:
        """执行单个账户的调仓"""
        try:
            report = await self.engine.run_cycle(
                tenant_id=account.tenant_id,
                user_id=account.user_id,
                strategy_id=account.strategy_id,
                pool_id=self._resolve_pool_ref(account),
                max_orders=self._resolve_max_orders(account),
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
