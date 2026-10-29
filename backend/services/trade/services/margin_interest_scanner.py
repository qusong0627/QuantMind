"""
融资融券每日计息扫描器 (Simulation Ledger)

每日执行一次，扫描所有的仿真/模拟账户，扣除融资和融券利息。
- 融资利息基数：可用现金为负的部分
- 融券利息基数：空头头寸总市值 (short_market_value)
- 费率：年化 6%，按 365 自然日计算
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

from backend.services.trade_shared.redis_client import redis_client
from backend.services.trade_shared.trade_config import settings
from backend.shared.simulation_margin_math import maintenance_margin_ratio
from backend.shared.trade_account_cache import read_json_cache, write_json_cache

logger = logging.getLogger(__name__)

_INTERVAL_SEC = 3600  # 默认每小时检查一次，但内部根据上次结算时间控制每日一次


async def _scan_and_settle() -> int:
    """扫描所有仿真账户，应用日计息。"""
    try:
        if not redis_client.client:
            logger.warning("Redis client not available for margin interest scanner.")
            return 0

        # 获取所有的模拟账户 key
        # 考虑到量级，如果是超大规模可以使用 scan_iter，此处简化使用 keys
        keys = redis_client.client.keys("quantmind:trade:sim:account:*")
        if not keys:
            return 0

        settled_count = 0
        now = datetime.now(timezone.utc)
        today_str = now.strftime("%Y-%m-%d")

        for key_bytes in keys:
            key = key_bytes.decode("utf-8") if isinstance(key_bytes, bytes) else key_bytes

            account = read_json_cache(redis_client, key)
            if not account:
                continue

            try:
                last_interest_date_str = account.get("last_interest_date")

                # 如果没有上次计息日期，初始化为今天并跳过计息（从明天开始算）
                if not last_interest_date_str:
                    account["last_interest_date"] = today_str
                    write_json_cache(redis_client, key, account)
                    continue

                if last_interest_date_str == today_str:
                    # 今天已经计过息了
                    continue

                last_date = datetime.strptime(last_interest_date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                days_diff = (now - last_date).days

                if days_diff <= 0:
                    continue

                cash = float(account.get("cash") or 0.0)
                short_market_value = float(account.get("short_market_value") or 0.0)

                cash_debt = max(0.0, -cash)
                total_debt = short_market_value + cash_debt

                if total_debt > 0:
                    daily_rate = 0.06 / 365.0
                    interest_charge = total_debt * daily_rate * days_diff

                    # 扣除利息
                    new_cash = cash - interest_charge
                    account["cash"] = new_cash

                    # 重新计算 equity 等
                    total_market_value = float(account.get("market_value") or 0.0)
                    short_proceeds = float(account.get("short_proceeds") or 0.0)
                    equity = new_cash + short_proceeds + total_market_value
                    account["total_asset"] = equity

                    liabilities = float(account.get("liabilities") or 0.0)
                    if liabilities > 0:
                        account["maintenance_margin_ratio"] = maintenance_margin_ratio(
                            equity,
                            float(account.get("short_market_value") or 0.0),
                            liabilities,
                        )

                    if interest_charge > 1:
                        logger.info(
                            f"Account {key} charged {interest_charge:.2f} margin interest for {days_diff} days."
                        )

                account["last_interest_date"] = today_str
                write_json_cache(redis_client, key, account)
                settled_count += 1

            except Exception as e:
                logger.error(f"Failed to process margin interest for {key}: {e}")

        return settled_count

    except Exception as exc:
        logger.error(f"Margin interest scan failed: {exc}", exc_info=True)
        return 0


async def run_margin_interest_scanner() -> None:
    """后台无限循环，定期执行融资融券日结计息。"""
    logger.info("Margin interest scanner started.")
    while True:
        try:
            count = await _scan_and_settle()
            if count > 0:
                logger.info(f"Margin interest scanner completed for {count} accounts.")
        except Exception as exc:
            logger.error("Margin interest scanner error: %s", exc)
        await asyncio.sleep(_INTERVAL_SEC)
