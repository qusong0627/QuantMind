"""EOD 五卡评分 worker（T-P4-05b）——交易日 16:00 后跑一次 ``run_all``，心跳入调度注册表。

职责：
- 每 60s 轮询：交易日且到点且当日未成功 → ``scripts/eval/run_all.py`` 的 ``run_all()``
  顺序评 因子/模型/策略/账户/每日选股 → ``eval_scores`` 落表；
- 单卡异常在 ``run_all`` 内部隔离（不阻塞整轮）；整轮无异常才写当日完成标记
  （``eval:scores:done:{date}``，TTL 3 天），有异常下一轮重试；
- 心跳写 ``scheduler_registry``（体检 C07 可见）。

环境变量：
  EVAL_SCORES_WORKER_ENABLED      默认 "1"
  EVAL_SCORES_TIME                默认 "16:00"（上海时区）
  EVAL_SCORES_CHECK_INTERVAL_SEC  默认 60
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

_DONE_KEY = "eval:scores:done:{date}"
_DONE_TTL_SECONDS = 3 * 24 * 3600

# 北京时间与 UTC 的固定偏移（A 股无夏令时）——与影子对照/双轨对账同口径
_CST_OFFSET = timedelta(hours=8)


def _env_bool(name: str, default: bool) -> bool:
    raw = str(os.getenv(name, "")).strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _config() -> dict:
    return {
        "enabled": _env_bool("EVAL_SCORES_WORKER_ENABLED", True),
        "time": str(os.getenv("EVAL_SCORES_TIME", "16:00")),
        "interval": max(10, int(os.getenv("EVAL_SCORES_CHECK_INTERVAL_SEC", "60"))),
    }


def parse_eval_time(raw: str) -> tuple[int, int]:
    """解析 ``HH:MM``，非法值回落 16:00。"""
    try:
        hour, minute = str(raw).strip().split(":", 1)
        h, m = int(hour), int(minute)
        if 0 <= h < 24 and 0 <= m < 60:
            return h, m
    except (TypeError, ValueError):
        pass
    return 16, 0


def _raw_client(redis) -> object | None:
    """兼容 RedisClient 包装器（``.client``）与原生 client（同影子对照纪律）。"""
    if redis is None:
        return None
    client = getattr(redis, "client", None)
    if client is not None:
        return client
    return redis if hasattr(redis, "set") else None


async def run_eval_scores_once(*, save: bool = True) -> dict:
    """执行一次五卡评分（手动重跑与 worker 共用同一入口）。"""
    from backend.scripts.eval.run_all import run_all

    summary = await run_all(save=save)
    logger.info(
        "[EvalScores] 五卡完成：落分=%s 异常=%s 耗时=%ss",
        summary.get("total_scored"),
        summary.get("total_errors"),
        summary.get("elapsed_sec"),
    )
    return summary


async def run_eval_scores_worker() -> None:
    """常驻循环：每交易日到点后跑一次，成功写 Redis 当日标记；心跳入调度注册表。"""
    cfg = _config()
    if not cfg["enabled"]:
        logger.info("[EvalScores] 评分任务关闭（EVAL_SCORES_WORKER_ENABLED=0）")
        return

    from backend.services.live_trading.services.trading_session import TZ
    from backend.services.trade_shared.redis_client import get_redis as get_trade_redis
    from backend.shared.scheduler_registry import heartbeat as _sched_heartbeat

    target_h, target_m = parse_eval_time(cfg["time"])
    logger.info(
        "[EvalScores] 评分任务启动：每交易日 %02d:%02d 执行", target_h, target_m
    )
    while True:
        try:
            _sched_heartbeat("eval_scores")
        except Exception:  # noqa: BLE001
            pass
        try:
            now = datetime.now(TZ)
            date_str = now.strftime("%Y%m%d")
            client = _raw_client(get_trade_redis())
            already = False
            if client is not None:
                try:
                    already = bool(client.exists(_DONE_KEY.format(date=date_str)))
                except Exception:  # noqa: BLE001
                    already = False
            if (
                now.weekday() < 5
                and (now.hour, now.minute) >= (target_h, target_m)
                and not already
            ):
                summary = await run_eval_scores_once(save=True)
                if client is not None and int(summary.get("total_errors") or 0) == 0:
                    try:
                        client.set(
                            _DONE_KEY.format(date=date_str), "1", ex=_DONE_TTL_SECONDS
                        )
                    except Exception:  # noqa: BLE001
                        pass
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error("[EvalScores] 评分任务异常: %s", exc, exc_info=True)
        await asyncio.sleep(cfg["interval"])
