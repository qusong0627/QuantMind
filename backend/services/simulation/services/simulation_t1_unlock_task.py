"""模拟盘 T+1 可卖量解锁任务（每交易日开盘后运行一次，独立于模拟调度器）。

模拟账户的 available_volume 由买入/卖出 LUA 维护：当日买入只增加 volume，
次日开盘前由 unlock_t1 把可卖量补齐（A 股 T+1）。

原设计由 ENABLE_SIMULATION_SCHEDULER 的调度器在开盘前调用 unlock_t1；
本部署未开启该调度器，导致模拟盘持仓可卖量永远为 0、卖出被永久锁定。
本任务与调度器解耦，每交易日 09:16 后把全部模拟账户解锁一次（幂等）。
"""
import asyncio
import logging
from datetime import datetime

from backend.services.trade_shared.redis_client import redis_client
from backend.services.trade_shared.simulation_manager import SimulationAccountManager

logger = logging.getLogger(__name__)

_UNLOCK_HOUR = 9
_UNLOCK_MINUTE = 16
_CHECK_INTERVAL_SECONDS = 60
_ACCOUNT_KEY_PATTERN = "simulation:account:*"


async def _unlock_all_accounts(manager: SimulationAccountManager) -> int:
    """把全部模拟账户的 T+1 可卖量补齐为总量，返回有新解锁持仓的账户数。

    按统一键规范解析（含 :MARKET 后缀的市场账户），市场透传给 unlock_t1，
    避免扫到 HK 账户却解了 CN 账户、HK 账户永远锁死。
    """
    if not redis_client.client:
        return 0
    try:
        keys = list(
            redis_client.client.scan_iter(match=_ACCOUNT_KEY_PATTERN, count=500)
        )
    except Exception as exc:
        logger.error("模拟盘 T+1 扫描账户失败: %s", exc)
        return 0

    unlocked_count = 0
    for key in keys:
        try:
            parsed = SimulationAccountManager.parse_account_key(str(key))
            if not parsed:
                continue
            tenant, user_raw, market = parsed
            if not user_raw.isdigit():
                continue
            result = await manager.unlock_t1(
                user_id=int(user_raw), tenant_id=tenant, market=market
            )
            if result.get("success") and result.get("unlocked", 0) > 0:
                unlocked_count += 1
        except Exception as exc:
            logger.debug("模拟盘 T+1 解锁失败 key=%s: %s", key, exc)
    return unlocked_count


async def run_simulation_t1_unlock_task(
    interval_seconds: int = _CHECK_INTERVAL_SECONDS,
) -> None:
    """每个交易日 09:16 后把全部模拟账户 T+1 可卖量补齐（幂等）。

    进程在解锁窗口后重启会补跑一次；周末/节假日解锁无害（幂等）。
    """
    manager = SimulationAccountManager(redis_client)
    last_date = ""
    while True:
        try:
            now = datetime.now()
            today = now.strftime("%Y%m%d")
            if (
                today != last_date
                and (now.hour, now.minute) >= (_UNLOCK_HOUR, _UNLOCK_MINUTE)
            ):
                last_date = today
                if now.weekday() >= 5:
                    continue  # 周末不解锁，周一自然补齐
                unlocked = await _unlock_all_accounts(manager)
                if unlocked:
                    logger.info("模拟盘 T+1 解锁: %d 个账户有新解锁持仓", unlocked)
        except Exception as exc:
            logger.warning("模拟盘 T+1 解锁任务异常: %s", exc)
        await asyncio.sleep(interval_seconds)
