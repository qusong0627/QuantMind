"""告警投递引擎：**去重 → 投递 → 送达之后才记键**。

一条纪律，多处消费（决策轮、TDX 滚动买卖腿、L2 实时腿）。抽出来是因为这套纪律
的每一句都是踩出来的，各写一遍必然漂移：

* **送达之后才记去重键**。先记后推，等于「推失败的那条告警永远不会再试」——
  键在 Redis 里、人在屏幕前，两边都以为对方知道。
* **读不到键按「没推过」办**。去重是为了少打扰，不是为了保证沉默；
  读失败时重复推一条，代价远小于漏掉一条。
* **绝不抛**。调用点都在「刚跑完一件事」之后（一轮决策、一次下单尝试、一个
  循环周期），此刻可能已经有真单出去了；通知层的毛病不许把那份结果变成异常。
* **没有要推的事就不碰通知设施**。`notifier_factory` 是**惰性**的：正常路径
  （无事可推）连通知模块都不导入、不构造——一个每天跑几千次的循环不该在
  通知链路上留任何足迹。
* **账户坐标为空不推**，但留 warning：推给空账户的通知落在谁那里都不对，
  而「账户坐标是空的」本身就是一条要查的事。

去重键的形状**由调用方定**（``(交易日, 家, 类别)`` 是决策轮的口径，
``(交易日, 执行腿, 类别)`` 是 TDX 执行腿的口径），本模块只管「有键就查、
送达才写」。

去重设施的形状：``get(key)`` + ``set(key, value, ...)``。本项目有两代客户端
（原生 redis-py 与 ``trade_shared.redis_client.RedisClient`` 包装层），两者的
``set`` 关键字名不同（``ex=`` / ``ttl=``），故写入按签名回退（见
``remember_sent``）——只认 ``ex=`` 就等于在包装层上去重静默失效。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, MutableSet
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

#: 去重键存活期（秒）：跨过一整个交易日即可——口径是「一天一次」，不是「一段时间一次」。
ALERT_TTL_S = 90_000


@dataclass(frozen=True, slots=True)
class Alert:
    """一条待推的告警（``level`` 只取 ``notification_publisher`` 认识的档位）。"""

    kind: str
    level: str
    title: str
    content: str


#: 通知器形状：``(user_id, title, content, level) -> Awaitable``。**账户坐标不在参数
#: 里**（与提交器同一条纪律：它属于「这件事发生在谁身上」，构造时闭合进去）。
Notifier = Callable[..., Awaitable[Any]]

#: 通知器工厂：**只在真要推的时候**才调用（见模块 docstring 第四条）。
NotifierFactory = Callable[[], Notifier]


def alert_sent(redis: Any, key: str) -> bool:
    """读过键没有。**读不到按「没推过」办**（重复推优于沉默）。

    ``redis=None`` ⇒ 没有去重设施，一切照推（调用方通常只在测试里这么用）。
    """
    if redis is None:
        return False
    try:
        return bool(redis.get(key))
    except Exception as exc:  # noqa: BLE001
        logger.warning("[Alert] 去重键读取失败 %s: %s", key, exc)
        return False


def remember_sent(redis: Any, key: str) -> None:
    """记下「已推」。写失败只告警：下次会重复推一条，这不是要拦下的错。

    **两种客户端形状都要能写**：原生 redis-py 是 ``set(k, v, ex=)``，而本项目
    包装层 ``trade_shared.redis_client.RedisClient`` 是 ``set(k, v, ttl=)`` 且把
    异常吞成一行日志。只认 ``ex=`` 的写法在包装层上会 TypeError 被吞掉——
    键永远写不下去、去重**静默失效**（每周期/每次运行都重推一遍），
    这正是 P2.6 记下的同一个坑。按签名回退，不按类型嗅探。
    """
    if redis is None:
        return
    try:
        redis.set(key, "1", ex=ALERT_TTL_S)
    except TypeError:
        try:
            redis.set(key, "1", ttl=ALERT_TTL_S)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Alert] 去重键写入失败（ttl= 形状） %s: %s", key, exc)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[Alert] 去重键写入失败 %s: %s", key, exc)


async def deliver_alert(
    alert: Alert,
    *,
    key: str,
    user_id: object,
    redis: Any = None,
    notifier_factory: NotifierFactory | None = None,
    seen: MutableSet[str] | None = None,
    log_prefix: str = "[Alert]",
) -> bool:
    """该推就推，推成功才记去重键。返回「这一次真的推出去了吗」。**绝不抛**。

    ``notifier_factory=None`` ⇒ 不推（调用方没接通知设施时的显式选择，
    留 warning 而不是静默返回 False——「推不出去」和「没什么可推」是两件事）。

    ``seen`` 是**进程内**去重集（键形与 ``key`` 同）：给循环用。Redis 读不出来
    （宕机/重建）时 ``alert_sent`` 按「没推过」办，一个 60s 的循环会退化成每周期
    推一条——那不是「重复推优于沉默」，是把人训练成不看通知。两个账一起记：
    Redis 记跨进程/跨重启的，``seen`` 记本进程的；两边不一致的后果是多推或少推
    **一条**，不是漏掉故障本身。事件驱动（一天几次）的调用方不必传。
    """
    uid = str(user_id or "").strip()
    if not uid:
        logger.warning(
            "%s %s 有告警要推（%s）但账户坐标为空，通知未推: %s",
            log_prefix,
            key,
            alert.kind,
            alert.title,
        )
        return False

    if seen is not None and key in seen:
        return False
    if alert_sent(redis, key):
        if seen is not None:
            # Redis 里已有 ⇒ 本进程也记上：此后 Redis 若短暂读不到，也不至于重复推。
            seen.add(key)
        return False

    if notifier_factory is None:
        logger.warning("%s %s 有事要推但没有通知器（%s）", log_prefix, key, alert.kind)
        return False
    send = notifier_factory()

    try:
        delivered = bool(await send(uid, alert.title, alert.content, alert.level))
    except Exception as exc:  # noqa: BLE001 通知炸了不许带走调用点的结果
        logger.warning(
            "%s 通知发送失败（%s）: %s", log_prefix, alert.kind, exc, exc_info=True
        )
        return False

    if delivered:
        if redis is not None:
            remember_sent(redis, key)
        if seen is not None:
            seen.add(key)
    if not delivered:
        logger.warning(
            "%s 通知未送达（%s）：下次同类失败会再试", log_prefix, alert.kind
        )
    return delivered
