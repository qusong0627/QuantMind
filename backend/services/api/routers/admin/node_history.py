"""
管理员 - 节点性能历史
=====================

采样：API 进程内后台任务，每 60s 复用 dashboard._get_host_workload() 采集一次
      cpu/mem/disk（与左下角「系统真实负载」同一数据源、同一容器视角），
      追加进 Redis 一个 JSON 数组键（滚动保留最近 N 点，TTL 过期）。
查询：GET /api/v1/admin/dashboard/node-history 返回历史序列，供前端画面积图。

多 worker 安全：每次采样先用 Redis NX 锁抢「采样权」，只有拿到锁的实例写点，
其余实例跳过 —— 避免并发工作进程重复采样。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

from fastapi import APIRouter, Depends, Query

from backend.services.api.user_app.middleware.auth import require_admin
from backend.shared.redis_sentinel_client import get_redis_sentinel_client
from backend.services.api.routers.admin.dashboard import _get_host_workload

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(require_admin)])

# ---- 存储配置 -------------------------------------------------------------
_KEY = "quantmind:perf:history"
_LOCK = "quantmind:perf:sampler_lock"
_MAX_POINTS = 720  # 1min 采样 → 滚动保留 12 小时
_TTL = 26 * 3600  # 序列整体 TTL（秒），比窗口略大，双保险清旧点
_INTERVAL = 60  # 采样间隔（秒）
_LOCK_TTL = 70  # 采样锁 TTL，略大于间隔，避免锁残留

_sampler_task: asyncio.Task | None = None

# ---- 阈值告警配置 ---------------------------------------------------------
_THRESHOLDS = {
    "disk": 90,
    "mem": 90,
    "cpu": 90,
}
_ALERT_COOLDOWN = 600  # 同类型告警最短间隔（秒），避免每次采样刷屏
_alert_last: dict[str, float] = {}


def _maybe_node_alert(workload: dict) -> None:
    """CPU/内存/磁盘超阈值时落一条 node_alert 系统事件（带冷却去重）。"""
    now = time.time()
    values = {
        "cpu": workload.get("cpu_percent"),
        "mem": workload.get("memory_percent"),
        "disk": workload.get("disk_percent"),
    }
    for kind, threshold in _THRESHOLDS.items():
        v = values.get(kind)
        if v is None or v < threshold:
            continue
        if now - _alert_last.get(kind, 0) < _ALERT_COOLDOWN:
            continue
        _alert_last[kind] = now
        try:
            from backend.shared.system_events import record_system_event

            record_system_event(
                event_type="node_alert",
                level="warning",
                source="quantmind-api",
                title=f"节点{KIND_LABELS.get(kind, kind)}负载告警",
                message=f"{KIND_LABELS.get(kind, kind)}使用率 {v:.1f}% 超出阈值 {threshold}%",
                meta={"kind": kind, "value": v, "threshold": threshold, **values},
            )
        except Exception:  # noqa: BLE001 - 事件记录非关键路径
            pass


KIND_LABELS = {"disk": "磁盘", "mem": "内存", "cpu": "CPU"}


def _sample_static() -> None:
    """同步采集一个点并写入 Redis 滚动序列（由锁保证唯一写者）。"""
    try:
        client = get_redis_sentinel_client()
    except Exception:  # noqa: BLE001
        logger.warning("node-history redis 不可用，跳过采样", exc_info=True)
        return

    # NX 抢锁：已有人在本周期采样则直接放弃；写完后释放。
    try:
        acquired = client.set(_LOCK, b"1", ex=_LOCK_TTL, nx=True)
    except Exception:  # noqa: BLE001
        logger.warning("node-history 抢锁失败，跳过采样", exc_info=True)
        return
    if not acquired:
        return

    try:
        workload = _get_host_workload()
        _maybe_node_alert(workload)
        raw = client.get(_KEY)
        series = json.loads(raw.decode("utf-8")) if raw else []
        series.append(
            {
                "ts": int(time.time()),
                "cpu": workload["cpu_percent"],
                "mem": workload["memory_percent"],
                "disk": workload["disk_percent"],
            }
        )
        series = series[-_MAX_POINTS:]
        client.setex(_KEY, _TTL, json.dumps(series, ensure_ascii=False).encode("utf-8"))
    except Exception:  # noqa: BLE001
        logger.exception("node-history 采样写入失败")
        return
    finally:
        try:
            client.delete(_LOCK)
        except Exception:  # noqa: BLE001
            pass


async def _sampler_loop() -> None:
    while True:
        try:
            await asyncio.to_thread(_sample_static)
        except Exception:  # noqa: BLE001
            logger.exception("node-history 采样循环异常")
        await asyncio.sleep(_INTERVAL)


def start_node_history_sampler() -> asyncio.Task:
    """在 API lifespan 中启动后台采样；已运行则复用。"""
    global _sampler_task
    if _sampler_task is None or _sampler_task.done():
        _sampler_task = asyncio.create_task(_sampler_loop())
    return _sampler_task


async def stop_node_history_sampler() -> None:
    global _sampler_task
    if _sampler_task:
        _sampler_task.cancel()
        try:
            await _sampler_task
        except asyncio.CancelledError:
            pass
        _sampler_task = None


async def _read_series() -> list[dict]:
    def _read():
        client = get_redis_sentinel_client()
        raw = client.get(_KEY)
        return json.loads(raw.decode("utf-8")) if raw else []

    try:
        return await asyncio.to_thread(_read)
    except Exception:  # noqa: BLE001
        logger.exception("node-history 读取失败")
        return []


@router.get("/node-history")
async def get_node_history(limit: int = Query(default=180, ge=1, le=_MAX_POINTS)):
    """返回节点性能历史序列（新到旧倒序，供图表直接取尾部）。"""
    series = await _read_series()
    return {"success": True, "data": {"series": series[-limit:]}}
