"""调度跳发台账（P2-5）——「到点但没派发，为什么」的持久面。

P2-3（``dd9a2712``）给市场同步/因子填充派发加了交易日历门：{今日, 昨日} 两个
自然日按市场日历都非交易日时跳过，原因进**派发返回值**的 ``skipped`` 段。而
beat 任务的返回值进 celery result backend，没有常态读者——跳发这件事只活在日志
里。本模块是 P2-5 值班摘要的取数面：把每次派发 tick 的 ``skipped`` 落成按日
分片的 Redis 哈希，摘要据此回答「今天哪些市场的同步被跳过、为什么」。

键空间（db0，与其他调度键 ``qm:sched:hb`` 同域）：

``qm:sched:skips:{ISO 日期}``  hash，字段 ``{job}:{market}`` → 跳过原因；
TTL 7 天（够当周值班回看）。job ∈ ``market_sync`` / ``factor_fill``。

语义纪律（为什么不是计数器）：

- **幂等最新态**：日历门在一整天里每个 tick 都给出同一个 skip（到点判据是
  ``now >= 配置时刻``），逐次计数只会堆出「1440 次」这种没有信息量的数字；
  字段值 = 原因文本，重复记录只覆盖不累加。
- **派发即清除**：周日凌晨 00:30 时 {昨天(六), 今天(日)} 均非交易日 → skip；
  周一凌晨同一市场放行派发时，必须把 CN 的旧 skip 清掉，否则摘要会把「已跑」
  读成「还在跳」——那是假故障。
- **读失败抛出**：``read_skips`` 遇到 Redis 故障绝不返回空 dict——空 dict 在
  摘要里渲染成「无跳发」，而真相是「读不到」。未知与正常必须分开（与全仓
  「缺失一律如实」同纪律）。

写入点（唯一）：``engine.tasks.dispatch_market_sync`` 任务体，对
``dispatch_due_syncs()`` / ``dispatch_due_factor_fills()`` 的返回值逐段调用
``record_skips`` / ``clear_skips``，best-effort（台账故障不许拖垮派发本身）。
读取点：值班摘要（trade 常驻任务）；死手检查不读本台账（跳发是正常状态，
不是「该响没响」）。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date
from typing import Any

SKIP_KEY_PREFIX = "qm:sched:skips"

#: 7 天：覆盖一个完整自然周（周末跳发在周一回看时仍在）。
SKIP_TTL_SECONDS = 7 * 24 * 3600


def skip_key(day: date) -> str:
    """按日分片的台账键（ISO 带横线，与调度域既有日键同格式）。"""
    return f"{SKIP_KEY_PREFIX}:{day.isoformat()}"


def record_skips(
    client: Any,
    *,
    day: date,
    job: str,
    skipped: Mapping[str, str],
) -> int:
    """记录一段派发的跳发字段（``{job}:{market}`` → 原因）。返回写入字段数。

    幂等：同一字段重复写入只覆盖（最新原因）。Redis 故障向上抛——调用方
    （celery 任务体）自行兜底，模块层不吞异常（吞掉的写失败没有第二个观测面）。
    """
    if not skipped:
        return 0
    key = skip_key(day)
    payload = {f"{job}:{market}": str(reason) for market, reason in skipped.items()}
    client.hset(key, mapping=payload)
    client.expire(key, SKIP_TTL_SECONDS)
    return len(payload)


def clear_skips(
    client: Any,
    *,
    day: date,
    job: str,
    dispatched: Iterable[str],
) -> int:
    """清除已派发市场的残留 skip 字段（派发成功 = 该市场今天不再处于「跳过」态）。"""
    markets = [str(m) for m in dispatched if str(m)]
    if not markets:
        return 0
    return int(client.hdel(skip_key(day), *[f"{job}:{market}" for market in markets]))


def read_skips(client: Any, day: date) -> dict[str, str]:
    """读某日台账：``{"market_sync:CN": 原因, ...}``。读失败抛出（见模块头纪律）。"""
    raw = client.hgetall(skip_key(day))
    return {str(field): str(value) for field, value in (raw or {}).items()}
