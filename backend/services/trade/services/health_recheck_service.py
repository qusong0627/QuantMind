"""月度体检复检 worker（T-P4-06 ③）——每月首周对 SIM/LIVE 策略重跑回测体检，心跳入注册表。

- 每月 1–7 日为执行窗口（3600s 轮询兜底）；本月完成（无异常）写 Redis 标记
  ``health:recheck:done:{YYYY-MM}``，有异常下一轮重试（策略级异常隔离在 run 内）；
- 复检实现唯一入口 = ``scripts/eval/health_recheck.py::run_health_recheck``
  （schedule_ctl 手动重跑共用，禁止旁路）。

环境变量：
  HEALTH_RECHECK_ENABLED        默认 "1"
  HEALTH_RECHECK_WINDOW_DAYS    默认 7（每月前 N 天为首周窗口）
  HEALTH_RECHECK_INTERVAL_SEC   默认 3600
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime

logger = logging.getLogger(__name__)

_DONE_KEY = "health:recheck:done:{month}"
_DONE_TTL_SECONDS = 45 * 24 * 3600


def _env_bool(name: str, default: bool) -> bool:
    raw = str(os.getenv(name, "")).strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _config() -> dict:
    return {
        "enabled": _env_bool("HEALTH_RECHECK_ENABLED", True),
        "window_days": max(
            1, min(28, int(os.getenv("HEALTH_RECHECK_WINDOW_DAYS", "7")))
        ),
        "interval": max(60, int(os.getenv("HEALTH_RECHECK_INTERVAL_SEC", "3600"))),
    }


def in_recheck_window(day: int, window_days: int) -> bool:
    """执行窗口判定（纯函数）：每月 1..window_days。"""
    return 1 <= int(day) <= int(window_days)


def _raw_client(redis) -> object | None:
    if redis is None:
        return None
    client = getattr(redis, "client", None)
    if client is not None:
        return client
    return redis if hasattr(redis, "set") else None


async def run_health_recheck_once(
    *, tenant_id: str = "default", save: bool = True
) -> dict:
    """执行一次月度复检（手动重跑与 worker 共用同一入口）。"""
    from backend.scripts.eval.health_recheck import run_health_recheck

    return await run_health_recheck(tenant_id=tenant_id, save=save)


async def run_health_recheck_worker() -> None:
    """常驻循环：每月首周跑一次；成功（无异常）写当月标记；心跳入调度注册表。"""
    cfg = _config()
    if not cfg["enabled"]:
        logger.info("[HealthRecheck] 月度复检任务关闭（HEALTH_RECHECK_ENABLED=0）")
        return

    from backend.services.trade_shared.redis_client import get_redis as get_trade_redis
    from backend.shared.scheduler_registry import heartbeat as _sched_heartbeat

    logger.info(
        "[HealthRecheck] 月度复检任务启动：每月前 %d 天执行（%ds 轮询）",
        cfg["window_days"],
        cfg["interval"],
    )
    while True:
        try:
            _sched_heartbeat("health_recheck")
        except Exception:  # noqa: BLE001
            pass
        try:
            now = datetime.now()
            month = now.strftime("%Y-%m")
            client = _raw_client(get_trade_redis())
            already = False
            if client is not None:
                try:
                    already = bool(client.exists(_DONE_KEY.format(month=month)))
                except Exception:  # noqa: BLE001
                    already = False
            if in_recheck_window(now.day, cfg["window_days"]) and not already:
                summary = await run_health_recheck_once(save=True)
                if client is not None and not summary.get("errors"):
                    try:
                        client.set(
                            _DONE_KEY.format(month=month), "1", ex=_DONE_TTL_SECONDS
                        )
                    except Exception:  # noqa: BLE001
                        pass
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error("[HealthRecheck] 月度复检任务异常: %s", exc, exc_info=True)
        await asyncio.sleep(cfg["interval"])
