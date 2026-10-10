"""市场因子数据集定时填充调度器。

与 market_sync_scheduler 同构：前端每个市场可配置每天 HH:MM 定时检查并补齐
本市场的训练直读因子数据集（`6_ml_datasets/`），配置存 Redis（db 0，
key: quantmind:factor_fill_schedule:{market}），celery beat 每分钟触发的
dispatch_market_sync 顺带检查是否有市场到点，到点则派发填充任务
（quantmind:factor_fill_last_run:{market}:{date} 防当天重复触发）。

设计要点（2026-10-08 港股/美股因子集停更 4 周事故的防线）：
- 因子集此前只是市场夜链的尾部步骤，上游限流把任务预算吃满时被超时截断，
  停更数周无人察觉。本调度独立于市场同步，自带「落后才建」判据：
  因子集最新分区 >= 来源数据最新分区时直接跳过（不重算、不空转），
  落后时按各生成器的增量语义补建 —— 正常日子秒级空跑，停更后自动追齐。
- ccass_factors 生成器全量重算（分钟级），过期判据保证它只在真正落后时重算。
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from backend.services.engine.tasks.schedule_gate import no_data_window, not_due_yet

logger = logging.getLogger(__name__)

_SCHEDULE_KEY = "quantmind:factor_fill_schedule:{market}"
_LAST_RUN_KEY = "quantmind:factor_fill_last_run:{market}:{date}"
_STATUS_KEY = "quantmind:factor_fill_status:{market}"

# 派发任务名（celery_tasks.py 注册；超时预算按 ccass 全量重算最坏情况放宽）
FACTOR_FILL_TASK_NAME = "engine.tasks.run_factor_fill_scheduled"

# market -> 标签（只收有 6_ml_datasets 因子集的市场；A 股/期货/加密不在本调度范围）
MARKETS = {
    "HK": "QuantHK 港股",
    "US": "QuantUS 美股",
}

# market -> {数据集名: (目标目录, 来源目录)}，相对该市场数据根（hub.data_dir）。
# 「来源最新分区」是过期判据的基准：因子集追平来源即视为最新。
FACTOR_DATASETS: dict[str, dict[str, tuple[str, str]]] = {
    "HK": {
        "l1_factors": ("6_ml_datasets/l1_factors", "1_kline_data/daily_forward"),
        "south_factors": ("6_ml_datasets/south_factors", "2_base_sector/hsgt_south"),
        "ccass_factors": ("6_ml_datasets/ccass_factors", "2_base_sector/ccass_top50"),
    },
    "US": {
        "l1_factors": ("6_ml_datasets/l1_factors", "1_kline_data/daily_forward"),
    },
}

DEFAULT_SCHEDULE = {
    "enabled": False,
    "time": "04:30",
    "datasets": [],
}

# 各市场在没有用户配置时的建议触发时间（仅供前端预填，不参与自动触发）。
# 排在各市场同步建议时间（HK 02:00 / US 05:30）加整条链时长余量之后；
# 「落后才建」判据保证提前跑、重复跑都是安全空跑。
#   HK  04:30  HK 夜链（K线→南向→L1→CCASS→因子集）通常 02:00 起 1-2h 内完成
#   US  07:30  US 夜链（雅虎限流下可跑 1.5h+）05:30 起
MARKET_SUGGESTED_TIMES: dict[str, str] = {
    "HK": "04:30",
    "US": "07:30",
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
    # 校验 time 格式 HH:MM；非法时回退到默认时间
    t = str(out["time"]).strip()
    try:
        datetime.strptime(t, "%H:%M")
        out["time"] = t
    except ValueError:
        out["time"] = DEFAULT_SCHEDULE["time"]
    # datasets 只允许该市场已登记的数据集名，防手改 Redis 后误派未知项
    valid = set(FACTOR_DATASETS.get(market or "", {}))
    out["datasets"] = [d for d in (out.get("datasets") or []) if d in valid]
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


def get_status(market: str) -> dict[str, Any] | None:
    """最近一次自动/手动填充的结果（含每数据集状态），无记录返回 None。"""
    r = _redis()
    raw = r.get(_STATUS_KEY.format(market=market))
    return json.loads(raw) if raw else None


def _save_status(market: str, result: dict[str, Any]) -> None:
    r = _redis()
    r.set(
        _STATUS_KEY.format(market=market),
        json.dumps(result, ensure_ascii=False, default=str),
        ex=30 * 24 * 3600,
    )


def _data_dir(market: str) -> Path:
    """市场数据根目录（与各生成器同一解析口径：env → /data/<market> → 项目根）。"""
    if market == "HK":
        from backend.services.engine.data_platform.quanthk_hub import QuantHKDataHub

        return Path(QuantHKDataHub().data_dir)
    from backend.services.engine.data_platform.quantus_hub import QuantUSDataHub

    return Path(QuantUSDataHub().data_dir)


def _latest_partition_dt(root: Path, rel: str) -> str | None:
    """目录下 dt=YYYYMMDD 分区的最大日期（YYYYMMDD 字符串，无分区返回 None）。"""
    d = root / rel
    if not d.is_dir():
        return None
    best: str | None = None
    for p in d.glob("dt=*"):
        v = p.name[3:]
        if len(v) == 8 and v.isdigit() and (best is None or v > best):
            best = v
    return best


def freshness(market: str) -> dict[str, dict[str, Any]]:
    """每数据集的新鲜度：因子集最新分区 vs 来源最新分区（不触发任何计算）。"""
    root = _data_dir(market)
    out: dict[str, dict[str, Any]] = {}
    for name, (target_rel, source_rel) in FACTOR_DATASETS.get(market, {}).items():
        tgt = _latest_partition_dt(root, target_rel)
        src = _latest_partition_dt(root, source_rel)
        out[name] = {
            "latest": tgt,
            "source_latest": src,
            "up_to_date": bool(tgt and src and tgt >= src),
        }
    return out


def _fill_dataset(market: str, name: str) -> dict[str, Any]:
    """调用对应生成器补建单个数据集（各生成器自带增量语义）。"""
    if name == "l1_factors":
        from backend.scripts.build_ml_l1_dataset import build_l1

        return build_l1("hong_kong" if market == "HK" else "us_stock")
    # 南向/CCASS 因子：本地信号数据集生成器（内部资产，不入库；缺失环境优雅跳过）
    try:
        from backend.scripts.build_ml_signal_datasets import (
            build_ccass_factors,
            build_south_factors,
        )
    except ModuleNotFoundError as exc:
        if "build_ml_signal_datasets" not in str(exc):
            raise
        return {"status": "skipped", "reason": "local module absent"}
    if name == "south_factors":
        return build_south_factors()
    if name == "ccass_factors":
        return build_ccass_factors()
    raise KeyError(f"未知数据集: {name}")


def run_factor_fill(market: str, cfg: dict[str, Any]) -> dict[str, Any]:
    """检查并补齐指定市场的因子数据集（「落后才建」，逐数据集隔离失败）。"""
    started = datetime.now()
    root = _data_dir(market)
    names = list(cfg.get("datasets") or FACTOR_DATASETS.get(market, {}).keys())
    result: dict[str, Any] = {
        "market": market,
        "started": started.isoformat(),
        "datasets": {},
    }

    for name in names:
        if name not in FACTOR_DATASETS.get(market, {}):
            result["datasets"][name] = {"status": "unknown_dataset"}
            continue
        target_rel, source_rel = FACTOR_DATASETS[market][name]
        src = _latest_partition_dt(root, source_rel)
        tgt = _latest_partition_dt(root, target_rel)
        if not src:
            result["datasets"][name] = {
                "status": "source_missing",
                "latest": tgt,
                "source_latest": None,
            }
            continue
        if tgt and tgt >= src:
            result["datasets"][name] = {
                "status": "up_to_date",
                "latest": tgt,
                "source_latest": src,
            }
            continue
        try:
            detail = _fill_dataset(market, name)
            new_latest = _latest_partition_dt(root, target_rel)
            result["datasets"][name] = {
                "status": "filled",
                "latest": new_latest,
                "source_latest": src,
                "detail": detail,
            }
            logger.info(
                "[FactorFill] %s %s 已补齐: %s -> %s", market, name, tgt, new_latest
            )
        except Exception as exc:  # noqa: BLE001 - 单数据集失败不拖垮其余数据集
            result["datasets"][name] = {
                "status": "error",
                "latest": tgt,
                "source_latest": src,
                "error": str(exc),
            }
            logger.exception("[FactorFill] %s %s 补建失败: %s", market, name, exc)

    statuses = {v.get("status") for v in result["datasets"].values()}
    if statuses <= {"up_to_date", "filled", "skipped"}:
        result["status"] = "ok"
    elif statuses == {"error"}:
        result["status"] = "error"
    else:
        result["status"] = "partial"
    result["finished"] = datetime.now().isoformat()
    result["elapsed_s"] = round((datetime.now() - started).total_seconds(), 1)
    _save_status(market, result)
    return result


def dispatch_due_factor_fills() -> dict[str, Any]:
    """检查各市场因子填充配置，到点且今天未跑过的派发填充任务。

    与 market_sync_scheduler.dispatch_due_syncs 同一纪律：**标记在派发成功
    之后写**。先写标记再派发，一次 broker 抖动就会留下「今天跑过了」的假记录，
    当天永不重试——因子集静默停在昨天的正是这类失效形态（2026-09-12~10-07
    事故的历史形态）。

    到点判据 = ``now >= 配置时刻``（``schedule_gate.not_due_yet``；审计 H4）：
    旧「精确分钟相等」判据下 worker 忙过 60s 就整天静默跳发，「落后才建」的
    自愈能力再强也没有机会启动。迟到仍是当日首次到点，日键保证至多一次。

    交易日历门（P2-3 / M7）：``no_data_window``——{今日, 昨日} 按该市场日历
    都非交易日（周日、长假内部日）时跳过并留 ``skipped`` 原因；周六凌晨补
    周五数据（HK 04:30 / US 07:30 的建议时刻正是此形态）**不跳**。日历
    答不了 = 放行（「落后才建」判据本就是最终闸门，此闸只省空跑）。
    """
    from backend.services.engine.qlib_app.celery_config import celery_app

    now = datetime.now()
    now_hm = now.strftime("%H:%M")
    date_str = now.strftime("%Y-%m-%d")
    dispatched: list[str] = []
    skipped: dict[str, str] = {}

    for market in MARKETS:
        cfg = get_schedule(market)
        if not cfg.get("enabled"):
            continue
        if not_due_yet(now, str(cfg.get("time") or "")):
            continue
        idle_reason = no_data_window(market, now)
        if idle_reason:
            skipped[market] = idle_reason
            continue
        if _last_run_today(market, date_str):
            continue
        try:
            celery_app.send_task(
                FACTOR_FILL_TASK_NAME,
                args=[market, cfg],
                queue="qlib_backtest_srv",
            )
        except Exception as exc:  # noqa: BLE001 - 单市场失败不拖垮同分钟的其他市场
            logger.error(
                "[FactorFill] %s 派发失败，今日不写标记以便下一分钟重试: %s",
                MARKETS[market],
                exc,
                exc_info=True,
            )
            continue
        _mark_run(market, date_str)
        dispatched.append(market)
        logger.info(
            "[FactorFill] %s 到点 %s，已派发因子填充任务", MARKETS[market], now_hm
        )

    return {"now": now_hm, "dispatched": dispatched, "skipped": skipped}
