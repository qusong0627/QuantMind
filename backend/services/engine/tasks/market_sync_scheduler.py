"""市场定时同步调度器。

前端每个市场 tab 可配置每天 HH:MM 定时同步上游数据（精确到分钟）。
配置存 Redis（db 0，key: quantmind:sync_schedule:{market}），
Celery beat 每分钟触发 dispatch_market_sync 检查是否有市场到点，
到点则派发对应市场同步任务（Redis 记录 last_run 防止重复触发）。

CUSTOM 市场不是上游数据源，而是「筛选保留合并因子」数据集（/data/quantcustom）
的重建入口 —— 与各市场同构地挂在同一套调度里（默认建议 03:00，在 A 股同步之后）。
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from backend.services.engine.tasks.schedule_gate import (
    no_data_window,
    not_due_yet,
    previous_trading_day,
)

logger = logging.getLogger(__name__)

_SCHEDULE_KEY = "quantmind:sync_schedule:{market}"
_LAST_RUN_KEY = "quantmind:sync_schedule_last_run:{market}:{date}"
#: 跨日补跑的去重键（值 = 补跑当天的日期）。**与 last_run 分开**是刻意的：
#: 补跑要在「当天正常班次还没到点」时提前跑一轮，如果占用当天的 last_run 名额，
#: 今晚到点的正常班次就会被去重逻辑挡掉，当天新数据反而永远不同步。
_CATCHUP_KEY = "quantmind:sync_schedule_catchup:{market}:{date}"
#: 数据没到位时的重试记账（次数 / 上次派发时间戳）。上游迟到是常态，
#: 只跑一次计划班次会让当天永远停在旧数据上；但重试要有天花板，不能每分钟重派。
_CATCHUP_TRIES_KEY = "quantmind:sync_schedule_catchup_tries:{market}:{date}"
_CATCHUP_AT_KEY = "quantmind:sync_schedule_catchup_at:{market}:{date}"
#: 每天最多派发几次（含计划班次本身），两次之间至少隔这么久。
CATCHUP_MAX_TRIES = 3
CATCHUP_MIN_GAP_S = 45 * 60

# 派发任务名：上游同步与自定义数据集重建的超时预算不同（后者是本地磁盘作业，
# 全量重建可达 40-60 分钟），因此 CUSTOM 走独立任务，见 celery_tasks.py。
_SYNC_TASK_NAME = "engine.tasks.run_market_scheduled_sync"
_CUSTOM_REBUILD_TASK_NAME = "engine.tasks.run_custom_dataset_rebuild"
#: HK 单独一个任务：它的链比别的市场长得多，共用的 6900s 预算装不下。
#: 实测（2026-11-06，按落盘时间戳反推）：K线 18–24 分钟 + CCASS 71 分钟 +
#: 索引/南向/L1 ≈ 89 分钟必做，之后还有 sdl / 因子集 / 雅虎元数据三段尾巴——
#: 尾巴每晚被砍，`ccass_factors` 因此停更 3 周没人发现。
_HK_SYNC_TASK_NAME = "engine.tasks.run_hk_scheduled_sync"


def task_name_for(market: str) -> str:
    if market == "CUSTOM":
        return _CUSTOM_REBUILD_TASK_NAME
    if market == "HK":
        return _HK_SYNC_TASK_NAME
    return _SYNC_TASK_NAME


# market -> (标签, 同步任务名)
MARKETS = {
    "A": "QuantDB A股",
    "US": "QuantUS 美股",
    "HK": "QuantHK 港股",
    "BC": "QuantBC 区块链",
    "FUTURES": "QuantFutures 期货",
    # 自定义市场不拉上游数据，而是重建「筛选保留合并因子」数据集
    # （/data/quantcustom，直读训练的输入；依赖 A 股同步先跑完）
    "CUSTOM": "QuantCustom 自定义因子集",
}

# 业务市场码 → 同步市场码（Redis 键第二段）。**CN 的同步码是 "A" 不是 "CN"**，
# CRYPTO 是 "BC"：同步键 `quantmind:sync_schedule_last_run:{token}:{date}` 按这套码写，
# 用 normalize_market 的 CN/CRYPTO 去拼键会永远查不到（体检 C08 假报「无同步记录」）。
_MARKET_TO_SYNC_TOKEN = {
    "CN": "A",
    "A": "A",
    "HK": "HK",
    "US": "US",
    "CRYPTO": "BC",
    "BC": "BC",
    "FUTURES": "FUTURES",
    "CUSTOM": "CUSTOM",
}


def sync_market_token(market: str | None) -> str:
    """业务市场码（CN/HK/US/CRYPTO…）→ 同步键市场码（A/HK/US/BC…）。

    无法识别的市场原样大写返回，由调用方决定是否回落。
    """
    raw = str(market or "CN").upper().strip()
    return _MARKET_TO_SYNC_TOKEN.get(raw, raw)


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
    # CUSTOM 在 A 股同步之后：先等 00:55 的 A 股同步把源数据（daily_forward +
    # 因子库）落盘，再合并出当日自定义分区；慢了也无妨（增量只补缺失分区）。
    "CUSTOM": "03:00",
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


def _has_run(market: str, date_str: str) -> bool:
    """该市场在 ``date_str``（**派发日期**，不是交易日期）是否已派发过。"""
    r = _redis()
    return r.exists(_LAST_RUN_KEY.format(market=market, date=date_str)) > 0


#: 旧名保留：语义就是「这一天是否跑过」，跨日补跑复用它查上一交易日。
_last_run_today = _has_run


def _catchup_done_today(market: str, date_str: str) -> bool:
    r = _redis()
    return r.exists(_CATCHUP_KEY.format(market=market, date=date_str)) > 0


def _mark_catchup(market: str, date_str: str) -> None:
    r = _redis()
    r.set(_CATCHUP_KEY.format(market=market, date=date_str), "1", ex=2 * 24 * 3600)


# ---------------------------------------------------------------------------
# 数据是否真的到位（补跑判据的唯一可信来源）
# ---------------------------------------------------------------------------
#: 每个市场的关键分区：**全部**覆盖到某交易日才算这个市场的数据齐了。
#: 只看其中一个会漏掉「同步跑到一半被杀」的情况——2026-11-05 崩机时
#: `daily_forward` 落了 11-05、`daily_unadjusted` 停在 11-04，只看前者会误判「已最新」。
#: 值为 (环境变量, 容器内默认根目录, 关键数据集相对路径)。
_KEY_PARTITIONS: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "A": (
        "QM_QUANTDB_DATA_DIR",
        "/data/quantdb",
        ("1_kline_data/daily_forward", "1_kline_data/daily_unadjusted"),
    ),
    "US": ("QM_QUANTUS_DATA_DIR", "/data/quantus", ("1_kline_data/daily_forward",)),
    "HK": ("QM_QUANTHK_DATA_DIR", "/data/quanthk", ("1_kline_data/daily_forward",)),
    "BC": ("QM_QUANTBC_DATA_DIR", "/data/quantbc", ("1_kline_data/daily_forward",)),
    "FUTURES": (
        "QM_QUANTFUTURES_DATA_DIR",
        "/data/quantfutures",
        ("1_kline_data/daily_forward",),
    ),
    # 自定义因子集：源是 A 股，产物在 quantcustom 的 l1_factors 分区
    "CUSTOM": (
        "QM_QUANTCUSTOM_DATA_DIR",
        "/data/quantcustom",
        ("6_ml_datasets/l1_factors",),
    ),
}


def _latest_partition_date(base: Path) -> date | None:
    """该数据集最新的 ``dt=YYYYMMDD`` 分区日；目录缺失/无分区返回 None。"""
    try:
        names = [p.name[3:] for p in base.iterdir() if p.name.startswith("dt=")]
    except OSError:
        return None
    dates = [n for n in names if len(n) == 8 and n.isdigit()]
    if not dates:
        return None
    try:
        return datetime.strptime(max(dates), "%Y%m%d").date()
    except ValueError:
        return None


def _market_data_covers(market: str, day: date | None) -> bool | None:
    """本地数据是否已经覆盖到 ``day``。

    True  = 关键分区全部到达该日（无需补跑）
    False = 确实没到（该补跑）
    None  = 无探针 / 目录不可读，判据不可用（调用方退回标记逻辑）
    """
    if day is None:
        return None
    spec = _KEY_PARTITIONS.get(market)
    if spec is None:
        return None
    env_key, default_root, rels = spec
    root = Path(os.getenv(env_key) or default_root)
    if not root.is_dir():
        return None
    latest: date | None = None
    for rel in rels:
        d = _latest_partition_date(root / rel)
        if d is None:
            return None  # 目录缺失/空：探针不可用，别拿它当「缺数据」
        latest = d if latest is None else min(latest, d)
    return latest >= day


def _catchup_tries(market: str, date_str: str) -> int | None:
    """今日已派发次数；Redis 读不到返回 None（调用方按「不许重试」处理）。"""
    try:
        raw = _redis().get(_CATCHUP_TRIES_KEY.format(market=market, date=date_str))
    except Exception:  # noqa: BLE001 - 记账读不到时宁可不再重派
        return None
    try:
        return int(raw or 0)
    except (TypeError, ValueError):
        return 0


def _catchup_last_at(market: str, date_str: str) -> float | None:
    try:
        raw = _redis().get(_CATCHUP_AT_KEY.format(market=market, date=date_str))
    except Exception:  # noqa: BLE001
        return None
    try:
        return float(raw or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _retry_allowed(market: str, now: datetime, date_str: str) -> bool:
    """数据缺时是否还能再补一次：限次 + 限频，避免每分钟重派。

    上游迟到是常态（用户侧实测数据 23:00 才发布），只跑一次计划班次的话，
    当天就永远停在旧数据上——所以「数据没到」必须能重试，但要有天花板。
    Redis 读不到时**不重试**（fail-closed）：拿不到次数就可能变成每分钟重派。
    """
    tries = _catchup_tries(market, date_str)
    if tries is None or tries >= CATCHUP_MAX_TRIES:
        return False
    last = _catchup_last_at(market, date_str)
    if last is None:
        return False
    if last and (now.timestamp() - last) < CATCHUP_MIN_GAP_S:
        return False
    return True


def _note_attempt(market: str, now: datetime, date_str: str) -> None:
    """记一次派发尝试（次数 + 时间戳），供 ``_retry_allowed`` 限频限次。"""
    tries = _catchup_tries(market, date_str)
    try:
        r = _redis()
        r.set(
            _CATCHUP_TRIES_KEY.format(market=market, date=date_str),
            str((tries or 0) + 1),
            ex=2 * 24 * 3600,
        )
        r.set(
            _CATCHUP_AT_KEY.format(market=market, date=date_str),
            str(now.timestamp()),
            ex=2 * 24 * 3600,
        )
    except Exception as exc:  # noqa: BLE001 - 记账失败只影响重试额度，不影响本次派发
        logger.warning("[SyncSchedule] 重试记账失败（不影响本次派发）: %s", exc)


def _catchup_needed(market: str, now: datetime, date_str: str) -> bool:
    """当日班次还没到点，但上一交易日的**数据**没到位 ⇒ 该补跑。

    2026-11-05 事故：宿主 21:44（北京）崩机、次日 09:03 才起，CN 排在 20:55 的班次
    整个丢掉。而派发判据只问「今天是否到点」——开机时 20:55 还没到，直接跳过，
    `daily_unadjusted` 于是停在崩机前一天，直到当晚 20:55 才可能补上；批量推理
    08:00/09:05 两班全部撞数据门禁，当天候选池空。

    2026-11-06 复盘：那次补跑**没触发**——判据看的是「上一交易日有没有派发标记」，
    而标记在**派发时**就写了（21:44 崩机前 20:55 已派发），系统认为「昨天跑过了」。
    现在改为先看**本地数据是否真的覆盖上一交易日**；探针不可用才退回标记判据。
    """
    prev = previous_trading_day(market, now.date())
    if prev is None:
        return False
    covered = _market_data_covers(market, prev)
    if covered is True:
        return False  # 数据已到位，补跑无意义
    if covered is False:
        return _retry_allowed(market, now, date_str)
    # 探针不可用（目录缺失/无分区）：维持旧判据
    if _catchup_done_today(market, date_str):
        return False
    return not _has_run(market, prev.isoformat())


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

        # 不跳过 PG：QuantDB 同步完成后滚动刷新 stock_daily_latest 最近 N 天
        # （run_daily_sync Phase 2，自带「QuantDB 未推进则跳过」门控）。
        result["result"] = run_daily_sync(skip_pg=False)
        return result

    if market == "CUSTOM":
        # 先增量更新三个经典因子库（alpha360/tdxgs/jq110）：它们没有独立
        # 调度、上游就是 QuantDB 日线，缺哪天分区，下面的合并就静默少列
        # （2026-09-14 起三库停更 → 合并产物少 122 列的事故形态）。
        # 三库都已有最新分区时整段跳过；build_libraries 内部只写缺失分区。
        from backend.scripts.build_factor_library import (
            ALL_LIBS,
            build_libraries,
            latest_source_date,
            libraries_up_to_date,
            update_start,
        )

        latest = latest_source_date()
        if latest and libraries_up_to_date(latest):
            result["libraries"] = {"skipped": f"三库已覆盖 {latest}"}
        elif latest:
            result["libraries"] = build_libraries(
                list(ALL_LIBS), update_start(), log=logger.info
            )
        else:
            # 源目录不可读：交给 rebuild 的空窗口/预检路径报错
            result["libraries"] = {"skipped": "daily_forward 源目录不可读"}

        # 重建「筛选保留合并因子」数据集（/data/quantcustom）；days/datasets/
        # with_qlib 不适用。增量守卫在 rebuild 内：筛选集或参数变化自动全量。
        from backend.scripts.build_factor_custom_dataset import (
            DEFAULT_MIN_COVERAGE,
            DEFAULT_START,
            rebuild,
        )

        result["result"] = rebuild(
            start=os.getenv("CUSTOM_DATASET_START") or DEFAULT_START,
            min_coverage=float(
                os.getenv("CUSTOM_DATASET_MIN_COVERAGE") or DEFAULT_MIN_COVERAGE
            ),
        )
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
        elapsed = (
            datetime.now() - datetime.fromisoformat(result["started"])
        ).total_seconds()
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
                logger.error(
                    "%s 定时同步 qlib 缓存失败: %s", market, exc, exc_info=True
                )
                result["qlib"] = {"status": "error", "reason": str(exc)}

    result["finished"] = datetime.now().isoformat()
    return result


def dispatch_due_syncs() -> dict[str, Any]:
    """检查所有市场定时配置，到点且今天未跑过的派发同步任务。

    **标记在派发成功之后写**，不在此之前。last_run 标记的语义是「今天这件事已经
    交出去了」，但它同时是唯一的去重依据：一旦先写标记再派发，任何派发失败
    （broker 写不进去、连接抖一下就够）都会留下一个「今天跑过了」的假记录，
    当天的同步永不重试——数据静默停在昨天，而所有依赖新数据的模型集体失效。
    2026-09-18 21:34 Redis `stop-writes-on-bgsave-error` 期间 beat 报
    `Couldn't apply scheduled task market-sync-dispatch`，走的正是这条路径。

    反过来的代价是：派发成功但进程随后崩溃会丢掉标记，下一分钟重复派发一次。
    各市场同步都是增量落分区，重复执行的代价远小于整天不执行。

    到点判据 = ``now >= 配置时刻``（``schedule_gate.not_due_yet``；审计 H4）：
    旧「精确分钟相等」判据下 worker 忙过 60s 就整天静默跳发，上面这句「下一
    分钟重复派发」根本不成立。迟到分钟/小时仍是当日首次到点，日键保证至多一次。

    交易日历门（P2-3 / M7）：``no_data_window``——{今日, 昨日} 两个自然日
    按该市场日历都非交易日（周日、长假内部日）时跳过并留 ``skipped`` 原因；
    周六凌晨取周五收盘的班次**不跳**（D-1 是交易日）。日历答不了 = 放行。

    跨日补跑（2026-11-05 崩机事故；2026-11-06 修正判据）：当日班次还**没到点**、
    且上一交易日的**本地数据没到位**时提前跑一轮（``_catchup_needed``）。判据改成
    看关键分区（``_market_data_covers``），不再看派发标记——标记在派发时就写了，
    任务中途被杀也照样「有标记」，11-06 早上正是因此没有补跑。数据缺时允许限次
    重试（``CATCHUP_MAX_TRIES`` / ``CATCHUP_MIN_GAP_S``），覆盖「上游数据迟到，
    计划班次跑空、之后再无同步」的老问题。补跑写独立的 catch-up 键，**不占**
    当天 last_run 名额，今晚的正常班次照跑。
    """
    from backend.services.engine.qlib_app.celery_config import celery_app

    now = datetime.now()
    now_hm = now.strftime("%H:%M")
    date_str = now.strftime("%Y-%m-%d")
    dispatched: list[str] = []
    catchup: list[str] = []
    skipped: dict[str, str] = {}

    for market in MARKETS:
        cfg = get_schedule(market)
        if not cfg.get("enabled"):
            continue
        due_today = not not_due_yet(now, str(cfg.get("time") or ""))
        prev_day = previous_trading_day(market, now.date())
        data_missing = _market_data_covers(market, prev_day) is False
        if due_today:
            if _has_run(market, date_str) and not (
                data_missing and _retry_allowed(market, now, date_str)
            ):
                # 当天班次派过、数据也到齐（或还在重试额度内等下一档）
                continue
        elif not _catchup_needed(market, now, date_str):
            continue
        idle_reason = no_data_window(market, now)
        if idle_reason:
            skipped[market] = idle_reason
            continue
        try:
            celery_app.send_task(
                task_name_for(market),
                args=[market, cfg],
                queue="qlib_backtest_srv",
            )
        except Exception as exc:  # noqa: BLE001 - 单个市场失败不拖垮同分钟的其他市场
            logger.error(
                "[SyncSchedule] %s 派发失败，今日不写标记以便下一分钟重试: %s",
                MARKETS[market],
                exc,
                exc_info=True,
            )
            continue
        _note_attempt(market, now, date_str)
        if not due_today:
            _mark_catchup(market, date_str)
            catchup.append(market)
            logger.info(
                "[SyncSchedule] %s 上一交易日（%s）数据未到位，%s 补跑",
                MARKETS[market],
                prev_day,
                now_hm,
            )
            continue
        _mark_run(market, date_str)
        dispatched.append(market)
        logger.info(
            "[SyncSchedule] %s 到点 %s，已派发同步任务", MARKETS[market], now_hm
        )

    return {
        "now": now_hm,
        "dispatched": dispatched,
        "catchup": catchup,
        "skipped": skipped,
    }
