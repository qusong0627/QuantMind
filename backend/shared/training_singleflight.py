"""训练单飞锁（P0-3）：训练串行约束的代码化。

背景（docs/滚动训练与模型生命周期_设计方案.md §4.3，实测 memory
`training-memory-budget`）：训练真内存上限是宿主 MemAvailable（~44G，全局
OOM 挑受害者不分青红皂白），训练必须串行。但提交入口有多个（UI / 外部门面
`/task kind=training` / 批跑器 / 未来的重训调度），此前无任何互斥 → 并发
提交 = 训练容器互杀。

设计：
- 键 ``qm:training:singleflight``，SET NX EX，**值 = run_id**（属主即任务）；
- TTL = ``max_time_minutes × 60 + 600``（预算 + 回调等待宽限），下限 1800s；
  合法运行不可能超过预算（超时即被编排器 kill），TTL 过期即视为持有者死亡；
- 释放走 Lua CAS（``redis_lock.release``，token=run_id）——三个释放层：
  编排 task 结束回调（orchestrator_base.attach_singleflight_release）、
  判尸回收（job_reaper）、ttl 自然过期；
- 自愈：提交时占用者已终态/行丢失（``holder_is_stale``）→ 释放后重试一次，
  杜绝「上次异常退出锁没放」把训练卡死；
- **续租**（设计 §4.3「持有者按 run_id 续租」，2026-10-08 复审 HIGH-4 补）：
  提交取锁成功后启动 ``start_renewal`` 心跳，每 ``RENEW_INTERVAL_SECONDS``
  走 Lua CAS EXPIRE（仅当持有者仍是 run_id）。API 重启后由 job_reaper 的
  重挂路径（``reattach_training_job``）重启心跳。心跳自终止：连续 2 次
  续租被拒（锁已释放/易主）即退出；进程退出时随 loop 关闭被取消。
  无续租时，重挂会把预算时钟重置（全量 max_time 重新计时），运行可能
  超出提交时算出的 TTL → 锁提前过期 → 下一次提交与存活训练双跑。

**顺序纪律（复审 HIGH-2，2026-10-08）**：提交方必须**先落 holder 行并提交、
再取锁**。反序存在「锁已发布、holder 行未提交」窗口：并发提交在窗口内
``session.get`` 读不到持有者行 → 误判「行丢失」→ 自愈抢占 → 双跑。
即：锁在手 ⇒ holder 行必然已提交可见。
"""

from __future__ import annotations

import asyncio
import functools
import logging

from backend.shared.redis_lock import acquire as _acquire
from backend.shared.redis_lock import holder as _holder
from backend.shared.redis_lock import release as _release

logger = logging.getLogger(__name__)

LOCK_KEY = "qm:training:singleflight"

DEFAULT_MAX_TIME_MINUTES = 120  # 与编排器默认一致
CALLBACK_GRACE_SECONDS = 600  # 与 _CALLBACK_TIMEOUT 同源
MIN_TTL_SECONDS = 1800
RENEW_INTERVAL_SECONDS = 300  # 续租节拍（TTL 的 1/6 级别；连续 2 次失手退出）

# 活动状态 = admin_training_jobs 的非终态（与 _CANCELABLE_STATUSES 同口径）
ACTIVE_STATUSES: tuple[str, ...] = (
    "pending",
    "provisioning",
    "running",
    "waiting_callback",
)


def lock_ttl_seconds(max_time_minutes: int | None) -> int:
    """TTL = 预算 + 回调宽限；缺省预算 120 分钟；下限 1800s。"""
    try:
        budget = int(max_time_minutes or DEFAULT_MAX_TIME_MINUTES)
    except (TypeError, ValueError):
        budget = DEFAULT_MAX_TIME_MINUTES
    budget = max(1, budget)
    return max(MIN_TTL_SECONDS, budget * 60 + CALLBACK_GRACE_SECONDS)


def try_acquire(redis_client, run_id: str, ttl_seconds: int | None = None) -> bool:
    """尝试取锁（值=run_id）；被占用返回 False。Redis 异常向上抛。"""
    ttl = ttl_seconds if ttl_seconds is not None else lock_ttl_seconds(None)
    return _acquire(redis_client, LOCK_KEY, str(run_id), int(ttl))


def release(redis_client, run_id: str) -> bool:
    """CAS 释放（仅当持有者仍是 run_id 才生效）。"""
    return _release(redis_client, LOCK_KEY, str(run_id))


def get_holder(redis_client) -> str | None:
    """当前持锁 run_id；无锁返回 None。"""
    return _holder(redis_client, LOCK_KEY)


def holder_is_stale(status: str | None) -> bool:
    """持有者是否已陈旧（纯函数）：终态 / 空 / 行丢失（None）→ 可自愈释放。"""
    return str(status or "") not in ACTIVE_STATUSES


# ── 续租心跳（设计 §4.3：持有者按 run_id 续租）─────────────────────────────

# CAS 续租：仅当持有者仍是本 run 才 EXPIRE（避免把已易主的锁续成自己的）
RENEW_LUA = """
local current = redis.call("GET", KEYS[1])
if current == ARGV[1] then
    return redis.call("EXPIRE", KEYS[1], ARGV[2])
end
return 0
"""


def renew(redis_client, run_id: str, ttl_seconds: int) -> bool:
    """CAS 续租；成功 True，非属主/键不存在 False。异常向上抛（客户端 eval 不吞）。"""
    return bool(
        redis_client.eval(RENEW_LUA, 1, LOCK_KEY, str(run_id), int(ttl_seconds))
    )


_RENEWERS: dict[str, asyncio.Task] = {}


def _retire_renewer(key: str, task: asyncio.Task) -> None:
    """done 回调：仅当字典里登记的仍是本任务才移除（复审 NEW-6）。

    竞态：旧任务已 done 但其 done 回调尚未运行（call_soon 排队）时，
    ``start_renewal`` 会看到 ``existing.done()`` → 覆盖 ``_RENEWERS[key]``
    建新任务。旧任务的迟到回调若无身份判断，会把**新任务**摘出字典 →
    丢失停止/幂等跟踪（``stop_renewal`` 找不到、再进 ``start_renewal``
    重复建心跳）。
    """
    if _RENEWERS.get(key) is task:
        _RENEWERS.pop(key, None)


async def _renew_loop(run_id: str, ttl_seconds: int) -> None:
    """续租循环：连续 2 次续租被拒 → 锁已释放/易主，自行退出（防任务泄漏）。"""
    misses = 0
    try:
        while True:
            await asyncio.sleep(RENEW_INTERVAL_SECONDS)
            try:
                from backend.shared.redis_sentinel_client import (
                    get_redis_sentinel_client,
                )

                ok = renew(get_redis_sentinel_client(), run_id, ttl_seconds)
            except Exception as exc:  # noqa: BLE001
                # eval 异常（连接失败等）不计为失手：Redis 恢复后仍应续上；
                # 若 Redis 持续不可用，TTL 兜底（锁在服务端仍按原 TTL 到期）
                logger.warning(
                    "[Singleflight] 续租 %s 异常（下轮重试）: %s", run_id, exc
                )
                continue
            if ok:
                misses = 0
                continue
            misses += 1
            if misses >= 2:
                logger.info(
                    "[Singleflight] 续租 %s 连续被拒（锁已释放/易主），心跳退出", run_id
                )
                return
    except asyncio.CancelledError:
        # 进程/loop 关闭：静默退出（任务随进程消亡，无需告警）
        return


def start_renewal(run_id: str, ttl_seconds: int) -> None:
    """启动持有者续租心跳（同 run 幂等）。调用点：提交取锁成功后、孤儿重挂后。"""
    key = str(run_id)
    existing = _RENEWERS.get(key)
    if existing is not None and not existing.done():
        return
    try:
        task = asyncio.get_running_loop().create_task(
            _renew_loop(key, int(ttl_seconds))
        )
    except RuntimeError:
        # 无运行 loop（同步上下文）→ 不续租，TTL 兜底
        logger.warning("[Singleflight] 无运行 loop，%s 跳过续租心跳", key)
        return
    _RENEWERS[key] = task
    task.add_done_callback(functools.partial(_retire_renewer, key))


def stop_renewal(run_id: str) -> None:
    """显式停止续租心跳（测试/收尾用；生产路径靠自终止）。"""
    task = _RENEWERS.pop(str(run_id), None)
    if task is not None and not task.done():
        task.cancel()
