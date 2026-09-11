from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
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
from backend.services.live_trading.services.manual_execution_service import (
    manual_execution_service,
)
from backend.services.simulation.services.rebalance_job_service import (
    SimulationRebalanceJobService,
)

logger = logging.getLogger(__name__)

_SH_TZ = ZoneInfo("Asia/Shanghai")
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
    merged["max_orders_per_cycle"] = max(
        1, _to_int(merged.get("max_orders_per_cycle"), 20)
    )
    merged["trigger_window_seconds"] = max(
        30, _to_int(merged.get("trigger_window_seconds"), 90)
    )
    return merged


def _parse_started_at(value: Any) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=_SH_TZ)
        return parsed.astimezone(_SH_TZ).date()
    except Exception:
        return None


def _session_index(day: date) -> int | None:
    if get_calendar is None:
        return None
    try:
        calendar = get_calendar("XSHG")
        session = calendar.date_to_session(pd.Timestamp(day), direction="previous")
        return int(calendar.sessions.get_loc(session))
    except Exception:
        return None


def _is_interval_rebalance_day(
    current_day: date,
    *,
    started_day: date | None,
    rebalance_days: int,
) -> bool:
    if rebalance_days <= 1:
        return True
    if started_day is None:
        return True

    current_idx = _session_index(current_day)
    started_idx = _session_index(started_day)
    if current_idx is not None and started_idx is not None:
        return max(0, current_idx - started_idx) % rebalance_days == 0

    return max(0, (current_day - started_day).days) % rebalance_days == 0


def _is_trading_day(day: date) -> bool:
    if get_calendar is None:
        return day.weekday() < 5
    try:
        calendar = get_calendar("XSHG")
        return calendar.is_session(pd.Timestamp(day))
    except Exception:
        return day.weekday() < 5


def _is_enabled_session(now_hhmm: str, live_trade_config: dict[str, Any]) -> bool:
    enabled = set(live_trade_config.get("enabled_sessions") or [])
    if "AM" in enabled and "09:30" <= now_hhmm <= "11:30":
        return True
    if "PM" in enabled and "13:00" <= now_hhmm <= "15:00":
        return True
    return False


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


def _should_trigger(
    *,
    now: datetime,
    live_trade_config: dict[str, Any],
    started_day: date | None,
) -> SimulationScheduleDecision:
    local_now = now.astimezone(_SH_TZ)
    now_hhmm = local_now.strftime("%H:%M")
    trade_date = local_now.date().isoformat()

    if not _is_trading_day(local_now.date()):
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
        ):
            return SimulationScheduleDecision(
                False, "IDLE", trade_date, "interval_skip"
            )

    phase = _resolve_phase(local_now, live_trade_config)
    if phase == "IDLE":
        return SimulationScheduleDecision(False, phase, trade_date, "before_window")
    return SimulationScheduleDecision(True, phase, trade_date, "matched")


def _is_time_in_enabled_session(target_hhmm: str, live_trade_config: dict[str, Any]) -> bool:
    return _is_enabled_session(str(target_hhmm or "").strip(), live_trade_config)


def _build_candidate_trigger_datetimes(
    *,
    current_day: date,
    live_trade_config: dict[str, Any],
) -> list[tuple[datetime, str]]:
    sell_time = str(live_trade_config.get("sell_time") or "14:45")
    buy_time = str(live_trade_config.get("buy_time") or "14:50")
    candidates: list[tuple[datetime, str]] = []

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
                    tzinfo=_SH_TZ,
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
    local_now = now.astimezone(_SH_TZ)
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
            now=current.astimezone(_SH_TZ).replace(microsecond=0, tzinfo=None)
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
        strategy_id = str(active_data.get("strategy_id") or "").strip()
        if not user_id or not strategy_id:
            return False

        live_trade_config = _normalize_live_trade_config(
            active_data.get("live_trade_config")
        )
        execution_config = (
            dict(active_data.get("execution_config"))
            if isinstance(active_data.get("execution_config"), dict)
            else {}
        )
        execution_config["trading_mode"] = "SIMULATION"
        started_day = _parse_started_at(active_data.get("started_at"))
        decision = _should_trigger(
            now=now,
            live_trade_config=live_trade_config,
            started_day=started_day,
        )
        if not decision.should_trigger:
            return False

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
            planned_run_at=now.astimezone(_SH_TZ).replace(microsecond=0),
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
                return False
        except Exception:
            logger.warning("failed to write simulation hosted lock: %s", lock_key)
            await SimulationRebalanceJobService.mark_skipped(
                task_id,
                last_error="failed to acquire distributed execution lock",
            )
            return False

        try:
            await SimulationRebalanceJobService.mark_started(task_id)
            result = await manual_execution_service.create_hosted_task(
                tenant_id=tenant_id,
                user_id=user_id,
                strategy_id=strategy_id,
                trading_mode="SIMULATION",
                execution_config=execution_config,
                live_trade_config=live_trade_config,
                trigger_context={
                    "source": "simulation_hosted_scheduler",
                    "schedule_type": live_trade_config.get("schedule_type"),
                    "phase": decision.phase,
                    "triggered_at": now.astimezone(_SH_TZ).isoformat(),
                    "runner_trade_date": decision.trade_date,
                    "runner_mode": "SIMULATION",
                    "started_at": active_data.get("started_at"),
                },
                parent_runtime_id=str(active_data.get("run_id") or "").strip() or None,
                note="auto schedule from simulation hosted scheduler",
                task_id=task_id,
            )
            logger.info(
                "simulation hosted task scheduled: tenant=%s user=%s strategy=%s phase=%s task=%s status=%s",
                tenant_id,
                user_id,
                strategy_id,
                decision.phase,
                task_id,
                result.get("status") if isinstance(result, dict) else None,
            )
            await SimulationRebalanceJobService.mark_finished(
                task_id,
                status="succeeded",
            )
            return True
        except Exception as exc:
            await SimulationRebalanceJobService.mark_finished(
                task_id,
                status="failed",
                last_error=str(exc),
            )
            try:
                self.redis.client.delete(lock_key)
            except Exception:
                pass
            raise
