"""滚动训练派发核心（P1 · 设计文档《滚动训练与模型生命周期》§4.2/§4.6）。

职责单一：把「一次滚动重训」从配置变成一次真实的训练提交 ——
就绪裁决（交易日历 + 因子分区）→ busy/内存守卫 → campaign 幂等裁决 →
payload 合成（配方 + 显式 split + rolling_meta）→ 复用既有 ``submit_training_job``。

四条纪律（每条都有事故形态对应）：

1. **不新增第二套提交路径**：真正提交一律走
   ``admin_training_utils.submit_training_job``（单飞锁/身份注入/因子解析都在那里）。
   admin 包只在函数体内 lazy import —— 模块顶层保持轻依赖，CLI/调度器/测试都能安全导入。
2. **busy 不消耗尝试**：单飞锁占用（409）记 ``failed(reason=busy_409)`` 但 attempts=0，
   :func:`decide_redispatch` 视其为可重试 —— 下一 tick 自动补发（验收 ②）。
3. **skip 不写周期标记**：数据未就绪/日历不可用等 skip 由调用方（retrain_scheduler）
   识别为「本轮不记账」，下一 tick 重试；告警每日一次（Redis SETNX 去重）。
4. **crash 窗口可自愈**：planned 行若最后状态迁移（updated_at）超 30 分钟
   （提交前后进程被杀），视为陈旧并重开重试——重开行保留旧 run_id 属预期，
   run_id 不参与陈旧判定；dispatched 行的终态由 run 回调/判尸回流，不在此处重投。
"""

from __future__ import annotations

import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

from backend.shared.training.recipe_registry import (
    Recipe,
    build_training_payload,
    load_recipe,
    recipe_hash,
)
from backend.shared.training.rolling_window import (
    DEFAULT_EXECUTION_LAG_DAYS,
    RollingWindow,
    WindowCalculationError,
    compute_window,
    parse_day,
    resolve_anchor,
)
from backend.shared.rolling_campaigns import (
    STATUS_PLANNED,
    build_campaign_id,
    decide_redispatch,
    get_campaign,
    get_campaign_by_window,
    insert_campaign,
    mark_dispatched,
    mark_failed,
    reopen_campaign,
)

logger = logging.getLogger(__name__)

#: 生产调度器归属标识（写进 rolling_meta.dispatched_by）。
DISPATCHED_BY_SCHEDULER = "retrain_dispatch"
#: 手动入口（CLI/端点显式调用）归属标识。
DISPATCHED_BY_MANUAL = "manual_api"

TRIGGER_SCHEDULE = "schedule"

#: 日历回看自然日数：窗口约 963 交易日出头，5 年自然日 ≈ 1215 交易日，留余量。
_CALENDAR_LOOKBACK_DAYS = 5 * 366

#: planned 行最后状态迁移（updated_at，回退 created_at）超过该时长 → 视作崩溃残留，
#: 允许重开（见模块 docstring 纪律 4）。
PLANNED_STALE_MINUTES = 30

#: 内存守卫阈值（GB 宿主 MemAvailable）；env 唯一读取点。
DEFAULT_MEM_MIN_GB = 50.0
MEM_MIN_ENV = "RETRAIN_MEM_MIN_GB"

#: 告警去重键：同一 reason 每日最多一条（TTL 2 天兜底清理）。
_ALERT_KEY = "qm:rolling:alert:{reason}:{day}"
_ALERT_TTL_SECONDS = 2 * 24 * 3600

CUSTOM_USER = {"tenant_id": "default", "user_id": "10000001"}


def _redis():
    import redis

    return redis.from_url(
        os.getenv("REDIS_URL", "redis://redis:6379/0"), socket_timeout=3
    )


# ─────────────────────────────────────────────────────────────────────────────
# 就绪裁决（纯函数）
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ReadinessResult:
    """就绪裁决结果：ready=False 时 reason/detail 说明为何跳过本轮。"""

    ready: bool
    reason: str
    detail: dict[str, Any] = field(default_factory=dict)
    anchor_date: date | None = None
    window: RollingWindow | None = None
    factor_min_date: str | None = None
    factor_max_date: str | None = None


def compute_plan(
    recipe: Recipe,
    *,
    calendar_days: list[Any] | None,
    factor_dates: list[Any] | None,
    today: date,
    anchor_override: Any = None,
    execution_lag_days: int = DEFAULT_EXECUTION_LAG_DAYS,
) -> ReadinessResult:
    """窗口就绪裁决（纯函数，不碰 IO）。

    顺序即优先级：日历 → 因子分区 → anchor（含数据滞后守卫）→ 窗口 → 历史覆盖。
    ``anchor_override``（手动回放用）跳过滞后守卫但保留覆盖性检查。
    """
    if not calendar_days:
        return ReadinessResult(False, "calendar_unavailable")
    days = sorted({parse_day(day) for day in calendar_days})
    if not days:
        return ReadinessResult(False, "calendar_unavailable")
    if not factor_dates:
        return ReadinessResult(False, "factor_source_empty")
    factor_days = sorted({parse_day(day) for day in factor_dates})
    factor_min, factor_max = factor_days[0], factor_days[-1]

    if anchor_override is not None:
        anchor = parse_day(anchor_override)
        if anchor > days[-1]:
            return ReadinessResult(
                False,
                "anchor_after_calendar",
                {"anchor_date": anchor.isoformat(), "calendar_end": days[-1].isoformat()},
            )
        if anchor > factor_max:
            return ReadinessResult(
                False,
                "anchor_beyond_factor_coverage",
                {
                    "anchor_date": anchor.isoformat(),
                    "factor_max_date": factor_max.isoformat(),
                },
                factor_min_date=factor_min.isoformat(),
                factor_max_date=factor_max.isoformat(),
            )
    else:
        # 数据滞后守卫：因子分区必须覆盖到「今天之前最近一个交易日」——
        # 否则上游同步断链，此刻算出来的窗口锚在陈旧数据上（整窗回退也无意义）。
        completed = [d for d in days if d < today]
        if not completed:
            return ReadinessResult(False, "calendar_no_completed_session")
        last_completed = completed[-1]
        if factor_max < last_completed:
            return ReadinessResult(
                False,
                "data_lag",
                {
                    "factor_max_date": factor_max.isoformat(),
                    "last_completed_trading_day": last_completed.isoformat(),
                },
                factor_min_date=factor_min.isoformat(),
                factor_max_date=factor_max.isoformat(),
            )
        anchor = resolve_anchor(factor_days, recipe.target_horizon_days, execution_lag_days)
        if anchor is None:
            return ReadinessResult(
                False,
                "factor_dates_insufficient",
                {
                    "available": len(factor_days),
                    "required": int(recipe.target_horizon_days) + int(execution_lag_days) + 1,
                },
                factor_min_date=factor_min.isoformat(),
                factor_max_date=factor_max.isoformat(),
            )

    try:
        window = compute_window(
            anchor, days, recipe.window_policy, recipe.target_horizon_days, execution_lag_days
        )
    except WindowCalculationError as exc:
        return ReadinessResult(
            False,
            "window_unavailable",
            {"error": str(exc)},
            anchor_date=anchor,
            factor_min_date=factor_min.isoformat(),
            factor_max_date=factor_max.isoformat(),
        )

    if factor_min > window.train_start:
        return ReadinessResult(
            False,
            "factor_history_insufficient",
            {
                "factor_min_date": factor_min.isoformat(),
                "train_start": window.train_start.isoformat(),
            },
            anchor_date=window.anchor_date,
            factor_min_date=factor_min.isoformat(),
            factor_max_date=factor_max.isoformat(),
        )

    return ReadinessResult(
        True,
        "ok",
        anchor_date=window.anchor_date,
        window=window,
        factor_min_date=factor_min.isoformat(),
        factor_max_date=factor_max.isoformat(),
    )


def planned_is_stale(
    existing: dict[str, Any] | None,
    *,
    now: datetime | None = None,
    stale_minutes: int = PLANNED_STALE_MINUTES,
) -> bool:
    """planned 且最后状态迁移（updated_at，回退 created_at）超 stale_minutes → 崩溃残留（纯函数）。

    run_id 不参与判定：重开行保留上一次尝试的 run_id 属预期（新鲜度看 reopen
    刷新的 updated_at）。若按「无 run_id」设卡，「重开后在提交前再次崩溃」的行
    会永久卡死在 planned。
    """
    if not existing or existing.get("status") != STATUS_PLANNED:
        return False
    stamp = existing.get("updated_at") or existing.get("created_at")
    if not stamp:
        return False
    try:
        last = datetime.fromisoformat(str(stamp))
    except (TypeError, ValueError):
        return False
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    return reference - last > timedelta(minutes=int(stale_minutes))


# ─────────────────────────────────────────────────────────────────────────────
# 运行时探针（Redis / PG / /proc）
# ─────────────────────────────────────────────────────────────────────────────


#: busy 探针专用引擎单例（NullPool；见 _get_probe_engine）。
_probe_engine: Any = None


def _get_probe_engine() -> Any:
    """busy 探针专用引擎：NullPool——每次 connect() 新建、释放即关。

    共享引擎（database_manager_v2）的池化连接会绑死在创建它的 event loop 上，
    而同步桥每 tick 一个 ``asyncio.run`` 短命 loop（retrain_scheduler._busy_probe /
    schedule_ctl 同款）→ 池化连接跨 loop 复用必炸 "attached to a different
    loop"（实测约半数 tick 误判 busy_probe_failed）。探针 60s 才一次，新建连接
    的成本可忽略。读取走主库：从库延迟会把刚插入的活动行藏起来，恰好制造本
    探针要防的并发窗口。
    """
    global _probe_engine
    if _probe_engine is None:
        from sqlalchemy.ext.asyncio import create_async_engine
        from sqlalchemy.pool import NullPool

        from backend.shared.database_manager_v2 import get_db_manager

        _probe_engine = create_async_engine(
            get_db_manager().config.get_master_url(), poolclass=NullPool
        )
    return _probe_engine


async def _query_active_training_row() -> Any:
    """admin_training_jobs 里最早的一条活动行（无 → None）。独立 NullPool 引擎。"""
    from sqlalchemy import text

    from backend.shared.training_singleflight import ACTIVE_STATUSES

    engine = _get_probe_engine()
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT id, status FROM admin_training_jobs "
                "WHERE status = ANY(:active) "
                "ORDER BY created_at ASC LIMIT 1"
            ),
            {"active": list(ACTIVE_STATUSES)},
        )
        return result.first()


async def probe_busy(redis_client: Any = None) -> dict[str, Any]:
    """侦查其它训练占位：单飞锁持有 ∨ admin_training_jobs 有活动行。

    探针自身失败一律按 busy 处理 —— 宁可跳过一轮（下一 tick 重试），
    不赌「Redis/DB 刚好挂了所以应该没事」去并发抢 44G 宿主内存。
    """
    try:
        from backend.shared.training_singleflight import get_holder

        client = redis_client if redis_client is not None else _redis()
        holder = get_holder(client)
    except Exception as exc:  # noqa: BLE001
        logger.error("[RollingDispatch] busy 探针（单飞锁）失败: %s", exc)
        return {"busy": True, "reason": "busy_probe_failed", "detail": str(exc)}
    if holder:
        return {"busy": True, "reason": "training_singleflight", "holder": str(holder)}

    try:
        row = await _query_active_training_row()
    except Exception as exc:  # noqa: BLE001
        logger.error("[RollingDispatch] busy 探针（活跃训练行）失败: %s", exc)
        return {"busy": True, "reason": "busy_probe_failed", "detail": str(exc)}
    if row:
        return {
            "busy": True,
            "reason": "active_training_job",
            "run_id": str(row[0]),
            "status": str(row[1]),
        }
    return {"busy": False, "reason": "idle"}


def read_mem_available_gb(path: str = "/proc/meminfo") -> float | None:
    """读宿主 MemAvailable（GB）；不可读返回 None（由调用方决定取向）。"""
    try:
        with open(path, encoding="ascii", errors="ignore") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / (1024.0 * 1024.0)
    except (OSError, ValueError, IndexError):
        return None
    return None


def mem_guard(min_gb: float | None = None, path: str = "/proc/meminfo") -> dict[str, Any]:
    """内存守卫：MemAvailable < 阈值 → ok=False（派发前跳过 + 告警一次/日）。

    /proc/meminfo 不可读时 fail-open（记 warning）：真正的串行化硬闸是单飞锁，
    本守卫是「锁没拦住的历史提交/外部占用」的额外缓冲，不是唯一防线。
    """
    threshold = (
        float(os.getenv(MEM_MIN_ENV, str(DEFAULT_MEM_MIN_GB)))
        if min_gb is None
        else float(min_gb)
    )
    available = read_mem_available_gb(path)
    if available is None:
        logger.warning("[RollingDispatch] %s 不可读，内存守卫 fail-open", path)
        return {"ok": True, "reason": "meminfo_unavailable", "min_gb": threshold}
    if available < threshold:
        return {
            "ok": False,
            "reason": "low_memory",
            "available_gb": round(available, 1),
            "min_gb": threshold,
        }
    return {"ok": True, "available_gb": round(available, 1), "min_gb": threshold}


def alert_once(reason: str, title: str, content: str, redis_client: Any = None) -> bool:
    """每日一次告警（Redis SETNX 去重；Redis 不可用时宁可多发不吞告警）。"""
    day = datetime.now().strftime("%Y-%m-%d")
    key = _ALERT_KEY.format(reason=reason, day=day)
    try:
        client = redis_client if redis_client is not None else _redis()
        first = bool(client.set(key, "1", nx=True, ex=_ALERT_TTL_SECONDS))
    except Exception:  # noqa: BLE001
        first = True
    if not first:
        return False
    try:
        from backend.shared.qq_notify import alert_async

        alert_async(level="warning", title=title, content=content, alert_type="rolling_retrain")
        return True
    except Exception:  # noqa: BLE001
        logger.exception("[RollingDispatch] 告警发送失败: %s", title)
        return False


# ─────────────────────────────────────────────────────────────────────────────
# IO 边界（薄封装，测试可整层 monkeypatch）
# ─────────────────────────────────────────────────────────────────────────────


def _calendar_days(calendar_market: str, today: date) -> list[date] | None:
    from backend.shared.trading_calendar import trading_days_xcal

    return trading_days_xcal(
        calendar_market, today - timedelta(days=_CALENDAR_LOOKBACK_DAYS), today
    )


def _factor_dates(factor_market: str, factor_source: str) -> list[str]:
    from backend.services.engine.data_platform.quantdb_factor_reader import (
        QuantDBFactorReader,
    )

    return QuantDBFactorReader(market=factor_market).available_dates(factor_source)


# ─────────────────────────────────────────────────────────────────────────────
# 派发主流程
# ─────────────────────────────────────────────────────────────────────────────


def _skip(status_reason: str, message: str, **extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"status": "skipped", "reason": status_reason, "message": message}
    out.update(extra)
    return out


async def execute_dispatch(
    *,
    market: str,
    recipe_id: str,
    trigger: str = TRIGGER_SCHEDULE,
    dry_run: bool = False,
    anchor_date: str | date | None = None,
    dispatched_by: str = DISPATCHED_BY_SCHEDULER,
    current_user: dict[str, Any] | None = None,
    submit_fn: Callable[..., Awaitable[dict[str, Any]]] | None = None,
    background_tasks: Any = None,
    redis_client: Any = None,
    today: date | None = None,
) -> dict[str, Any]:
    """一条龙派发：就绪 → 幂等 → 组 payload → 提交 → 记账。

    返回 dict（全部可 JSON 序列化），``status`` ∈
    ``dispatched | duplicate | skipped | dry_run``。异常路径向上抛（端点转 5xx，
    调度器据此不写 last_run，下一 tick 重试）。
    """
    recipe = load_recipe(recipe_id)
    market_up = str(market or "").strip().upper()
    if market_up and market_up != recipe.market.upper():
        raise ValueError(f"市场不匹配：请求 {market_up}，配方 {recipe_id} 属于 {recipe.market}")

    today = today or date.today()

    calendar_days = _calendar_days(recipe.calendar_market, today)
    if calendar_days is None:
        if dry_run:
            return {"status": "dry_run", "ready": False, "reason": "calendar_unavailable"}
        alert_once(
            "calendar",
            "滚动重训：交易日历不可用",
            f"market={recipe.market} recipe={recipe_id}：xcal 日历缺失或未覆盖 {today}，本轮跳过。",
            redis_client,
        )
        return _skip("calendar_unavailable", "交易日历缺失或未覆盖今天")

    try:
        factor_dates = _factor_dates(recipe.factor_market, recipe.factor_source)
    except Exception as exc:  # noqa: BLE001
        if dry_run:
            return {
                "status": "dry_run",
                "ready": False,
                "reason": "factor_source_unavailable",
                "detail": {"error": str(exc)},
            }
        alert_once(
            "factor_source",
            "滚动重训：因子源不可读",
            f"market={recipe.market} source={recipe.factor_source}：{exc}",
            redis_client,
        )
        return _skip("factor_source_unavailable", "因子源分区不可读", detail={"error": str(exc)})

    readiness = compute_plan(
        recipe,
        calendar_days=calendar_days,
        factor_dates=factor_dates,
        today=today,
        anchor_override=anchor_date,
    )
    if not readiness.ready:
        if dry_run:
            out = {"status": "dry_run", "ready": False, "reason": readiness.reason}
            if readiness.detail:
                out["detail"] = readiness.detail
            return out
        alert_once(
            f"data:{readiness.reason}",
            "滚动重训：数据未就绪",
            f"market={recipe.market} recipe={recipe_id} 跳过本轮：{readiness.reason} "
            f"{readiness.detail}",
            redis_client,
        )
        return _skip(readiness.reason, "数据未就绪", detail=readiness.detail)

    window = readiness.window
    assert window is not None  # ready=True 时必有窗口
    plan = window.to_plan()

    if dry_run:
        return {
            "status": "dry_run",
            "ready": True,
            "market": recipe.market,
            "recipe_id": recipe.recipe_id,
            "anchor_date": window.anchor_date.isoformat(),
            "window_index": window.window_index,
            "factor_min_date": readiness.factor_min_date,
            "factor_max_date": readiness.factor_max_date,
            "plan": plan,
        }

    busy = await probe_busy(redis_client)
    if busy.get("busy"):
        return _skip("busy", "有训练占位，本轮不派发", detail=busy)

    campaign_id = build_campaign_id(recipe.market, recipe.recipe_id, window.anchor_date, trigger)
    existing = await get_campaign_by_window(
        recipe.market, recipe.recipe_id, window.anchor_date, trigger
    )
    decision = decide_redispatch(existing)
    if decision["action"] == "reuse" and planned_is_stale(existing):
        decision = {"action": "redispatch", "reason": "stale_planned"}

    if decision["action"] == "reuse":
        return {
            "status": "duplicate",
            "reason": decision["reason"],
            "campaign_id": campaign_id,
            "campaign_status": (existing or {}).get("status"),
            "run_id": (existing or {}).get("run_id"),
            "anchor_date": window.anchor_date.isoformat(),
        }

    if decision["action"] == "create":
        row = await insert_campaign(
            campaign_id=campaign_id,
            market=recipe.market,
            recipe_id=recipe.recipe_id,
            recipe_hash=recipe_hash(recipe),
            trigger=trigger,
            anchor_date=window.anchor_date,
            window_index=window.window_index,
            purge_days=window.purge_days,
            window_policy=recipe.window_policy.to_dict(),
            window_plan=plan,
        )
        if row is None:
            # 并发同窗派发：唯一键挡住 → 以既有行重新裁决，绝不二次提交
            existing = await get_campaign_by_window(
                recipe.market, recipe.recipe_id, window.anchor_date, trigger
            )
            return {
                "status": "duplicate",
                "reason": decide_redispatch(existing)["reason"],
                "campaign_id": campaign_id,
                "campaign_status": (existing or {}).get("status"),
                "run_id": (existing or {}).get("run_id"),
                "anchor_date": window.anchor_date.isoformat(),
            }
    else:  # redispatch：重开既有 failed/skipped/崩溃残留 planned 行（campaign_id 不变）
        row = await reopen_campaign(existing["campaign_id"])
        if row is None:
            latest = await get_campaign(existing["campaign_id"])
            return {
                "status": "duplicate",
                "reason": f"race:{decide_redispatch(latest)['reason']}",
                "campaign_id": existing["campaign_id"],
                "campaign_status": (latest or {}).get("status"),
                "run_id": (latest or {}).get("run_id"),
                "anchor_date": window.anchor_date.isoformat(),
            }

    campaign_row = row
    campaign_id = campaign_row["campaign_id"]

    payload = build_training_payload(recipe, window, campaign_id, dispatched_by)
    payload["job_name"] = campaign_id
    payload["display_name"] = f"滚动重训 {recipe.recipe_id} @ {window.anchor_date.isoformat()}"

    if submit_fn is None:
        from backend.services.api.routers.admin.admin_training_utils import (
            submit_training_job,
        )

        submit_fn = submit_training_job
    user = dict(current_user or CUSTOM_USER)

    try:
        result = await submit_fn(payload, background_tasks, user)
    except Exception as exc:  # noqa: BLE001
        status_code = getattr(exc, "status_code", None)
        if status_code == 409:
            # 单飞锁占用：未消耗计算（attempts 不增长）→ 下一 tick 可重试
            await mark_failed(campaign_id, "busy_409", {"detail": str(getattr(exc, "detail", exc))})
            return _skip("busy", "提交时训练占位（409）", campaign_id=campaign_id)
        await mark_failed(campaign_id, "submit_failed", {"error": str(exc)})
        raise

    run_id = str((result or {}).get("runId") or "")
    if not run_id:
        await mark_failed(campaign_id, "submit_no_run_id", {"result": str(result)[:500]})
        raise RuntimeError(f"submit_training_job 未返回 runId（campaign={campaign_id}）")

    dispatched_row = await mark_dispatched(campaign_id, run_id)
    if dispatched_row is None:
        # 提交成功但状态未落：极端竞态（行被并发改写）。不吞——run 在跑，台账对不上。
        logger.error(
            "[RollingDispatch] campaign=%s run=%s 已提交但 mark_dispatched 未命中（状态竞态）",
            campaign_id,
            run_id,
        )

    logger.info(
        "[RollingDispatch] 已派发 campaign=%s run=%s anchor=%s window_index=%s",
        campaign_id,
        run_id,
        window.anchor_date.isoformat(),
        window.window_index,
    )
    return {
        "status": "dispatched",
        "campaign_id": campaign_id,
        "run_id": run_id,
        "market": recipe.market,
        "recipe_id": recipe.recipe_id,
        "anchor_date": window.anchor_date.isoformat(),
        "window_index": window.window_index,
        "attempts": (dispatched_row or {}).get("attempts"),
        "plan": plan,
    }
