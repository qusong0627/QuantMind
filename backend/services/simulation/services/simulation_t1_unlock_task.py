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
from zoneinfo import ZoneInfo

from backend.services.trade_shared.redis_client import redis_client
from backend.services.trade_shared.simulation_manager import SimulationAccountManager

logger = logging.getLogger(__name__)
_SH_TZ = ZoneInfo("Asia/Shanghai")

_UNLOCK_HOUR = 9
_UNLOCK_MINUTE = 16
_CHECK_INTERVAL_SECONDS = 60
_ACCOUNT_KEY_PATTERN = "simulation:account:*"


def _is_cn_trade_date(day) -> bool:
    try:
        import pandas as pd
        from exchange_calendars import get_calendar

        return bool(get_calendar("XSHG").is_session(pd.Timestamp(day)))
    except Exception:
        return day.weekday() < 5


async def _unlock_all_accounts(
    manager: SimulationAccountManager, *, as_of_date=None
) -> int:
    """按 PG 持仓批次同步全部模拟账户可卖量。

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
            if market == "CN":
                result = await manager.sync_t1_from_ledger(
                    user_id=int(user_raw),
                    tenant_id=tenant,
                    market=market,
                    as_of_date=as_of_date,
                )
            else:
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
    """每个交易日 09:16 后按持仓批次同步 T+1 可卖量（幂等）。

    进程在解锁窗口后重启会补跑，但当日买入批次不会被解锁。
    """
    manager = SimulationAccountManager(redis_client)
    last_date = ""
    while True:
        from backend.shared.scheduler_registry import heartbeat as _sched_heartbeat

        _sched_heartbeat("t1_unlock")
        try:
            now = datetime.now(_SH_TZ)
            today = now.strftime("%Y%m%d")
            if (
                today != last_date
                and (now.hour, now.minute) >= (_UNLOCK_HOUR, _UNLOCK_MINUTE)
            ):
                last_date = today
                if not _is_cn_trade_date(now.date()):
                    logger.info("模拟盘 T+1 跳过非交易日: %s", today)
                    continue
                unlocked = await _unlock_all_accounts(
                    manager, as_of_date=now.date()
                )
                logger.info(
                    "模拟盘 T+1 同步完成: date=%s unlocked_accounts=%d",
                    today,
                    unlocked,
                )
        except Exception as exc:
            logger.warning("模拟盘 T+1 解锁任务异常: %s", exc)
        await asyncio.sleep(interval_seconds)
