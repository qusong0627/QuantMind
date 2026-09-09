"""A 股交易时段判定（单一口径）。

被 QMT 执行端轮询器（``qmt_exec_poller``）与真单镜像（``real_mirror_service``）
共用，避免两处各写一份时段常量后漂移。口径与 ``tdx_quote_feed`` 一致：
周一至周五 09:15-11:35 / 12:55-15:05（含集合竞价与尾盘缓冲，不含节假日判断）。
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")

# (开始小时, 开始分钟, 结束小时, 结束分钟)
TRADING_SESSIONS: tuple[tuple[int, int, int, int], ...] = (
    (9, 15, 11, 35),
    (12, 55, 15, 5),
)


def now_shanghai() -> datetime:
    """当前上海时间。"""
    return datetime.now(TZ)


def is_trading_time(now: datetime | None = None) -> bool:
    """是否 A 股交易时段（不判断节假日，节假日本身无委托可下）。"""
    now = now or now_shanghai()
    if now.weekday() >= 5:
        return False
    hm = (now.hour, now.minute)
    return any((sh, sm) <= hm < (eh, em) for sh, sm, eh, em in TRADING_SESSIONS)


def trade_date_str(now: datetime | None = None) -> str:
    """当日日期串 ``YYYYMMDD``（日限额/日切计数用）。"""
    return (now or now_shanghai()).strftime("%Y%m%d")
