"""QMT/TDX 健康告警节流：盘中每次提醒，盘前/盘后每段最多 2 次。

用户口径（2026-11-14）：桥/QMT「没有在线」在盘前或盘后持续复提醒太吵；
盘中允许每次提醒（掉线影响下单），盘外每段只提醒 2 次。恢复事件不受节流
（掉线→恢复的闭环必须送到手机）。

实现：Redis 计数器（db0）
``qm:alert:health:{channel}:{YYYYMMDD}:{pre|post}``，INCR + 2 天过期；
盘中（``trading_session.is_trading_time``：09:15–11:35 / 12:55–15:05）直接放行。
Redis 不可用时 **fail-open**（宁可多提醒一次，不因节流器故障漏掉掉线）。
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any

from backend.services.live_trading.services.trading_session import (
    is_trading_time,
    now_shanghai,
)

logger = logging.getLogger(__name__)

#: 盘前/盘后每段最多提醒次数（用户 2026-11-14 拍板）。
OUTSIDE_SESSION_LIMIT = 2
#: 计数器保留时长：覆盖同一交易日的盘前与盘后。
KEY_TTL_S = 2 * 86400


def _period(now: datetime) -> str:
    """盘前/盘后分段：09:15 之前 pre，15:05 之后 post；盘中不走本函数。"""
    hm = (now.hour, now.minute)
    return "pre" if hm < (9, 15) else "post"


def _is_trading_day(now: datetime) -> bool:
    """交易日判据；日历答不了（None/异常）按交易日放行（fail-open）。"""
    try:
        from backend.shared.trading_calendar import is_trading_day_xcal

        verdict = is_trading_day_xcal("CN", now.date())
    except Exception:  # noqa: BLE001 - 判据异常不拦告警
        return True
    return verdict is not False


def throttle_key(channel: str, now: datetime | None = None) -> str:
    moment = now or now_shanghai()
    period = "off" if not _is_trading_day(moment) else _period(moment)
    return f"qm:alert:health:{channel}:{moment:%Y%m%d}:{period}"


def _redis_client() -> Any:
    import redis as _redis

    return _redis.Redis(
        host=os.getenv("REDIS_HOST") or "redis",
        port=int(os.getenv("REDIS_PORT", "6379")),
        password=os.getenv("REDIS_PASSWORD") or None,
        db=int(os.getenv("REDIS_DB_GENERAL", "0")),
        decode_responses=True,
        socket_connect_timeout=0.3,
        socket_timeout=0.3,
    )


def allow_health_alert(
    channel: str,
    now: datetime | None = None,
    *,
    client: Any | None = None,
) -> bool:
    """是否允许本次盘外健康提醒。

    - 交易时段（含报单缓冲）→ 永远 True；
    - 盘外 → 同一 channel/日期/段（pre/post）最多 ``OUTSIDE_SESSION_LIMIT`` 次；
    - Redis 异常 → True（fail-open，不因节流器故障漏报掉线）。
    """
    moment = now or now_shanghai()
    if _is_trading_day(moment) and is_trading_time(moment):
        return True
    key = throttle_key(channel, moment)
    own = client is None
    cli = client or _redis_client()
    try:
        count = int(cli.incr(key))
        if count == 1:
            cli.expire(key, KEY_TTL_S)
        return count <= OUTSIDE_SESSION_LIMIT
    except Exception as exc:  # noqa: BLE001 - 节流器故障不得拦掉线告警
        logger.warning("[HealthThrottle] %s 计数失败（fail-open）: %s", key, exc)
        return True
    finally:
        if own:
            try:
                cli.close()
            except Exception:  # noqa: BLE001
                pass
