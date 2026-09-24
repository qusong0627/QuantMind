"""每日自动强制更新调度。

管理后台「系统设置」里的一个开关：开启后每天 00:00 自动执行一次宿主
``deploy/update.sh --force``（经分离的 updater 容器，见 system_update.py）。

配置与状态存 Redis db 0：
  quantmind:auto_update:config      {enabled}
  quantmind:auto_update:fired:DATE  当日已决策（NX 抢锁，防多 worker 重复触发）
  quantmind:auto_update:last        最近一次决策结果，供前端回显

三道闸门，任一不过就只记事件、不更新：
  1. 到点且在放行窗口内、当日未决策；
  2. 版本闸门：release-index 明确「有落后」才放行。索引不可达或 status=diverged
     时无法判断，一律跳过 —— 宁可不更，不可在未知状态下 reset --hard；
  3. 脏工作区：由 updater 容器内的 guard_dirty 检查兜底（API 容器没有宿主 .git）。

调度循环跑在 API 进程内而不是 Celery：docker.sock 只挂在 quantmind 主容器
（docker-compose.yml），celery-worker / celery-beat 拿不到 socket。
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_CONFIG_KEY = "quantmind:auto_update:config"
_FIRED_KEY = "quantmind:auto_update:fired:{date}"
_LAST_KEY = "quantmind:auto_update:last"
_DOCKER_SOCKET = "/var/run/docker.sock"

# 每天 00:00 触发；窗口用于容忍正好卡在午夜时的服务重启，锁保证当日只决策一次。
RUN_AT = "00:00"
RUN_WINDOW_MINUTES = 5
_POLL_INTERVAL = 20

DEFAULT_CONFIG = {"enabled": False}

_loop_task: asyncio.Task | None = None


def _redis():
    from backend.shared.redis_sentinel_client import get_redis_sentinel_client

    return get_redis_sentinel_client()


def _decode(raw: Any) -> Any:
    return raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw


def get_config() -> dict[str, Any]:
    try:
        raw = _decode(_redis().get(_CONFIG_KEY))
        cfg = json.loads(raw) if raw else {}
    except Exception:  # noqa: BLE001 - Redis 不可读时按「未开启」处理，绝不误触发
        logger.warning("读取自动更新配置失败，按未开启处理", exc_info=True)
        cfg = {}
    return {"enabled": bool(cfg.get("enabled", False))}


def save_config(enabled: bool) -> dict[str, Any]:
    _redis().set(_CONFIG_KEY, json.dumps({"enabled": bool(enabled)}))
    logger.info("[AutoUpdate] 开关已%s", "开启" if enabled else "关闭")
    return {"enabled": bool(enabled)}


def get_last_decision() -> dict[str, Any] | None:
    try:
        raw = _decode(_redis().get(_LAST_KEY))
        return json.loads(raw) if raw else None
    except Exception:  # noqa: BLE001
        return None


def _record(action: str, reason: str, level: str = "info") -> None:
    payload = {
        "at": datetime.now().isoformat(timespec="seconds"),
        "action": action,
        "reason": reason,
    }
    try:
        _redis().set(_LAST_KEY, json.dumps(payload, ensure_ascii=False))
    except Exception:  # noqa: BLE001
        logger.warning("[AutoUpdate] 写入决策结果失败", exc_info=True)
    try:
        from backend.shared.system_events import record_system_event

        record_system_event(
            event_type="system_update",
            level=level,
            source="quantmind-auto-update",
            title=f"每日自动更新：{action}",
            message=reason,
            meta=payload,
        )
    except Exception:  # noqa: BLE001 - 事件记录非关键路径
        logger.warning("[AutoUpdate] 记录系统事件失败", exc_info=True)
    logger.info("[AutoUpdate] %s - %s", action, reason)


def _within_run_window(now: datetime) -> bool:
    run_at = datetime.strptime(RUN_AT, "%H:%M").time()
    start = now.replace(hour=run_at.hour, minute=run_at.minute, second=0, microsecond=0)
    return start <= now < start + timedelta(minutes=RUN_WINDOW_MINUTES)


def _claim_today(now: datetime) -> bool:
    """抢占当日决策权。多 worker / 窗口内多次轮询都只有一个赢家。"""
    try:
        return bool(
            _redis().set(
                _FIRED_KEY.format(date=now.strftime("%Y-%m-%d")),
                b"1",
                ex=2 * 24 * 3600,
                nx=True,
            )
        )
    except Exception:  # noqa: BLE001 - Redis 异常时不触发，避免无锁重复执行
        logger.warning("[AutoUpdate] 抢锁失败，本次跳过", exc_info=True)
        return False


async def _is_outdated() -> tuple[bool, str]:
    """版本闸门：只有 release-index 明确落后才放行。"""
    from backend.shared.version import check_updates

    result = await check_updates(force=True)
    if result is None:
        return False, "发布索引不可达，无法判断是否有新版本，跳过"
    if result.get("status") != "ok":
        return False, f"本地提交不在发布索引中（status={result.get('status')}），跳过"
    if result.get("is_up_to_date"):
        return False, "已是最新版本，无需更新"
    return True, f"落后 {result.get('behind')} 个提交，触发强制更新"


async def tick(now: datetime | None = None) -> None:
    """由后台循环每分钟调用一次；不满足条件时静默返回。"""
    now = now or datetime.now()
    if not get_config()["enabled"]:
        return
    if not _within_run_window(now):
        return
    if not _claim_today(now):
        return
    if not Path(_DOCKER_SOCKET).exists():
        _record("跳过", "docker socket 未挂载，无法执行更新", level="warning")
        return

    from backend.services.api.routers.admin.system_update import (
        UpdaterBusy,
        launch_updater,
    )

    outdated, reason = await _is_outdated()
    if not outdated:
        _record("跳过", reason)
        return
    try:
        data = await asyncio.to_thread(launch_updater, guard_dirty=True, source="auto")
    except UpdaterBusy:
        _record("跳过", "已有更新任务在执行中", level="warning")
        return
    except Exception as exc:  # noqa: BLE001
        _record("失败", f"触发更新失败：{exc}", level="error")
        return
    _record("已触发", f"{reason}（updater 容器 {data.get('task_id', '')[:12]}）")


async def _loop() -> None:
    while True:
        try:
            await tick()
        except Exception:  # noqa: BLE001
            logger.exception("[AutoUpdate] 轮询异常")
        await asyncio.sleep(_POLL_INTERVAL)


def start_auto_update_loop() -> asyncio.Task:
    """在 API lifespan 中启动每日自动更新轮询；已运行则复用。"""
    global _loop_task
    if _loop_task is None or _loop_task.done():
        _loop_task = asyncio.create_task(_loop())
    return _loop_task


async def stop_auto_update_loop() -> None:
    global _loop_task
    if _loop_task:
        _loop_task.cancel()
        try:
            await _loop_task
        except asyncio.CancelledError:
            pass
        _loop_task = None


def describe_state() -> dict[str, Any]:
    """供前端回显：开关、下次执行时间、最近一次决策。"""
    cfg = get_config()
    next_run: str | None = None
    if cfg["enabled"]:
        now = datetime.now()
        hour, minute = map(int, RUN_AT.split(":"))
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        next_run = candidate.isoformat(timespec="seconds")
    return {
        **cfg,
        "run_at": RUN_AT,
        "next_run_at": next_run,
        "available": Path(_DOCKER_SOCKET).exists(),
        "last_decision": get_last_decision(),
    }
