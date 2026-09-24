"""A 股交易时段判定 + 真单时段闸门（单一口径）。

被 QMT 执行端轮询器（``qmt_exec_poller``）与真单镜像（``real_mirror_service``）
共用，避免两处各写一份时段常量后漂移。口径与 ``tdx_quote_feed`` 一致：
周一至周五 09:15-11:35 / 12:55-15:05（含集合竞价与尾盘缓冲，不含节假日判断）。

**为什么拒因文案也放在这里**（2026-09-24）：闸门此前进驻在
``TdxPushService.place_order``（滚动单/L2），而经 ``TradingEngine`` 的
``broker_client`` 五个真券商通道**一道闸都没有**——同一个 Windows 桥、
同一种失败形态（盘外提交被客户端挂成次日单）。闸门补齐到那五处之后，
若各写各的文案，用户在委托备注里看到的原因就会取决于他从哪条路进来。
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


#: 真单时段闸门的**唯一拒因文案**（人工可见面：委托备注、告警、桥回执）。
#: 措辞里带上具体时段，是因为用户看到它时下一个问题必然是"那我几点能下"。
OUT_OF_SESSION_MESSAGE = "非交易时段（A 股 09:15–11:35 / 12:55–15:05），委托未提交"


def real_order_session_refusal(now: datetime | None = None) -> str | None:
    """真单时段闸门：不在时段内返回拒因文案，在时段内返回 ``None``。

    **为什么盘外必须拒而不是"交给柜台试试"**：A 股委托只可能在交易时段有效，
    盘外提交的委托要么被柜台拒，要么被客户端**挂成次日单**——后者尤其坏：
    一笔"现在"的决定变成了明天的无主委托（价格、仓位、风控前提全部过期）。

    **为什么不判断节假日**：节假日无委托可下，柜台自会拒；这里只管"现在这个
    时刻有没有可能成交"。加一层节假日判断只会多一个需要常年维护的真相源。

    测试注入：``monkeypatch.setattr(trading_session, "is_trading_time",
    lambda now=None: True)``——打在**本模块**的谓词上，闸门与所有时段消费方
    一起被接管。**必须注入**：不注入的用例是"白天绿、收盘后红"，测的是墙上钟
    而不是被测代码（``test_broker_plan_id_uniqueness`` 与 ``test_qmt_exec_broker``
    都因为漏注入而在盘外集体转红过一次）。
    """
    if is_trading_time(now):
        return None
    return OUT_OF_SESSION_MESSAGE
