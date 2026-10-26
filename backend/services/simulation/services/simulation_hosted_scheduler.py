from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

try:
    from exchange_calendars import get_calendar
except ImportError:  # pragma: no cover - optional in local/unit test env
    get_calendar = None

from backend.services.trade_shared.redis_client import RedisClient
from backend.services.simulation.services.rebalance_job_service import (
    SimulationRebalanceJobService,
)
from backend.shared.market_sessions import (
    market_calendar,
    market_timezone,
    session_ranges_local,
    in_session_hhmm,
)

logger = logging.getLogger(__name__)

_SH_TZ = ZoneInfo("Asia/Shanghai")


def _job_naive_shanghai(value: datetime) -> datetime:
    """瞬时 → ``simulation_rebalance_jobs`` 的时间口径（naive 上海墙钟）。

    作业表三列（planned_run_at/window_start_at/window_end_at）都是
    ``timestamp without time zone``，而 ``expire_outdated_jobs`` 也按 naive
    上海比较。传 aware datetime 会让 asyncpg 在 INSERT 前就抛
    ``DataError: can't subtract offset-naive and offset-aware datetimes``
    ——2026-10-19 托管调度每 30s 崩在 planned_run_at 上，整日一轮都没跑成。
    """
    if value.tzinfo is None:
        return value.replace(microsecond=0)
    return value.astimezone(_SH_TZ).replace(microsecond=0, tzinfo=None)


_DEFAULT_LIVE_TRADE_CONFIG: dict[str, Any] = {
    "rebalance_days": 3,
    "schedule_type": "interval",
    "trade_weekdays": [],
    "enabled_sessions": ["PM"],
    "sell_time": "14:45",
    "buy_time": "14:50",
    "sell_first": True,
    "order_type": "MARKET",
    "max_price_deviation": 0.02,
    "max_orders_per_cycle": 20,
    "trigger_window_seconds": 90,
    # 全局股票池（P3）：非空时信号与调仓只在该池内进行（严格语义）
    "pool_id": None,
}


@dataclass(frozen=True)
class SimulationScheduleDecision:
    should_trigger: bool
    phase: str
    trade_date: str
    reason: str


@dataclass(frozen=True)
class SimulationNextTrigger:
    phase: str
    trade_date: str
    target_at: datetime
    window_start_at: datetime
    window_end_at: datetime
    reason: str


def _to_int(value: Any, default: int) -> int:
    try:
        number = int(float(value))
        return number if math.isfinite(number) else default
    except Exception:
        return default


def _normalize_live_trade_config(value: Any) -> dict[str, Any]:
    cfg = value if isinstance(value, dict) else {}
    merged = {**_DEFAULT_LIVE_TRADE_CONFIG, **cfg}
    merged["schedule_type"] = str(merged.get("schedule_type") or "interval").lower()
    merged["trade_weekdays"] = [
        str(item).upper() for item in (merged.get("trade_weekdays") or [])
    ]
    merged["enabled_sessions"] = [
        str(item).upper() for item in (merged.get("enabled_sessions") or ["PM"])
    ]
    merged["order_type"] = str(merged.get("order_type") or "MARKET").upper()
    merged["rebalance_days"] = max(1, _to_int(merged.get("rebalance_days"), 3))
    # 全局股票池（P3）：空串归一为 None，避免下游把 "" 当成池引用去解析
    merged["pool_id"] = str(merged.get("pool_id") or "").strip() or None
    merged["max_orders_per_cycle"] = max(
        1, _to_int(merged.get("max_orders_per_cycle"), 20)
    )
    merged["trigger_window_seconds"] = max(
        30, _to_int(merged.get("trigger_window_seconds"), 90)
    )
    return merged


def hosted_cycle_ready(phase: str) -> bool:
    """卖/买分窗时只在 BUY/ALL 跑一整轮：SimulationEngine 是先卖后买原子调仓。"""
    return str(phase or "").upper() in {"BUY", "ALL"}


#: 跳过原因 → 可读文案（T-RC-14）：控制台「为什么今天没动」的唯一答案。
#: 键与 ``SimulationScheduleDecision.reason`` 逐一对应，新增原因必须在此补齐。
SKIP_REASON_TEXT: dict[str, str] = {
    "non_trading_day": "非交易日",
    "outside_session": "不在已启用执行时段内",
    "weekday_skip": "周内日不匹配（weekly 调度）",
    "interval_skip": "未到调仓日（interval 节奏）",
    "before_window": "尚未进入当日触发窗口",
    "sell_only_window": "处于卖出时段：引擎买卖原子执行，仅在买入窗口跑整轮",
    "lock_held": "该执行窗口已触发过（幂等锁占用）",
    "lock_error": "分布式执行锁获取失败",
    # minibt 模拟盘桥：目标由侧车按策略计算，未就绪时**显式跳过**，绝不回落到模型信号。
    "minibt_target_pending": "minibt 目标计算中（侧车尚未返回）",
    "minibt_target_stale": "minibt 目标已过期（超过新鲜度上限）",
    "minibt_target_error": "minibt 目标计算失败",
    "minibt_target_unavailable": "minibt 目标不可用（侧车无响应或重试耗尽）",
}


def skip_reason_text(reason: str) -> str:
    """原因码 → 文案；未知码原样透出，不静默吞掉。"""
    key = str(reason or "").strip()
    return SKIP_REASON_TEXT.get(key, key or "未知原因")


def hosted_sim_log(
    *,
    tenant_id: str,
    user_id: str,
    line: str,
    strategy_id: str = "",
    level: str = "info",
    **kwargs: Any,
) -> None:
    """托管模拟链路写运行日志（source=hosted_sim）。惰性 import，绝不抛出。"""
    try:
        from backend.services.live_trading.services.runtime_log_stream import (
            SOURCE_HOSTED_SIM,
            log_runtime,
        )

        log_runtime(
            tenant_id=tenant_id,
            user_id=user_id,
            line=line,
            level=level,
            source=SOURCE_HOSTED_SIM,
            strategy_id=strategy_id,
            **kwargs,
        )
    except Exception as exc:  # noqa: BLE001 - 日志失败不得影响调度
        logger.debug("hosted sim runtime log failed: %s", exc)


def hosted_sim_skip_once(
    *, tenant_id: str, user_id: str, reason: str, strategy_id: str = ""
) -> None:
    """跳过原因**变化时**记一条（30s 轮询下同因不重复写，见 log_skip_once）。"""
    try:
        from backend.services.live_trading.services.runtime_log_stream import (
            SOURCE_HOSTED_SIM,
            runtime_log_stream,
        )

        runtime_log_stream.log_skip_once(
            tenant_id=tenant_id,
            user_id=user_id,
            reason=reason,
            line=f"本轮未触发：{skip_reason_text(reason)}",
            level="debug",
            stage="skip",
            status="skipped",
            source=SOURCE_HOSTED_SIM,
            strategy_id=strategy_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("hosted sim skip log failed: %s", exc)


def hosted_sim_clear_skip_marker(*, tenant_id: str, user_id: str) -> None:
    """离开跳过态（本轮真的触发）后复位标记，下次跳过能照常记一条。"""
    try:
        from backend.services.live_trading.services.runtime_log_stream import (
            runtime_log_stream,
        )

        runtime_log_stream.clear_skip_marker(tenant_id=tenant_id, user_id=user_id)
    except Exception as exc:  # noqa: BLE001
        logger.debug("hosted sim skip marker clear failed: %s", exc)


def report_to_hosted_result(report: Any) -> dict[str, Any]:
    return {
        "task_id": getattr(report, "run_id", None),
        "status": "failed" if getattr(report, "error", None) else "succeeded",
        "error": getattr(report, "error", None),
        "signal_count": getattr(report, "signal_count", 0),
        "order_count": getattr(report, "order_count", 0),
        "filled_count": getattr(report, "filled_count", 0),
        "rejected_count": getattr(report, "rejected_count", 0),
        "capped_count": getattr(report, "capped_order_count", 0),
    }


async def run_simulation_cycle_for_active(
    *,
    tenant_id: str,
    user_id: str,
    strategy_id: str,
    live_trade_config: dict[str, Any] | None = None,
    run_id: str | None = None,
    exclude_symbols: set[str] | None = None,
    quantity_overrides: dict[tuple[str, str], int] | None = None,
    log_source: str | None = "hosted_sim",
    minibt_target: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """托管模拟盘唯一执行入口：RebalanceCalculator + ashare_matcher。

    ``minibt_target`` 非空时走 minibt 桥：信号与目标股数都由侧车给出，
    **不读模型信号表**；撮合/台账/风控仍走同一条引擎链路。
    """
    from backend.services.simulation.engine import simulation_engine

    cfg = _normalize_live_trade_config(live_trade_config)
    params_override: dict[str, Any] = {}
    if cfg.get("pool_id"):
        params_override["pool_id"] = cfg["pool_id"]
    signals_override = None
    target_quantities = None
    if minibt_target:
        from datetime import date as _date

        from backend.services.simulation.services.signal_loader import SignalScore

        symbol = str(minibt_target.get("symbol") or "").strip()
        shares = max(0, int(float(minibt_target.get("target_shares") or 0)))
        target_date = str(minibt_target.get("trade_date") or "")[:10]
        if not symbol or not target_date:
            raise RuntimeError("minibt target 缺少 symbol/trade_date")
        signals_override = [
            SignalScore(
                symbol=symbol,
                score=1.0 if shares > 0 else 0.0,
                trade_date=_date.fromisoformat(target_date),
                run_id=str(minibt_target.get("request_id") or run_id or "minibt"),
                tenant_id=tenant_id,
                user_id=user_id,
            )
        ]
        target_quantities = {symbol: shares}
        params_override["topk"] = 1
    report = await simulation_engine.run_cycle(
        tenant_id=tenant_id,
        user_id=user_id,
        strategy_id=strategy_id,
        run_id=run_id,
        params_override=params_override or None,
        pool_id=cfg.get("pool_id"),
        exclude_symbols=exclude_symbols,
        quantity_overrides=quantity_overrides,
        log_source=log_source,
        max_orders=cfg.get("max_orders_per_cycle"),
        signals_override=signals_override,
        target_quantities=target_quantities,
    )
    result = report_to_hosted_result(report)
    # T-FE-05 v2：人工改量裁定随报告透传（前端如实展示 applied/ignored）
    adjustments = getattr(report, "quantity_adjustments", None)
    if adjustments:
        result["quantity_adjustments"] = list(adjustments)
    return result


async def preview_simulation_plan_for_active(
    *,
    tenant_id: str,
    user_id: str,
    strategy_id: str,
    live_trade_config: dict[str, Any] | None = None,
    exclude_symbols: set[str] | None = None,
) -> dict[str, Any]:
    """调仓计划预演（T-FE-05，只读）：dry-run run_cycle —— 与执行入口共用同一
    RebalanceCalculator 与池/风控前置，但**绝不撮合、不落单、不写快照**。

    返回 {available, dry_run, order_count, planned orders, signal_count, error}。
    """
    from backend.services.simulation.engine import simulation_engine

    cfg = _normalize_live_trade_config(live_trade_config)
    params_override: dict[str, Any] = {}
    if cfg.get("pool_id"):
        params_override["pool_id"] = cfg["pool_id"]
    report = await simulation_engine.run_cycle(
        tenant_id=tenant_id,
        user_id=user_id,
        strategy_id=strategy_id,
        params_override=params_override or None,
        pool_id=cfg.get("pool_id"),
        dry_run=True,
        exclude_symbols=exclude_symbols,
        # 预演必须与执行同口径（同上限），否则预演条数是假预告
        max_orders=cfg.get("max_orders_per_cycle"),
    )
    return {
        "available": True,
        "dry_run": True,
        "order_count": int(report.order_count),
        "signal_count": int(report.signal_count),
        "orders": list(report.planned_orders),
        "error": report.error,
    }


async def execute_simulation_plan_for_active(
    *,
    tenant_id: str,
    user_id: str,
    strategy_id: str,
    live_trade_config: dict[str, Any] | None = None,
    exclude_symbols: set[str] | None = None,
    quantity_overrides: dict[tuple[str, str], int] | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    """交易台"一键执行"（T-FE-05，人工触发一次调仓）——**与托管调度共用唯一执行入口**
    ``run_simulation_cycle_for_active``（RebalanceCalculator + ashare_matcher；
    风控买锁/退出规则全量生效；退出规则单**不接受** exclude_symbols 排除与数量改写）。

    防重与只读纪律在调用方（desk 端点：60s Redis 锁 + 仅模拟盘）。
    返回 report_to_hosted_result 形态的执行报告（含 quantity_adjustments 裁定）。
    """
    cfg = _normalize_live_trade_config(live_trade_config)
    return await run_simulation_cycle_for_active(
        tenant_id=tenant_id,
        user_id=user_id,
        strategy_id=strategy_id,
        live_trade_config=cfg,
        run_id=run_id,
        exclude_symbols=exclude_symbols,
        quantity_overrides=quantity_overrides,
        log_source="manual",
    )


def _parse_started_at(value: Any, market: Any = "CN") -> date | None:
    """解析 started_at → 策略市场本地日期（T-P3-07：按市场时区解释 naive 时间）。"""
    text = str(value or "").strip()
    if not text:
        return None
    tz = market_timezone(market)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=tz)
        return parsed.astimezone(tz).date()
    except Exception:
        return None


def _session_index(
    day: date, market: Any = "CN", *, direction: str = "previous"
) -> int | None:
    """日期 → 该市场会话序号；非交易日按 direction 折算（previous=上一个，next=下一个）。"""
    cal_id = market_calendar(market)
    if get_calendar is None or cal_id is None:
        return None
    try:
        calendar = get_calendar(cal_id)
        session = calendar.date_to_session(pd.Timestamp(day), direction=direction)
        return int(calendar.sessions.get_loc(session))
    except Exception:
        return None


def _is_interval_rebalance_day(
    current_day: date,
    *,
    started_day: date | None,
    rebalance_days: int,
    market: Any = "CN",
) -> bool:
    if rebalance_days <= 1:
        return True
    if started_day is None:
        return True

    current_idx = _session_index(current_day, market)
    # 起算日锚到「当天或之后的第一个交易日」：周末/节假日启动 = 下一个交易日第 0 档。
    # 曾用 direction="previous"（回退上一个交易日），周末启动的策略首调仓被推迟
    # N-1 个交易日，管理端「下次窗口」同步错位（2026-09-28 修，
    # 回归测试 tests/test_hosted_interval_anchor.py）。
    started_idx = _session_index(started_day, market, direction="next")
    if current_idx is not None and started_idx is not None:
        return max(0, current_idx - started_idx) % rebalance_days == 0

    anchor = started_day
    while anchor.weekday() >= 5:  # 无日历时以工作日近似交易日，锚点同样只向前滚
        anchor += timedelta(days=1)
    return max(0, (current_day - anchor).days) % rebalance_days == 0


def _is_trading_day(day: date, market: Any = "CN") -> bool:
    cal_id = market_calendar(market)
    if get_calendar is None or cal_id is None:
        return day.weekday() < 5
    try:
        calendar = get_calendar(cal_id)
        return calendar.is_session(pd.Timestamp(day))
    except Exception:
        return day.weekday() < 5


def _is_enabled_session(now_hhmm: str, live_trade_config: dict[str, Any]) -> bool:
    """会话门（T-P3-07）：时段表按**策略市场本地时钟**（shared/market_sessions 唯一事实源）。

    CN 15:05–15:30 盘后（T-P2-07 新规）、美股常规 09:30–16:00、期货夜盘 21:00–02:30 跨午夜，
    全部由时段表 + in_session_hhmm 表达；缺省市场（None）回落 CN 保持存量语义。
    """
    ranges = session_ranges_local(live_trade_config.get("market"))
    enabled = live_trade_config.get("enabled_sessions") or []
    return any(
        in_session_hhmm(now_hhmm, *ranges[s]) for s in enabled if s in ranges
    )


def _matches_trigger_window(
    local_now: datetime,
    target_hhmm: str,
    *,
    window_seconds: int,
) -> bool:
    target_text = str(target_hhmm or "").strip()
    if len(target_text) != 5 or ":" not in target_text:
        return False
    try:
        hour = int(target_text[:2])
        minute = int(target_text[3:5])
    except Exception:
        return False

    target_dt = local_now.replace(
        hour=hour,
        minute=minute,
        second=0,
        microsecond=0,
    )
    delta_seconds = (local_now - target_dt).total_seconds()
    return 0 <= delta_seconds <= max(1, int(window_seconds))


def _resolve_phase(local_now: datetime, live_trade_config: dict[str, Any]) -> str:
    sell_time = str(live_trade_config.get("sell_time") or "14:45")
    buy_time = str(live_trade_config.get("buy_time") or "14:50")
    window_seconds = max(30, _to_int(live_trade_config.get("trigger_window_seconds"), 90))
    sell_hit = _matches_trigger_window(
        local_now,
        sell_time,
        window_seconds=window_seconds,
    )
    buy_hit = _matches_trigger_window(
        local_now,
        buy_time,
        window_seconds=window_seconds,
    )
    if sell_time == buy_time:
        return "ALL" if sell_hit else "IDLE"

    if bool(live_trade_config.get("sell_first", True)):
        if sell_hit:
            return "SELL"
        if buy_hit:
            return "BUY"
    else:
        if buy_hit:
            return "BUY"
        if sell_hit:
            return "SELL"
    return "IDLE"


def _minibt_precompute_due(now: datetime, live_trade_config: dict[str, Any]) -> bool:
    """是否处于「买入窗口前的预计算窗」。

    调度器 30s 一拍：若 14:50:00 才发请求、14:50:30 才执行，实际下单比配置
    晚 30s。提前 ``QM_MINIBT_SIM_PRECOMPUTE_LEAD``（默认 90s）让侧车先把目标
    算好，14:50 第一拍直接执行；目标用 14:49 左右的现价当收盘价，成交价仍是
    执行时的实时价。
    """
    tz = market_timezone(live_trade_config.get("market"))
    local_now = now.astimezone(tz)
    buy_time = str(live_trade_config.get("buy_time") or "14:50")
    try:
        hour = int(buy_time[:2])
        minute = int(buy_time[3:5])
    except (TypeError, ValueError):
        return False
    target = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    delta = (target - local_now).total_seconds()
    lead = max(30, _to_int(os.getenv("QM_MINIBT_SIM_PRECOMPUTE_LEAD", "90"), 90))
    return 0 < delta <= lead


def _should_trigger(
    *,
    now: datetime,
    live_trade_config: dict[str, Any],
    started_day: date | None,
) -> SimulationScheduleDecision:
    market = live_trade_config.get("market")
    local_now = now.astimezone(market_timezone(market))
    now_hhmm = local_now.strftime("%H:%M")
    trade_date = local_now.date().isoformat()

    if not _is_trading_day(local_now.date(), market):
        return SimulationScheduleDecision(False, "IDLE", trade_date, "non_trading_day")

    if not _is_enabled_session(now_hhmm, live_trade_config):
        return SimulationScheduleDecision(False, "IDLE", trade_date, "outside_session")

    schedule_type = str(live_trade_config.get("schedule_type") or "interval").lower()
    if schedule_type == "weekly":
        weekday = local_now.strftime("%a").upper()[:3]
        allowed = set(live_trade_config.get("trade_weekdays") or [])
        if weekday not in allowed:
            return SimulationScheduleDecision(False, "IDLE", trade_date, "weekday_skip")
    else:
        if not _is_interval_rebalance_day(
            local_now.date(),
            started_day=started_day,
            rebalance_days=max(1, _to_int(live_trade_config.get("rebalance_days"), 3)),
            market=market,
        ):
            return SimulationScheduleDecision(
                False, "IDLE", trade_date, "interval_skip"
            )

    phase = _resolve_phase(local_now, live_trade_config)
    if phase == "IDLE":
        return SimulationScheduleDecision(False, phase, trade_date, "before_window")
    return SimulationScheduleDecision(True, phase, trade_date, "matched")


def _is_time_in_enabled_session(
    target_hhmm: str, live_trade_config: dict[str, Any]
) -> bool:
    return _is_enabled_session(str(target_hhmm or "").strip(), live_trade_config)


def _build_candidate_trigger_datetimes(
    *,
    current_day: date,
    live_trade_config: dict[str, Any],
) -> list[tuple[datetime, str]]:
    sell_time = str(live_trade_config.get("sell_time") or "14:45")
    buy_time = str(live_trade_config.get("buy_time") or "14:50")
    candidates: list[tuple[datetime, str]] = []

    tz = market_timezone(live_trade_config.get("market"))

    def _append_candidate(target_hhmm: str, phase: str) -> None:
        if not _is_time_in_enabled_session(target_hhmm, live_trade_config):
            return
        try:
            hour = int(target_hhmm[:2])
            minute = int(target_hhmm[3:5])
        except Exception:
            return
        candidates.append(
            (
                datetime(
                    current_day.year,
                    current_day.month,
                    current_day.day,
                    hour,
                    minute,
                    tzinfo=tz,
                ),
                phase,
            )
        )

    if sell_time == buy_time:
        _append_candidate(sell_time, "ALL")
    else:
        if bool(live_trade_config.get("sell_first", True)):
            _append_candidate(sell_time, "SELL")
            _append_candidate(buy_time, "BUY")
        else:
            _append_candidate(buy_time, "BUY")
            _append_candidate(sell_time, "SELL")
    candidates.sort(key=lambda item: item[0])
    return candidates


def _next_scheduled_trigger(
    *,
    now: datetime,
    live_trade_config: dict[str, Any],
    started_day: date | None,
    horizon_days: int = 30,
) -> SimulationNextTrigger | None:
    local_now = now.astimezone(market_timezone(live_trade_config.get("market")))
    normalized_config = _normalize_live_trade_config(live_trade_config)
    window_seconds = max(
        30, _to_int(normalized_config.get("trigger_window_seconds"), 90)
    )

    for offset in range(max(1, int(horizon_days or 30)) + 1):
        candidate_day = local_now.date() + timedelta(days=offset)
        if not _is_trading_day(candidate_day):
            continue

        for target_at, phase in _build_candidate_trigger_datetimes(
            current_day=candidate_day,
            live_trade_config=normalized_config,
        ):
            if target_at < local_now:
                continue
            probe = _should_trigger(
                now=target_at,
                live_trade_config=normalized_config,
                started_day=started_day,
            )
            if not probe.should_trigger:
                continue
            window_end_at = target_at + timedelta(seconds=window_seconds)
            return SimulationNextTrigger(
                phase=phase,
                trade_date=candidate_day.isoformat(),
                target_at=target_at,
                window_start_at=target_at,
                window_end_at=window_end_at,
                reason="future_window",
            )
    return None


def _task_id(
    *,
    tenant_id: str,
    user_id: str,
    strategy_id: str,
    trade_date: str,
    phase: str,
) -> str:
    source = json.dumps(
        {
            "tenant_id": tenant_id,
            "user_id": user_id,
            "strategy_id": strategy_id,
            "trade_date": trade_date,
            "phase": phase,
            "mode": "SIMULATION",
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"hosted_sim_{hashlib.sha1(source.encode('utf-8')).hexdigest()[:16]}"


def _lock_key(
    *,
    tenant_id: str,
    user_id: str,
    strategy_id: str,
    trade_date: str,
    phase: str,
) -> str:
    return (
        f"qm:hosted:simulation:{tenant_id}:{user_id}:{strategy_id}:{trade_date}:{phase}"
    )


class SimulationHostedScheduler:
    def __init__(self, redis: RedisClient, interval_seconds: int = 30):
        self.redis = redis
        self.interval_seconds = max(5, int(interval_seconds or 30))
        self._stopped = asyncio.Event()
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stopped.clear()
        self._task = asyncio.create_task(
            self._run(), name="simulation-hosted-scheduler"
        )

    async def stop(self) -> None:
        self._stopped.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        logger.info(
            "simulation hosted scheduler started, interval=%ss", self.interval_seconds
        )
        while not self._stopped.is_set():
            # T-RC-14：心跳必须在每轮**开始**写（而非结束），否则本轮卡死时
            # 心跳反而"新鲜"，正是要检出的停摆形态。redis_client 不传——heartbeat
            # 默认走 db0，与体检 C07 读取口径一致（传 trade 客户端会写到 db2 读不到）。
            try:
                from backend.shared.scheduler_registry import (
                    heartbeat as _sched_heartbeat,
                )

                _sched_heartbeat("sim_hosted")
            except Exception as exc:  # noqa: BLE001 - 心跳失败不得影响调度
                logger.debug("sim_hosted heartbeat failed: %s", exc)
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(
                    "simulation hosted scheduler loop failed: %s", exc, exc_info=True
                )

            try:
                await asyncio.wait_for(
                    self._stopped.wait(), timeout=self.interval_seconds
                )
            except asyncio.TimeoutError:
                continue

    async def run_once(self, *, now: datetime | None = None) -> int:
        if not self.redis.client:
            return 0

        triggered = 0
        current = now or datetime.now(_SH_TZ)
        await SimulationRebalanceJobService.expire_outdated_jobs(
            now=_job_naive_shanghai(current)
        )
        for raw_key in self.redis.client.scan_iter(
            match="trade:active_strategy:*", count=500
        ):
            try:
                did_trigger = await self._process_key(str(raw_key), now=current)
                if did_trigger:
                    triggered += 1
            except Exception as exc:
                logger.warning(
                    "simulation hosted scheduler skipped key=%s error=%s",
                    raw_key,
                    exc,
                    exc_info=True,
                )
        return triggered

    def _resolve_minibt_target(
        self,
        *,
        tenant_id: str,
        user_id: str,
        strategy_id: str,
        run_id: str,
        trade_date: str,
        phase: str,
        now: datetime,
    ) -> tuple[dict[str, Any] | None, str]:
        """读 minibt 目标；不可用时向侧车发一次计算请求并给出显式跳过原因。

        「请求 → 侧车计算 → 下一 tick 取目标」两拍完成：调度器 30s 一轮、
        触发窗口 90s，正常路径下一拍即可执行；超时/失败按原因码跳过，
        **绝不回落到模型信号**（同一账户被两套信号驱动是最坏的失败方式）。
        """
        from backend.shared.minibt_sim_keys import (
            attempts_key,
            build_request_payload,
            request_key,
            target_key,
            target_state,
        )

        client = self.redis.client
        max_age = _to_int(os.getenv("QM_MINIBT_SIM_TARGET_MAX_AGE", "180"), 180)
        raw = None
        try:
            raw = client.get(target_key(tenant_id, user_id, strategy_id))
        except Exception as exc:  # noqa: BLE001 - 读失败按不可用处理
            logger.warning("minibt target read failed: %s", exc)
        payload = None
        if raw:
            try:
                payload = json.loads(raw)
            except Exception:  # noqa: BLE001
                payload = None
        state, reason = target_state(
            payload,
            trade_date=trade_date,
            now_ts=now.timestamp(),
            max_age_seconds=float(max_age),
        )
        if state == "ok":
            return payload, "ok"

        logger.info(
            "minibt target state=%s reason=%s tenant=%s user=%s strategy=%s",
            state,
            reason,
            tenant_id,
            user_id,
            strategy_id,
        )
        if state == "error" and isinstance(payload, dict):
            try:
                hosted_sim_log(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    strategy_id=strategy_id,
                    line=f"minibt 目标计算失败：{payload.get('error') or 'unknown'}",
                    level="warning",
                    stage="minibt_target",
                    status="failed",
                    run_id=str(payload.get("request_id") or ""),
                    phase=phase,
                )
            except Exception:  # noqa: BLE001 - 日志失败不得影响调度
                pass
        attempts_key_value = attempts_key(tenant_id, user_id, strategy_id, trade_date)
        try:
            attempts = int(client.incr(attempts_key_value))
            client.expire(attempts_key_value, 24 * 3600)
        except Exception:  # noqa: BLE001 - Redis 异常=不可用，不假装已请求
            return None, "minibt_target_unavailable"
        if attempts > 3:
            return None, "minibt_target_unavailable"
        request = build_request_payload(
            tenant_id=tenant_id,
            user_id=user_id,
            strategy_id=strategy_id,
            run_id=run_id,
            trade_date=trade_date,
            phase=phase,
            request_id=str(uuid.uuid4()),
        )
        try:
            client.set(
                request_key(tenant_id, user_id, strategy_id),
                json.dumps(request, ensure_ascii=False),
                ex=120,
                nx=True,
            )
        except Exception:  # noqa: BLE001
            return None, "minibt_target_unavailable"
        if state == "error":
            return None, "minibt_target_error"
        if state == "stale":
            return None, "minibt_target_stale"
        return None, "minibt_target_pending"

    async def _process_key(self, key: str, *, now: datetime) -> bool:
        raw = self.redis.client.get(key)
        if not raw:
            return False
        try:
            active_data = json.loads(raw)
        except Exception:
            return False
        if not isinstance(active_data, dict):
            return False
        if str(active_data.get("mode") or "").upper() != "SIMULATION":
            return False

        parts = key.split(":")
        if len(parts) < 4:
            return False
        from backend.shared.simulation_account_keys import resolve_active_identity

        tenant_id, user_id = resolve_active_identity(
            tenant_suffix=parts[-2], user_suffix=parts[-1], payload=active_data
        )
        strategy_id = str(
            active_data.get("runtime_strategy_id")
            or active_data.get("strategy_id")
            or ""
        ).strip()
        if not user_id or not strategy_id:
            return False

        live_trade_config = _normalize_live_trade_config(
            active_data.get("live_trade_config")
        )
        runtime = str(active_data.get("runtime") or "sandbox").strip().lower()
        if runtime == "minibt":
            # minibt 的信号由策略在每根 K 线上给出，按日评估；rebalance_days
            # 是模型组合的调仓节奏，套到信号策略上会把信号最多推迟 N-1 天。
            if int(live_trade_config.get("rebalance_days") or 1) != 1:
                logger.info(
                    "simulation hosted: minibt runtime evaluates daily, "
                    "rebalance_days=%s ignored tenant=%s user=%s strategy=%s",
                    live_trade_config.get("rebalance_days"),
                    tenant_id,
                    user_id,
                    strategy_id,
                )
            live_trade_config = {**live_trade_config, "rebalance_days": 1}
        started_day = _parse_started_at(
            active_data.get("started_at"), market=live_trade_config.get("market")
        )
        decision = _should_trigger(
            now=now,
            live_trade_config=live_trade_config,
            started_day=started_day,
        )
        if not decision.should_trigger:
            if (
                runtime == "minibt"
                and decision.reason == "before_window"
                and _minibt_precompute_due(now, live_trade_config)
            ):
                # 预计算：只写请求，不执行；14:50 第一拍拿到目标直接下单。
                self._resolve_minibt_target(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    strategy_id=strategy_id,
                    run_id=str(active_data.get("run_id") or ""),
                    trade_date=decision.trade_date,
                    phase="BUY",
                    now=now,
                )
                return False
            hosted_sim_skip_once(
                tenant_id=tenant_id,
                user_id=user_id,
                reason=decision.reason,
                strategy_id=strategy_id,
            )
            return False
        if not hosted_cycle_ready(decision.phase):
            logger.info(
                "simulation hosted skip SELL-only window tenant=%s user=%s strategy=%s "
                "(engine runs sell+buy atomically on BUY/ALL)",
                tenant_id,
                user_id,
                strategy_id,
            )
            hosted_sim_skip_once(
                tenant_id=tenant_id,
                user_id=user_id,
                reason="sell_only_window",
                strategy_id=strategy_id,
            )
            return False
        minibt_target: dict[str, Any] | None = None
        if runtime == "minibt":
            minibt_target, target_reason = self._resolve_minibt_target(
                tenant_id=tenant_id,
                user_id=user_id,
                strategy_id=strategy_id,
                run_id=str(active_data.get("run_id") or ""),
                trade_date=decision.trade_date,
                phase=decision.phase,
                now=now,
            )
            if minibt_target is None:
                hosted_sim_skip_once(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    reason=target_reason,
                    strategy_id=strategy_id,
                )
                return False
            hosted_sim_log(
                tenant_id=tenant_id,
                user_id=user_id,
                strategy_id=strategy_id,
                line=(
                    f"minibt 目标 {minibt_target.get('symbol')} "
                    f"{minibt_target.get('target_shares')} 股"
                    f"（bar={minibt_target.get('bar_date')} "
                    f"{minibt_target.get('bar_source')}）"
                ),
                level="info",
                stage="minibt_target",
                status="ready",
                run_id=str(minibt_target.get("request_id") or ""),
                phase=decision.phase,
                summary={
                    "symbol": minibt_target.get("symbol"),
                    "target_shares": minibt_target.get("target_shares"),
                    "bar_source": minibt_target.get("bar_source"),
                    "compute_seconds": minibt_target.get("compute_seconds"),
                },
            )

        # 真的进入执行态：复位跳过标记，下一段静默期（如收盘后）能重新记录原因
        hosted_sim_clear_skip_marker(tenant_id=tenant_id, user_id=user_id)

        lock_key = _lock_key(
            tenant_id=tenant_id,
            user_id=user_id,
            strategy_id=strategy_id,
            trade_date=decision.trade_date,
            phase=decision.phase,
        )
        task_id = _task_id(
            tenant_id=tenant_id,
            user_id=user_id,
            strategy_id=strategy_id,
            trade_date=decision.trade_date,
            phase=decision.phase,
        )
        await SimulationRebalanceJobService.ensure_job(
            job_id=task_id,
            tenant_id=tenant_id,
            user_id=user_id,
            strategy_id=strategy_id,
            schedule_type=str(live_trade_config.get("schedule_type") or "interval"),
            planned_run_at=_job_naive_shanghai(now),
            window_seconds=max(
                30, _to_int(live_trade_config.get("trigger_window_seconds"), 90)
            ),
            idempotency_key=lock_key,
        )
        await SimulationRebalanceJobService.mark_ready(task_id)
        try:
            if not self.redis.client.set(lock_key, task_id, ex=36 * 3600, nx=True):
                await SimulationRebalanceJobService.mark_skipped(
                    task_id,
                    last_error="idempotency lock already exists for this execution window",
                )
                hosted_sim_skip_once(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    reason="lock_held",
                    strategy_id=strategy_id,
                )
                return False
        except Exception:
            logger.warning("failed to write simulation hosted lock: %s", lock_key)
            await SimulationRebalanceJobService.mark_skipped(
                task_id,
                last_error="failed to acquire distributed execution lock",
            )
            hosted_sim_skip_once(
                tenant_id=tenant_id,
                user_id=user_id,
                reason="lock_error",
                strategy_id=strategy_id,
            )
            return False
        hosted_sim_log(
            tenant_id=tenant_id,
            user_id=user_id,
            strategy_id=strategy_id,
            line=(
                f"托管周期开轮 phase={decision.phase} 交易日={decision.trade_date} "
                f"任务={task_id} 调仓节奏={live_trade_config.get('rebalance_days')}日"
            ),
            level="info",
            stage="cycle_start",
            status="running",
            run_id=task_id,
            phase=decision.phase,
        )

        try:
            await SimulationRebalanceJobService.mark_started(task_id)
            result = await run_simulation_cycle_for_active(
                tenant_id=tenant_id,
                user_id=user_id,
                strategy_id=strategy_id,
                live_trade_config=live_trade_config,
                run_id=task_id,
                minibt_target=minibt_target,
            )
            if result.get("status") == "failed" and result.get("error"):
                err = str(result["error"])
                if err not in {"无可用信号", "账户不存在"}:
                    raise RuntimeError(err)
            logger.info(
                "simulation hosted cycle finished: tenant=%s user=%s strategy=%s phase=%s task=%s status=%s filled=%s",
                tenant_id,
                user_id,
                strategy_id,
                decision.phase,
                task_id,
                result.get("status"),
                result.get("filled_count"),
            )
            await SimulationRebalanceJobService.mark_finished(
                task_id,
                status="succeeded",
            )
            hosted_sim_log(
                tenant_id=tenant_id,
                user_id=user_id,
                strategy_id=strategy_id,
                line=(
                    f"托管周期结束 status={result.get('status')} "
                    f"信号={result.get('signal_count', 0)} 计划={result.get('order_count', 0)} "
                    f"成交={result.get('filled_count', 0)} 拒绝={result.get('rejected_count', 0)}"
                ),
                level="info",
                stage="cycle_end",
                status=str(result.get("status") or "succeeded"),
                run_id=task_id,
                phase=decision.phase,
                summary={
                    "signal_count": result.get("signal_count", 0),
                    "order_count": result.get("order_count", 0),
                    "filled_count": result.get("filled_count", 0),
                    "rejected_count": result.get("rejected_count", 0),
                },
            )
            return True
        except Exception as exc:
            await SimulationRebalanceJobService.mark_finished(
                task_id,
                status="failed",
                last_error=str(exc),
            )
            hosted_sim_log(
                tenant_id=tenant_id,
                user_id=user_id,
                strategy_id=strategy_id,
                line=f"托管周期失败：{exc}",
                level="error",
                stage="cycle_error",
                status="failed",
                run_id=task_id,
                phase=decision.phase,
                summary={"error": str(exc)},
            )
            try:
                self.redis.client.delete(lock_key)
            except Exception:
                pass
            raise
