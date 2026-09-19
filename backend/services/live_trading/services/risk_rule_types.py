"""Risk rule type registry and parameter validation.

Gate rules are checked at order submission. Trigger rules are consumed by
the independent risk scanner and can flatten positions without waiting for
a strategy rebalance window.
"""

from __future__ import annotations

from typing import Any

from backend.shared.benchmark import BENCHMARK_SYMBOL

GATE_RULE_TYPES = frozenset(
    {
        "max_order_size",
        "min_order_size",
        "max_position_size",
        "max_daily_trades",
    }
)

TRIGGER_RULE_TYPES = frozenset(
    {
        "position_stop_loss",
        "position_take_profit",
        "market_index_move",
    }
)

KNOWN_RULE_TYPES = GATE_RULE_TYPES | TRIGGER_RULE_TYPES

TRADING_MODES = frozenset({"SIMULATION", "REAL", "BOTH"})
DEFAULT_INDEX = BENCHMARK_SYMBOL
DEFAULT_MARKETS = ("CN",)


class RiskRuleValidationError(ValueError):
    """Raised when a risk rule payload is invalid."""


def _as_float(value: Any, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise RiskRuleValidationError(f"{field} must be a number") from exc
    if number != number:  # NaN
        raise RiskRuleValidationError(f"{field} must be a finite number")
    return number


def _as_markets(value: Any) -> list[str]:
    if value is None:
        return list(DEFAULT_MARKETS)
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        raise RiskRuleValidationError("markets must be a list of market codes")
    markets = [str(item).strip().upper() for item in items if str(item).strip()]
    return markets or list(DEFAULT_MARKETS)


def _as_trading_mode(value: Any) -> str:
    mode = str(value or "SIMULATION").strip().upper()
    if mode not in TRADING_MODES:
        raise RiskRuleValidationError(
            f"trading_mode must be one of {sorted(TRADING_MODES)}"
        )
    return mode


def validate_rule_parameters(
    rule_type: str, parameters: dict[str, Any] | None
) -> dict[str, Any]:
    """Validate and normalize rule parameters. Unknown extra keys are kept."""
    rule_type = str(rule_type or "").strip()
    if rule_type not in KNOWN_RULE_TYPES:
        raise RiskRuleValidationError(
            f"unsupported rule_type: {rule_type}. "
            f"known={sorted(KNOWN_RULE_TYPES)}"
        )

    params = dict(parameters or {})
    params["markets"] = _as_markets(params.get("markets"))
    params["trading_mode"] = _as_trading_mode(params.get("trading_mode"))

    cooldown = params.get("cooldown_seconds")
    if cooldown is not None:
        seconds = int(_as_float(cooldown, "cooldown_seconds"))
        if seconds < 0:
            raise RiskRuleValidationError("cooldown_seconds must be >= 0")
        params["cooldown_seconds"] = seconds

    if rule_type == "position_stop_loss":
        pct = _as_float(params.get("pct"), "pct")
        if pct >= 0:
            raise RiskRuleValidationError("position_stop_loss pct must be negative")
        if pct < -0.5:
            raise RiskRuleValidationError("position_stop_loss pct cannot be below -50%")
        params["pct"] = pct
    elif rule_type == "position_take_profit":
        pct = _as_float(params.get("pct"), "pct")
        if pct <= 0:
            raise RiskRuleValidationError("position_take_profit pct must be positive")
        if pct > 5:
            raise RiskRuleValidationError("position_take_profit pct cannot exceed 500%")
        params["pct"] = pct
    elif rule_type == "market_index_move":
        pct = _as_float(params.get("pct"), "pct")
        if pct == 0:
            raise RiskRuleValidationError("market_index_move pct cannot be 0")
        if abs(pct) > 0.2:
            raise RiskRuleValidationError("market_index_move pct must be within ±20%")
        params["pct"] = pct
        index = str(params.get("index") or DEFAULT_INDEX).strip().upper()
        if not index:
            raise RiskRuleValidationError("market_index_move index is required")
        params["index"] = index
    elif rule_type == "max_order_size":
        if "max_value" in params:
            params["max_value"] = _as_float(params.get("max_value"), "max_value")
    elif rule_type == "min_order_size":
        if "min_value" in params:
            params["min_value"] = _as_float(params.get("min_value"), "min_value")
    elif rule_type == "max_position_size":
        if "max_percentage" in params:
            pct = _as_float(params.get("max_percentage"), "max_percentage")
            if pct <= 0 or pct > 1:
                raise RiskRuleValidationError("max_percentage must be in (0, 1]")
            params["max_percentage"] = pct
    elif rule_type == "max_daily_trades":
        if "max_count" in params:
            count = int(_as_float(params.get("max_count"), "max_count"))
            if count < 1:
                raise RiskRuleValidationError("max_count must be >= 1")
            params["max_count"] = count

    return params


def is_trigger_rule(rule_type: str) -> bool:
    return str(rule_type or "").strip() in TRIGGER_RULE_TYPES


def rule_matches_trading_mode(rule_mode: str, account_mode: str) -> bool:
    mode = str(rule_mode or "SIMULATION").strip().upper()
    account = str(account_mode or "SIMULATION").strip().upper()
    return mode == "BOTH" or mode == account


def rule_matches_market(rule_markets: Any, market: str) -> bool:
    markets = {str(item).strip().upper() for item in (rule_markets or DEFAULT_MARKETS)}
    return str(market or "CN").strip().upper() in markets
