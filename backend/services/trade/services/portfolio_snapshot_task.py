"""
Portfolio Snapshot Task - 投资组合快照定时任务
"""

import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from backend.shared.database_manager_v2 import get_db_manager
from backend.services.trade_shared.portfolio.models import Portfolio
from backend.services.trade_shared.portfolio.services.portfolio_service import PortfolioService

logger = logging.getLogger(__name__)
_SH_TZ = ZoneInfo("Asia/Shanghai")
_SNAPSHOT_TIMEOUT_SECONDS = 120

async def run_portfolio_snapshot_task(interval_seconds: int = 3600):
    """
    定期为所有活跃投资组合创建快照
    默认每小时执行一次，15:00 时创建专属结算快照
    """
    logger.info("Starting portfolio snapshot task...")

    async def _do_snapshots(is_settlement: bool = False):
        try:
            db_manager = get_db_manager()
            async with db_manager.get_master_session() as db:
                stmt = select(Portfolio).where(Portfolio.is_deleted.is_(False))
                result = await db.execute(stmt)
                portfolios = result.scalars().all()

                for portfolio in portfolios:
                    try:
                        await PortfolioService.calculate_portfolio_metrics(db, portfolio)
                        # 触发快照，带上结算标志位
                        await PortfolioService.create_snapshot(db, portfolio, is_settlement=is_settlement)
                        logger.debug(f"Snapshot created for portfolio {portfolio.id} (settlement={is_settlement})")
                    except Exception as e:
                        logger.error(f"Failed to snapshot portfolio {portfolio.id}: {e}")

                await db.commit()
                logger.info(f"Finished {'settlement ' if is_settlement else ''}snapshots for {len(portfolios)} portfolios")
        except Exception as e:
            logger.error(f"Error during triggering snapshots: {e}")

    async def _do_snapshots_bounded(is_settlement: bool = False):
        try:
            await asyncio.wait_for(
                _do_snapshots(is_settlement=is_settlement),
                timeout=_SNAPSHOT_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.error(
                "Portfolio snapshot task timed out after %ss (settlement=%s)",
                _SNAPSHOT_TIMEOUT_SECONDS,
                is_settlement,
            )

    # 记录今天是否已执行过结算快照 (初始化时检查数据库)
    last_settlement_date = None

    # 启动启动时的补录检查
    now = datetime.now(_SH_TZ)
    if now.hour >= 15:
        # 简单检查：如果现在已经过了 15:00，系统刚启动，尝试触发一次结算
        # 注意：这里逻辑上可以更精细地去查数据库确认今日是否真的有过 is_settlement=True
        logger.info("Service started after 15:00. Triggering potential catch-up settlement...")
        await _do_snapshots_bounded(is_settlement=True)
        last_settlement_date = now.date()
    else:
        logger.info("Triggering initial portfolio snapshots on startup...")
        await _do_snapshots_bounded(is_settlement=False)

    while True:
        try:
            now = datetime.now(_SH_TZ)
            today = now.date()

            # 判断是否到了 15:00 结算时刻
            is_settlement_time = now.hour == 15
            should_settle = is_settlement_time and last_settlement_date != today

            if should_settle:
                logger.info("Target time 15:00 reached. Triggering daily settlement snapshots...")
                await _do_snapshots_bounded(is_settlement=True)
                last_settlement_date = today
            else:
                # 每小时的常规快照
                logger.info("Triggering scheduled portfolio snapshots...")
                await _do_snapshots_bounded(is_settlement=False)

            # 动态计算下一次运行时间，尽量对齐整点
            next_run_seconds = interval_seconds
            current_minute = datetime.now(_SH_TZ).minute
            if interval_seconds == 3600:
                 # 如果是一小时一次，尽量在每小时的 05 分运行，避开整点可能的拥堵
                 next_run_seconds = ((65 - current_minute) % 60) * 60
                 if next_run_seconds < 60:
                     next_run_seconds = 3600

            logger.debug(f"Next snapshot task in {next_run_seconds}s")
            await asyncio.sleep(next_run_seconds)
        except asyncio.CancelledError:
            logger.info("Portfolio snapshot task cancelled")
            break
        except Exception as e:
            logger.error(f"Error in portfolio snapshot task: {e}")
            await asyncio.sleep(60)
