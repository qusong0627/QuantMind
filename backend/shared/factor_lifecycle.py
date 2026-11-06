"""Owner-neutral factor maturity derivation and blind-window redaction.

The caller supplies owner-scoped factor and run evidence. This module performs
no I/O and keeps maturity derived from persisted facts rather than a second
mutable status column.
"""

from __future__ import annotations

import calendar
import math
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from typing import Any

from backend.shared.factor_tags import read_metadata
from backend.shared.utc_datetime import as_utc, to_utc_iso

STATE_EXPERIMENT = "experiment"
STATE_OBSERVING = "observing"
STATE_ACTIVE = "active"
STATE_DOWNWEIGHTED = "downweighted"
STATES = frozenset(
    {STATE_EXPERIMENT, STATE_OBSERVING, STATE_ACTIVE, STATE_DOWNWEIGHTED}
)
MATURITY_UNKNOWN = "unknown"
MATURITY_VALIDATING = "验证中"
MATURITY_LABELS = {
    STATE_EXPERIMENT: "实验",
    STATE_OBSERVING: "观察",
    STATE_ACTIVE: "在用",
    STATE_DOWNWEIGHTED: "退役",
}
BLIND_DAYS = 180
ICIR_FLOOR = 0.2
STREAK_MONTHS = 3
WINDOW_DAYS = 365
MIN_N_DAYS = 120
STALE_DAYS = 60
MAX_LIFECYCLE_POINTS = 360
READING_OK = "ok"
READING_TOO_FEW = "too_few_days"
READING_SERIES_STALE = "series_stale"
READING_FLAT = "flat_series"
READING_NO_SERIES = "no_series"
DECISION_HOLD = "hold"
DECISION_OBSERVING_PROTECTED = "observing_protected"
DECISION_DOWNWEIGHT = "downweight"
_BLIND_KEYS = frozenset({"last_eval", "evidence"})


@dataclass(frozen=True)
class MonthReading:
    """One rolling 12-month ICIR reading at a natural month end."""

    month_end: str
    n_days: int
    icir: float | None
    status: str
    below_floor: bool | None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _as_date(value: date | str | None) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and len(value) >= 10:
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def read_month(
    series: Mapping[str, Any] | None,
    *,
    month_end: date | str,
    window_days: int = WINDOW_DAYS,
    min_n_days: int = MIN_N_DAYS,
    stale_days: int = STALE_DAYS,
    icir_floor: float = ICIR_FLOOR,
) -> MonthReading:
    """Compute a conservative rolling ICIR; missing or stale data is never weak."""
    end = _as_date(month_end)
    end_text = end.isoformat() if end else str(month_end)
    dates = list((series or {}).get("dates") or [])
    ics = list((series or {}).get("ic") or [])
    if not dates or len(dates) != len(ics) or len(dates) > MAX_LIFECYCLE_POINTS:
        return MonthReading(end_text, 0, None, READING_NO_SERIES, None)
    if end is None:
        return MonthReading(end_text, 0, None, READING_NO_SERIES, None)

    pairs: list[tuple[date, float]] = []
    for raw_day, raw_value in zip(dates, ics, strict=True):
        if raw_value is None:
            continue
        day = _as_date(raw_day if isinstance(raw_day, (str, date)) else None)
        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            continue
        if day is not None and math.isfinite(value):
            pairs.append((day, value))
    if not pairs:
        return MonthReading(end_text, 0, None, READING_NO_SERIES, None)

    start = end - timedelta(days=window_days)
    values = [value for day, value in pairs if start < day <= end]
    latest_day = max((day for day, _ in pairs if day <= end), default=None)
    if latest_day is None or (end - latest_day).days > stale_days:
        return MonthReading(end_text, len(values), None, READING_SERIES_STALE, None)
    if len(values) < min_n_days:
        return MonthReading(end_text, len(values), None, READING_TOO_FEW, None)
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / (len(values) - 1)
    std = math.sqrt(variance)
    if std == 0.0:
        return MonthReading(end_text, len(values), None, READING_FLAT, None)
    icir = mean / std
    return MonthReading(end_text, len(values), icir, READING_OK, icir < icir_floor)


def streak_of(readings: list[MonthReading]) -> int:
    """Count consecutive valid weak readings backwards from the newest month."""
    streak = 0
    for reading in reversed(readings):
        if reading.status == READING_OK and reading.below_floor:
            streak += 1
        else:
            break
    return streak


def verdict_of(
    readings: list[MonthReading], *, blind_until: datetime | None
) -> dict[str, Any]:
    """Require three consecutive weak months and respect the blind-window day."""
    streak = streak_of(readings)
    latest = readings[-1] if readings else None
    if streak < STREAK_MONTHS:
        decision = DECISION_HOLD
    else:
        latest_day = _as_date(latest.month_end) if latest else None
        protected = (
            blind_until is not None
            and latest_day is not None
            and latest_day <= as_utc(blind_until).date()
        )
        decision = DECISION_OBSERVING_PROTECTED if protected else DECISION_DOWNWEIGHT
    return {
        "streak": streak,
        "decision": decision,
        "latest": latest.as_dict() if latest else None,
    }


def month_ends_back(asof: date, count: int = 4) -> list[date]:
    """Return the most recent completed natural month ends in ascending order."""
    if count <= 0:
        return []
    year, month = asof.year, asof.month
    last = date(year, month, calendar.monthrange(year, month)[1])
    if last > asof:
        month -= 1
        if month == 0:
            year, month = year - 1, 12
        last = date(year, month, calendar.monthrange(year, month)[1])
    ends: list[date] = []
    for _ in range(min(count, 24)):
        ends.append(last)
        month -= 1
        if month == 0:
            year, month = year - 1, 12
        last = date(year, month, calendar.monthrange(year, month)[1])
    return ends[::-1]


def blind_until_of(
    *,
    created_at: datetime | None,
    first_completed_run_at: datetime | None,
) -> datetime | None:
    anchor = first_completed_run_at or created_at
    return as_utc(anchor) + timedelta(days=BLIND_DAYS) if anchor else None


def evaluate_readings(
    series: Mapping[str, Any] | None,
    *,
    blind_until: datetime | None,
    asof: date,
) -> tuple[list[MonthReading], dict[str, Any]]:
    """Evaluate a fixed daily IC snapshot at the last four complete month ends."""
    readings = [
        read_month(series, month_end=month_end) for month_end in month_ends_back(asof)
    ]
    return readings, verdict_of(readings, blind_until=blind_until)


def evaluate_lifecycle(
    row: Mapping[str, Any],
    *,
    series: Mapping[str, Any] | None,
    first_completed_run_at: datetime | None,
    now: datetime,
) -> dict[str, Any]:
    """Evaluate one factor without I/O; caller must supply owner-scoped evidence."""
    metadata = read_metadata(row)
    metadata_lifecycle = metadata.get("lifecycle")
    lifecycle = metadata_lifecycle if isinstance(metadata_lifecycle, Mapping) else {}
    blind_until = blind_until_of(
        created_at=(
            row.get("created_at")
            if isinstance(row.get("created_at"), datetime)
            else None
        ),
        first_completed_run_at=first_completed_run_at,
    )
    has_completed = (
        first_completed_run_at is not None or row.get("status") == "completed"
    )
    state_before = resolve_state(
        explicit_state=(
            str(lifecycle.get("state")) if lifecycle.get("state") is not None else None
        ),
        has_completed_run=has_completed,
        blind_until=blind_until,
        now=now,
    )
    readings, verdict = evaluate_readings(
        series,
        blind_until=blind_until,
        asof=now.date(),
    )
    if series is None:
        eval_status = "no_lifecycle_series"
    elif readings:
        eval_status = readings[-1].status
    else:
        eval_status = READING_NO_SERIES
    if verdict["decision"] == DECISION_DOWNWEIGHT:
        action = "already" if state_before == STATE_DOWNWEIGHTED else "downweight"
    else:
        action = "none"
    state_after = STATE_DOWNWEIGHTED if action == "downweight" else state_before
    return {
        "factor_id": str(row.get("factor_id") or ""),
        "state_before": state_before,
        "state_after": state_after,
        "blind_until": to_utc_iso(blind_until) if blind_until else None,
        "eval_status": eval_status,
        "readings": [reading.as_dict() for reading in readings],
        "verdict": verdict,
        "action": action,
    }


def lifecycle_write_payload(
    evaluation: Mapping[str, Any],
    *,
    existing_lifecycle: Mapping[str, Any] | None,
    now: datetime,
    apply_downweight: bool = True,
) -> dict[str, Any]:
    """Merge an evaluation into an owner's lifecycle metadata snapshot."""
    lifecycle = dict(existing_lifecycle or {})
    verdict = evaluation.get("verdict") or {}
    latest = verdict.get("latest") or {}
    if evaluation.get("eval_status") != "no_run":
        lifecycle["last_eval"] = {
            "month_end": latest.get("month_end"),
            "n_days": latest.get("n_days"),
            "icir": latest.get("icir"),
            "status": str(evaluation.get("eval_status") or ""),
            "streak": int(verdict.get("streak") or 0),
            "decision": verdict.get("decision"),
            "evaluated_at": to_utc_iso(now),
        }
    if apply_downweight and evaluation.get("action") == "downweight":
        lifecycle.update(
            {
                "state": STATE_DOWNWEIGHTED,
                "since": to_utc_iso(now),
                "reason": "rolling_12m_icir_below_floor_3_months",
                "evidence": {
                    "month_end": latest.get("month_end"),
                    "n_days": latest.get("n_days"),
                    "icir": latest.get("icir"),
                    "streak": int(verdict.get("streak") or 0),
                },
                "blind_until": evaluation.get("blind_until"),
            }
        )
    return lifecycle


def resolve_state(
    *,
    explicit_state: str | None,
    has_completed_run: bool,
    blind_until: datetime | None,
    now: datetime,
) -> str:
    """Derive experiment/observing/active unless a valid explicit override exists."""
    if explicit_state in STATES:
        return explicit_state
    if not has_completed_run:
        return STATE_EXPERIMENT
    if blind_until is not None and as_utc(now) < as_utc(blind_until):
        return STATE_OBSERVING
    return STATE_ACTIVE


def lifecycle_view(
    row: Mapping[str, Any],
    *,
    first_completed_run_at: datetime | None,
    now: datetime,
    has_active_validation: bool = False,
) -> dict[str, Any]:
    """Produce the safe maturity view from one owner's factor and run evidence."""
    metadata = read_metadata(row)
    raw_lifecycle = metadata.get("lifecycle")
    lifecycle = raw_lifecycle if isinstance(raw_lifecycle, Mapping) else {}
    created_at = row.get("created_at")
    anchor = first_completed_run_at or (
        created_at if isinstance(created_at, datetime) else None
    )
    blind_until = as_utc(anchor) + timedelta(days=BLIND_DAYS) if anchor else None
    has_completed = (
        first_completed_run_at is not None or row.get("status") == "completed"
    )
    state = resolve_state(
        explicit_state=lifecycle.get("state"),
        has_completed_run=has_completed,
        blind_until=blind_until,
        now=now,
    )
    last_eval = lifecycle.get("last_eval")
    if state == STATE_OBSERVING or not isinstance(last_eval, Mapping):
        last_eval = None
    maturity = (
        MATURITY_VALIDATING
        if state == STATE_EXPERIMENT and has_active_validation
        else MATURITY_LABELS.get(state, MATURITY_UNKNOWN)
    )
    return {
        "state": state,
        "maturity": maturity,
        "blind_until": to_utc_iso(blind_until) if blind_until else None,
        "last_eval": dict(last_eval) if last_eval is not None else None,
    }


def redact_blind_readings(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    """Remove lifecycle readings from the public metadata copy during blind mode."""
    output = dict(metadata) if isinstance(metadata, Mapping) else {}
    lifecycle = output.get("lifecycle")
    if isinstance(lifecycle, Mapping):
        output["lifecycle"] = {
            key: value for key, value in lifecycle.items() if key not in _BLIND_KEYS
        }
    return output
