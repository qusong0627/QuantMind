"""市场定时同步调度器。

前端每个市场 tab 可配置每天 HH:MM 定时同步上游数据（精确到分钟）。
配置存 Redis（db 0，key: quantmind:sync_schedule:{market}），
Celery beat 每分钟触发 dispatch_market_sync 检查是否有市场到点，
到点则派发对应市场同步任务（Redis 记录 last_run 防止重复触发）。
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

_SCHEDULE_KEY = "quantmind:sync_schedule:{market}"
_LAST_RUN_KEY = "quantmind:sync_schedule_last_run:{market}:{date}"

# market -> (标签, 同步任务名)
MARKETS = {
    "A": "QuantDB A股",
    "US": "QuantUS 美股",
    "HK": "QuantHK 港股",
    "BC": "QuantBC 区块链",
    "FUTURES": "QuantFutures 期货",
}

DEFAULT_SCHEDULE = {
    "enabled": False,
    "time": "03:00",
    "days": 5,
    "datasets": [],
    "with_qlib": False,
}

# 各市场在没有用户配置时的建议触发时间（仅供前端「同步调度」预填，不参与自动触发）。
#
# 自动同步是否开启、何时触发，一律以用户在前端保存的配置为准（Redis
# quantmind:sync_schedule:{market}，由 market-sync-dispatch 每分钟比对派发）。
# 任何市场都不再内置 enabled=true 的默认调度：否则所有部署会在同一固定时刻
# 全量同步，给上游数据源与服务器造成突发压力。
# 建议时间遵循既有约定：每日自动同步上游数据建议设置到次日 00:00 以后，
# 按需错峰触发，避免集中请求。下列仅为前端预填的推荐值，各市场互不错峰：
#   A        01:00  QuantDB release 盘后发布，次日凌晨已就绪
#   HK       02:00  雅虎/akshare/CCASS 次日凌晨陆续就绪
#   FUTURES  03:00  日盘/夜盘结算数据落库后
#   BC       04:15  加密市场全天候交易，选凌晨低谷时段拉取
#   US       05:30  美股收盘(北京约 04:00/05:00)后，EOD 数据已稳定
MARKET_SUGGESTED_TIMES: dict[str, str] = {
    "A": "01:00",
    "HK": "02:00",
    "FUTURES": "03:00",
    "BC": "04:15",
    "US": "05:30",
}


def _redis():
    import redis

    return redis.from_url(
        os.getenv("REDIS_URL", "redis://redis:6379/0"), socket_timeout=3
    )


def _normalize(cfg: dict[str, Any] | None, market: str | None = None) -> dict[str, Any]:
    out = dict(DEFAULT_SCHEDULE)
    # 建议时间只用于预填，不会把 enabled 置为 True
    if market is not None and market in MARKET_SUGGESTED_TIMES:
        out["time"] = MARKET_SUGGESTED_TIMES[market]
    for k in out:
        if k in (cfg or {}):
            out[k] = cfg[k]
    # 校验 time 格式 HH:MM；非法时回退到全局默认时间
    t = str(out["time"]).strip()
    try:
        datetime.strptime(t, "%H:%M")
        out["time"] = t
    except ValueError:
        out["time"] = DEFAULT_SCHEDULE["time"]
    return out


def get_schedule(market: str) -> dict[str, Any]:
    r = _redis()
    raw = r.get(_SCHEDULE_KEY.format(market=market))
    cfg = json.loads(raw) if raw else None
    return _normalize(cfg, market)


def get_all_schedules() -> dict[str, dict[str, Any]]:
    return {m: get_schedule(m) for m in MARKETS}


def save_schedule(market: str, cfg: dict[str, Any]) -> dict[str, Any]:
    normalized = _normalize(cfg, market)
    r = _redis()
    r.set(
        _SCHEDULE_KEY.format(market=market),
        json.dumps(normalized, ensure_ascii=False),
    )
    return normalized


def _last_run_today(market: str, date_str: str) -> bool:
    r = _redis()
    return r.exists(_LAST_RUN_KEY.format(market=market, date=date_str)) > 0


def _mark_run(market: str, date_str: str) -> None:
    r = _redis()
    r.set(
        _LAST_RUN_KEY.format(market=market, date=date_str),
        "1",
        ex=2 * 24 * 3600,
    )


def run_market_sync(market: str, cfg: dict[str, Any]) -> dict[str, Any]:
    """执行指定市场的同步（按配置的数据集/天数）。"""
    days = int(cfg.get("days") or 5)
    datasets = cfg.get("datasets") or []
    with_qlib = bool(cfg.get("with_qlib"))

    result: dict[str, Any] = {"market": market, "started": datetime.now().isoformat()}

    if market == "A":
        from backend.scripts.quantdb_daily_sync import run_daily_sync

        result["result"] = run_daily_sync(skip_pg=True)
        return result

    if market == "US":
        from backend.scripts.quantus_daily_sync import run
    elif market == "HK":
        from backend.scripts.quanthk_daily_sync import run
    elif market == "BC":
        from backend.scripts.quantbc_daily_sync import run
    elif market == "FUTURES":
        from backend.scripts.quantfutures_daily_sync import run
    else:
        return {"market": market, "error": f"未知市场: {market}"}

    kwargs: dict[str, Any] = {"days": days}
    if datasets:
        kwargs["datasets"] = list(datasets)
    result["result"] = run(**kwargs)

    if with_qlib:
        # 数据拉取阶段若被上游限流拖长，再重建 qlib 缓存会超出任务硬超时被 SIGKILL。
        # 这里按已耗时判断剩余时间是否够用，不够则跳过并在结果里标记 skipped。
        elapsed = (datetime.now() - datetime.fromisoformat(result["started"])).total_seconds()
        budget = float(os.getenv("MARKET_SYNC_SOFT_TIME_LIMIT", "1800"))
        if elapsed > budget * 0.5:
            logger.error(
                "[SyncSchedule] %s 数据拉取已耗时 %.0fs，超过预算 %.0fs 的一半，跳过 qlib 缓存重建",
                market,
                elapsed,
                budget,
            )
            result["qlib"] = {
                "status": "skipped",
                "reason": f"data stage took {elapsed:.0f}s, too long to rebuild qlib cache",
            }
        else:
            try:
                from backend.services.engine.qlib_data_builder import ensure_qlib_cache

                qlib_market = {
                    "US": "US",
                    "HK": "HK",
                    "BC": "CRYPTO",
                    "FUTURES": "FUTURES",
                }[market]
                result["qlib"] = {
                    "status": "ok",
                    "provider_uri": ensure_qlib_cache(market=qlib_market),
                }
            except Exception as exc:  # noqa: BLE001
                logger.error("%s 定时同步 qlib 缓存失败: %s", market, exc, exc_info=True)
                result["qlib"] = {"status": "error", "reason": str(exc)}

    result["finished"] = datetime.now().isoformat()
    return result


def dispatch_due_syncs() -> dict[str, Any]:
    """检查所有市场定时配置，到点且今天未跑过的派发同步任务。"""
    from backend.services.engine.qlib_app.celery_config import celery_app

    now = datetime.now()
    now_hm = now.strftime("%H:%M")
    date_str = now.strftime("%Y-%m-%d")
    dispatched: list[str] = []

    for market in MARKETS:
        cfg = get_schedule(market)
        if not cfg.get("enabled"):
            continue
        if cfg.get("time") != now_hm:
            continue
        if _last_run_today(market, date_str):
            continue
        _mark_run(market, date_str)
        celery_app.send_task(
            "engine.tasks.run_market_scheduled_sync",
            args=[market, cfg],
            queue="qlib_backtest_srv",
        )
        dispatched.append(market)
        logger.info("[SyncSchedule] %s 到点 %s，已派发同步任务", MARKETS[market], now_hm)

    return {"now": now_hm, "dispatched": dispatched}
