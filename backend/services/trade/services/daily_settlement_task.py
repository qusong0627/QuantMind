"""每日日结常驻任务（trade）。

每交易日 15:20（北京时间）后跑一次：把当天的智能体台账（成交/平仓）+ 执行事实
（orders/trades）+ 真实资金锚（real_account_snapshots）汇总成日结单。

纪律：

- **幂等**：``daily_settlement.run_day`` 按 (tenant,user,day) upsert，重跑只更新；
  done 键只是省一次重算，不是正确性的前提。
- **读失败不落 done**：数据库不可用时下个周期重试，绝不把故障渲染成「今天没账」。
- **非交易日秒过**：与收盘报表同口径（日历不可用按工作日近似）。
- 任务由 ``trade/main.py`` lifespan 起停；开关 ``QM_DAILY_SETTLEMENT_ENABLED``
  在 main（建不建）与本循环（跑不跑）各判一次。心跳键 ``daily_settlement``。
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import date, datetime, time

from backend.services.live_trading.services.trading_session import TZ
from backend.services.trade.services.daily_pnl_report_task import (
    _env_bool,
    _is_trading_day,
    _redis_client,
)

logger = logging.getLogger(__name__)

#: 日结时点（北京时间）：收盘 15:00 后留 20 分钟给成交回报/持仓快照落库。
_SETTLE_AT = time(15, 20)

_POLL_INTERVAL_S = 60.0
_DONE_TTL_S = 7 * 24 * 3600


def _done_key(day: str) -> str:
    return f"qm:daily_settlement:done:{day}"


def _users_with_activity_sql() -> str:
    return """
        SELECT DISTINCT user_id FROM orders
        WHERE trading_mode = 'REAL' AND created_at >= :start AND created_at < :end
        UNION
        SELECT DISTINCT user_id FROM real_account_snapshots
        WHERE snapshot_date = :day
    """


async def run_settlement_cycle(now: datetime) -> int:
    """单周期：到点 → 交易日门 → 逐用户日结。返回成功用户数。"""
    if (now.hour, now.minute) < (_SETTLE_AT.hour, _SETTLE_AT.minute):
        return 0
    day = now.date()
    if not await _is_trading_day(day):
        return 0

    from backend.services.trade.services import daily_settlement as settlement
    from backend.services.trade_shared.deps import get_redis
    from backend.shared.database_manager_v2 import get_session

    client = _redis_client(get_redis())
    day_str = day.strftime("%Y%m%d")
    if client is not None:
        try:
            if client.exists(_done_key(day_str)):
                return 0
        except Exception:  # noqa: BLE001 - 读不到 done 就重算（幂等，无害）
            logger.warning("[DailySettlement] done 键读取失败，本周期重算")

    anchor_source = await settlement.resolve_anchor_source(client)
    start = datetime.combine(day, time.min)
    from datetime import timedelta

    end = start + timedelta(days=1)
    async with get_session(read_only=False) as session:
        await settlement.ensure_table_async(session)
        from sqlalchemy import text

        rows = (
            await session.execute(
                text(_users_with_activity_sql()),
                {"start": start, "end": end, "day": day},
            )
        ).fetchall()
        users = [str(r[0]) for r in rows if r and r[0]]

        done = 0
        for user_id in users:
            try:
                await settlement.run_day(
                    session,
                    tenant_id="default",
                    user_id=user_id,
                    day=day,
                    anchor_source=anchor_source,
                )
                done += 1
            except Exception as exc:  # noqa: BLE001 - 单用户失败隔离
                logger.error(
                    "[DailySettlement] user=%s %s 日结失败: %s",
                    user_id,
                    day,
                    exc,
                    exc_info=True,
                )
        if done and client is not None:
            try:
                client.set(_done_key(day_str), "1", ex=_DONE_TTL_S)
            except Exception:  # noqa: BLE001
                logger.warning("[DailySettlement] done 键写入失败（下周期重算）")
        return done


async def run_daily_settlement_task() -> None:
    """常驻循环：每交易日 15:20 后跑一次日结。"""
    if not _env_bool("QM_DAILY_SETTLEMENT_ENABLED", True):
        logger.info("[DailySettlement] 日结任务关闭（QM_DAILY_SETTLEMENT_ENABLED=0）")
        return
    try:
        interval = max(
            10.0, float(os.getenv("QM_DAILY_SETTLEMENT_INTERVAL_S") or _POLL_INTERVAL_S)
        )
    except (TypeError, ValueError):
        interval = _POLL_INTERVAL_S

    from backend.shared.scheduler_registry import heartbeat as _sched_heartbeat

    logger.info(
        "[DailySettlement] 日结任务启动：每交易日 15:20 后结算（%.0fs 轮询）", interval
    )
    last_error = ""
    while True:
        try:
            _sched_heartbeat("daily_settlement")
        except Exception:  # noqa: BLE001 心跳是旁路
            pass
        try:
            await run_settlement_cycle(datetime.now(TZ))
            last_error = ""
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            message = f"{type(exc).__name__}: {exc}"
            if message != last_error:
                logger.error("[DailySettlement] 周期失败: %s", message, exc_info=True)
                last_error = message
        await asyncio.sleep(interval)
