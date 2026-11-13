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
- qlib 缓存兜底（2026-10-15）：市场夜链的 with_qlib 阶段自带预算闸——数据段
  耗时超过 MARKET_SYNC_SOFT_TIME_LIMIT/2（默认 900s）即整段跳过。HK（CCASS
  全市场爬约 1h）与 US（雅虎全量 30min+）结构性超时，qlib 缓存长期停更无人
  察觉（实测 HK 止 09-11、US 止 09-01），与因子集事故同一失效形态。本调度
  顺带按同一「落后才建」纪律追齐 qlib 缓存。A 股走自身链路内联维护、期货
  夜链数据段足够快不受预算闸影响，均不在兜底范围。
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
        # 机构级参与方日频流水（2026-11-13 新增；由同一份 CCASS 源重建）
        "ccass_participant_flows": (
            "6_ml_datasets/ccass_participant_flows",
            "2_base_sector/ccass_top50",
        ),
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

# 需要 qlib 缓存兜底的市场 → ensure_qlib_cache 的市场名（缓存路径由
# backend.shared.qlib_paths.resolve_qlib_provider_uri 单一出处解析）
QLIB_MARKETS = {"HK": "HK", "US": "US"}

# qlib 缓存新鲜度的基准来源（与因子集过期判据同一来源目录）
QLIB_SOURCE_REL = "1_kline_data/daily_forward"

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
    """目录下 dt=YYYYMMDD 分区的最大日期（要求 data.parquet 存在）。

    只看目录名会把「建了目录但写盘中断」的半成品当成最新，导致「落后才建」
    判据误判为已追平、永远不补。
    """
    d = root / rel
    if not d.is_dir():
        return None
    best: str | None = None
    for p in d.glob("dt=*"):
        v = p.name[3:]
        if (
            len(v) == 8
            and v.isdigit()
            and (p / "data.parquet").is_file()
            and (best is None or v > best)
        ):
            best = v
    return best


def _qlib_calendar_last(market: str) -> str | None:
    """该市场 qlib 缓存日历（calendars/day.txt）最后交易日，归一为 YYYYMMDD。

    缓存/日历缺失或读取失败一律返回 None——按「落后才建」判据即触发重建
    （ensure_qlib_cache 对未构建的缓存走全量构建，天然兜底）。
    """
    qlib_market = QLIB_MARKETS.get(market)
    if not qlib_market:
        return None
    try:
        from backend.shared.qlib_paths import resolve_qlib_provider_uri

        cal = Path(resolve_qlib_provider_uri(qlib_market)) / "calendars" / "day.txt"
        if not cal.is_file():
            return None
        last: str | None = None
        with open(cal, encoding="utf-8") as fh:
            for line in fh:
                s = line.strip()
                if s:
                    last = s
    except Exception as exc:  # noqa: BLE001 - 读不到日历=落后，交给重建兜底
        logger.warning("[FactorFill] %s qlib 日历读取失败: %s", market, exc)
        return None
    return last.replace("-", "") if last else None


def _rebuild_qlib(market: str) -> str:
    """按 qlib 缓存构建口径重建（日历 + 标的 + 特征整库重写），返回 provider_uri。"""
    from backend.services.engine.qlib_data_builder import ensure_qlib_cache

    return ensure_qlib_cache(market=QLIB_MARKETS[market])


def _catch_up_qlib(market: str, root: Path) -> dict[str, Any]:
    """qlib 缓存兜底：日历落后 daily_forward 最新分区时重建，追平即空跑。

    背景见模块 docstring 设计要点 3；失败不抛错，以 ``error`` 状态落盘
    （可见性走 freshness 的 qlib_cache 行与状态 JSON 的 qlib 字段）。
    """
    qlib_market = QLIB_MARKETS.get(market)
    if not qlib_market:
        return {"status": "not_applicable"}
    src = _latest_partition_dt(root, QLIB_SOURCE_REL)
    cal = _qlib_calendar_last(market)
    if not src:
        return {"status": "source_missing", "calendar": cal}
    if cal and cal >= src:
        return {"status": "up_to_date", "calendar": cal, "source_latest": src}
    try:
        provider_uri = _rebuild_qlib(market)
        logger.info("[FactorFill] %s qlib 缓存已追齐: %s -> %s", market, cal, src)
        return {
            "status": "rebuilt",
            "calendar": _qlib_calendar_last(market),
            "source_latest": src,
            "provider_uri": provider_uri,
        }
    except Exception as exc:  # noqa: BLE001 - 兜底失败不拖垮因子集结果主链路
        logger.exception("[FactorFill] %s qlib 缓存兜底重建失败: %s", market, exc)
        return {
            "status": "error",
            "calendar": cal,
            "source_latest": src,
            "error": str(exc),
        }


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
    # qlib 缓存单列一行（2026-10-15）：面板按 key 动态渲染，夜链预算闸跳过
    # with_qlib 导致缓存停更时，此处直接呈橙色（缓存日历 vs daily_forward）
    if market in QLIB_MARKETS:
        src = _latest_partition_dt(root, QLIB_SOURCE_REL)
        cal = _qlib_calendar_last(market)
        out["qlib_cache"] = {
            "latest": cal,
            "source_latest": src,
            "up_to_date": bool(cal and src and cal >= src),
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
            build_participant_flows,
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
    if name == "ccass_participant_flows":
        return build_participant_flows()
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

    # qlib 缓存兜底（2026-10-15）：独立于因子集数据集，任何一次到点运行都顺带
    # 追齐 qlib 缓存。qlib 失败不影响 datasets 主状态（见 _catch_up_qlib）
    result["qlib"] = _catch_up_qlib(market, root)

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
