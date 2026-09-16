"""Pure evaluation of trigger-type risk rules (no I/O)."""

from __future__ import annotations

from dataclasses import dataclass, field
from collections.abc import Iterable, Mapping
from typing import Any

from backend.services.live_trading.services.risk_rule_types import (
    DEFAULT_INDEX,
    is_trigger_rule,
    rule_matches_market,
    rule_matches_trading_mode,
)
from backend.services.live_trading.services.risk_lock import normalize_lock_symbol
from backend.shared.stock_utils import StockCodeUtil


@dataclass(frozen=True)
class QuoteView:
    symbol: str
    price: float
    pct_chg: float | None = None


@dataclass
class TriggerCandidate:
    rule_id: int | None
    rule_name: str
    rule_type: str
    symbol: str
    action: str
    quantity: float
    trigger_price: float | None
    cost_price: float | None
    pnl_pct: float | None
    status: str
    message: str
    trading_mode: str
    cooldown_seconds: int | None = None


@dataclass
class RuleView:
    id: int | None
    rule_name: str
    rule_type: str
    parameters: dict[str, Any] = field(default_factory=dict)
    applies_to_all: bool = True
    user_ids: list[int] | None = None
    priority: int = 0

    @classmethod
    def from_orm(cls, rule: Any) -> RuleView:
        return cls(
            id=getattr(rule, "id", None),
            rule_name=str(getattr(rule, "rule_name", "") or ""),
            rule_type=str(getattr(rule, "rule_type", "") or ""),
            parameters=dict(getattr(rule, "parameters", None) or {}),
            applies_to_all=bool(getattr(rule, "applies_to_all", True)),
            user_ids=list(getattr(rule, "user_ids", None) or []) or None,
            priority=int(getattr(rule, "priority", 0) or 0),
        )


def parse_user_id(raw: object) -> int:
    text = str(raw or "").strip()
    if text.isdigit():
        return int(text)
    return 0


def rule_applies_to_user(rule: RuleView, user_id: int) -> bool:
    if rule.applies_to_all:
        return True
    return user_id in {int(item) for item in (rule.user_ids or [])}


def implicit_stop_loss_rule(pct: float, user_id: int) -> RuleView:
    return RuleView(
        id=0,
        rule_name="execution_config.stop_loss",
        rule_type="position_stop_loss",
        parameters={"pct": float(pct), "trading_mode": "SIMULATION", "markets": ["CN"]},
        applies_to_all=False,
        user_ids=[user_id],
        priority=0,
    )


def _lookup_quote(quotes: Mapping[str, QuoteView], symbol: str) -> QuoteView | None:
    if symbol in quotes:
        return quotes[symbol]
    suffix = StockCodeUtil.to_suffix(symbol)
    if suffix and suffix in quotes:
        return quotes[suffix]
    prefix = StockCodeUtil.to_prefix(symbol)
    if prefix and prefix in quotes:
        return quotes[prefix]
    return None


def position_volumes(pos: Mapping[str, Any]) -> tuple[float, float]:
    volume = float(pos.get("volume") or pos.get("quantity") or 0)
    if "available_volume" in pos and pos.get("available_volume") is not None:
        available = float(pos.get("available_volume") or 0)
    elif "available_quantity" in pos and pos.get("available_quantity") is not None:
        available = float(pos.get("available_quantity") or 0)
    else:
        available = volume
    return volume, available


def position_cost(pos: Mapping[str, Any]) -> float:
    for key in ("cost", "cost_price", "avg_price", "average_price"):
        raw = pos.get(key)
        if raw is None:
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return 0.0


def pnl_pct(price: float, cost: float) -> float | None:
    if price <= 0 or cost <= 0:
        return None
    return (price - cost) / cost


def _applicable_trigger_rules(
    rules: Iterable[RuleView],
    *,
    user_id: int,
    market: str,
    account_mode: str,
) -> list[RuleView]:
    applicable: list[RuleView] = []
    for rule in rules:
        if not is_trigger_rule(rule.rule_type):
            continue
        if not rule_applies_to_user(rule, user_id):
            continue
        params = rule.parameters or {}
        if not rule_matches_trading_mode(params.get("trading_mode"), account_mode):
            continue
        if not rule_matches_market(params.get("markets"), market):
            continue
        applicable.append(rule)
    applicable.sort(key=lambda item: (-int(item.priority or 0), int(item.id or 0)))
    return applicable


def _pick_tightest(
    rules: list[RuleView], *, prefer_min: bool
) -> tuple[RuleView, float] | None:
    scored: list[tuple[float, RuleView]] = []
    for rule in rules:
        try:
            pct = float((rule.parameters or {}).get("pct"))
        except (TypeError, ValueError):
            continue
        scored.append((pct, rule))
    if not scored:
        return None
    pct, rule = (min if prefer_min else max)(scored, key=lambda item: item[0])
    return rule, pct


def evaluate_account(
    *,
    positions: Mapping[str, Mapping[str, Any]],
    quotes: Mapping[str, QuoteView],
    rules: Iterable[RuleView],
    user_id: int,
    market: str = "CN",
    account_mode: str = "SIMULATION",
) -> list[TriggerCandidate]:
    """Evaluate trigger rules against one account. No I/O."""
    trigger_rules = _applicable_trigger_rules(
        rules, user_id=user_id, market=market, account_mode=account_mode
    )
    if not trigger_rules:
        return []

    sl_rules = [r for r in trigger_rules if r.rule_type == "position_stop_loss"]
    tp_rules = [r for r in trigger_rules if r.rule_type == "position_take_profit"]
    market_rules = [r for r in trigger_rules if r.rule_type == "market_index_move"]

    candidates: list[TriggerCandidate] = []
    candidates.extend(
        _evaluate_positions(
            positions,
            quotes,
            sl_rules=sl_rules,
            tp_rules=tp_rules,
            account_mode=account_mode,
        )
    )
    candidates.extend(
        _evaluate_market_rules(
            positions,
            quotes,
            market_rules,
            account_mode=account_mode,
        )
    )
    return candidates


def _evaluate_positions(
    positions: Mapping[str, Mapping[str, Any]],
    quotes: Mapping[str, QuoteView],
    *,
    sl_rules: list[RuleView],
    tp_rules: list[RuleView],
    account_mode: str,
) -> list[TriggerCandidate]:
    if not sl_rules and not tp_rules:
        return []
    sl_pick = _pick_tightest(sl_rules, prefer_min=True)
    tp_pick = _pick_tightest(tp_rules, prefer_min=True)
    out: list[TriggerCandidate] = []
    for raw_symbol, pos in (positions or {}).items():
        symbol = str(raw_symbol or "").strip()
        if not symbol or "::" in symbol:
            continue
        volume, available = position_volumes(pos)
        if volume <= 0:
            continue
        quote = _lookup_quote(quotes, symbol)
        if quote is None or quote.price <= 0:
            rule = (sl_pick or tp_pick or (None, None))[0]
            if rule is None:
                continue
            out.append(
                TriggerCandidate(
                    rule_id=rule.id,
                    rule_name=rule.rule_name,
                    rule_type=rule.rule_type,
                    symbol=normalize_lock_symbol(symbol),
                    action="flatten_symbol",
                    quantity=available,
                    trigger_price=None,
                    cost_price=position_cost(pos) or None,
                    pnl_pct=None,
                    status="skipped_no_quote",
                    message=f"无可用行情: {symbol}",
                    trading_mode=account_mode,
                    cooldown_seconds=_cooldown(rule),
                )
            )
            continue
        cost = position_cost(pos)
        pnl = pnl_pct(quote.price, cost)
        if pnl is None:
            continue

        # T-P2-04：止损/止盈判定委托 exit_rules 唯一实现（规则语义同线：
        # 扫描器 pct 为负数比例，canonical 用正比例；映射回命中类型保持本模块契约）
        from backend.shared.exit_rules import (
            RULE_HARD_STOP,
            ExitRuleSet,
            PositionState,
            evaluate_exit,
        )

        decision = evaluate_exit(
            ExitRuleSet(
                hard_stop_pct=abs(float(sl_pick[1])) if sl_pick is not None else None,
                take_profit_pct=float(tp_pick[1]) if tp_pick is not None else None,
            ),
            PositionState(entry_price=cost, last_price=quote.price),
        )

        hit_rule: RuleView | None = None
        hit_type = ""
        if decision.should_exit and decision.rule_id == RULE_HARD_STOP and sl_pick is not None:
            hit_rule, _ = sl_pick
            hit_type = "position_stop_loss"
        elif decision.should_exit and tp_pick is not None:
            hit_rule, _ = tp_pick
            hit_type = "position_take_profit"
        if hit_rule is None:
            continue

        if available <= 0:
            out.append(
                TriggerCandidate(
                    rule_id=hit_rule.id,
                    rule_name=hit_rule.rule_name,
                    rule_type=hit_type,
                    symbol=normalize_lock_symbol(symbol),
                    action="flatten_symbol",
                    quantity=0,
                    trigger_price=quote.price,
                    cost_price=cost,
                    pnl_pct=pnl,
                    status="skipped_t1",
                    message=f"T+1 不可卖: {symbol} volume={volume}",
                    trading_mode=account_mode,
                    cooldown_seconds=_cooldown(hit_rule),
                )
            )
            continue

        threshold = float((hit_rule.parameters or {}).get("pct"))
        out.append(
            TriggerCandidate(
                rule_id=hit_rule.id,
                rule_name=hit_rule.rule_name,
                rule_type=hit_type,
                symbol=normalize_lock_symbol(symbol),
                action="flatten_symbol",
                quantity=available,
                trigger_price=quote.price,
                cost_price=cost,
                pnl_pct=pnl,
                status="pending",
                message=f"{hit_type} 触及 {threshold:.2%} 实际 {pnl:.2%}",
                trading_mode=account_mode,
                cooldown_seconds=_cooldown(hit_rule),
            )
        )
    return out


def _evaluate_market_rules(
    positions: Mapping[str, Mapping[str, Any]],
    quotes: Mapping[str, QuoteView],
    market_rules: list[RuleView],
    *,
    account_mode: str,
) -> list[TriggerCandidate]:
    out: list[TriggerCandidate] = []
    for rule in market_rules:
        index = str((rule.parameters or {}).get("index") or DEFAULT_INDEX)
        try:
            threshold = float((rule.parameters or {}).get("pct"))
        except (TypeError, ValueError):
            continue
        quote = _lookup_quote(quotes, index)
        if quote is None or quote.pct_chg is None:
            out.append(
                TriggerCandidate(
                    rule_id=rule.id,
                    rule_name=rule.rule_name,
                    rule_type=rule.rule_type,
                    symbol="*",
                    action="flatten_all",
                    quantity=0,
                    trigger_price=None,
                    cost_price=None,
                    pnl_pct=None,
                    status="skipped_no_quote",
                    message=f"指数无行情: {index}",
                    trading_mode=account_mode,
                    cooldown_seconds=_cooldown(rule),
                )
            )
            continue
        hit = (
            quote.pct_chg <= threshold if threshold < 0 else quote.pct_chg >= threshold
        )
        if not hit:
            continue

        sellable = [
            (str(symbol), position_volumes(pos)[1], position_volumes(pos)[0], pos)
            for symbol, pos in (positions or {}).items()
            if str(symbol).strip() and "::" not in str(symbol)
        ]
        if not sellable:
            out.append(
                TriggerCandidate(
                    rule_id=rule.id,
                    rule_name=rule.rule_name,
                    rule_type=rule.rule_type,
                    symbol="*",
                    action="flatten_all",
                    quantity=0,
                    trigger_price=quote.price,
                    cost_price=None,
                    pnl_pct=quote.pct_chg,
                    status="skipped_empty",
                    message=f"全市场条件触发但无持仓 index={index} chg={quote.pct_chg:.2%}",
                    trading_mode=account_mode,
                    cooldown_seconds=_cooldown(rule),
                )
            )
            continue

        for symbol, available, volume, pos in sellable:
            px = _lookup_quote(quotes, symbol)
            cost = position_cost(pos)
            if available <= 0:
                out.append(
                    TriggerCandidate(
                        rule_id=rule.id,
                        rule_name=rule.rule_name,
                        rule_type=rule.rule_type,
                        symbol=normalize_lock_symbol(symbol),
                        action="flatten_all",
                        quantity=0,
                        trigger_price=px.price if px else None,
                        cost_price=cost or None,
                        pnl_pct=quote.pct_chg,
                        status="skipped_t1",
                        message=f"全市场条件触发但 T+1 不可卖: {symbol} volume={volume}",
                        trading_mode=account_mode,
                        cooldown_seconds=_cooldown(rule),
                    )
                )
                continue
            out.append(
                TriggerCandidate(
                    rule_id=rule.id,
                    rule_name=rule.rule_name,
                    rule_type=rule.rule_type,
                    symbol=normalize_lock_symbol(symbol),
                    action="flatten_all",
                    quantity=available,
                    trigger_price=px.price if px else quote.price,
                    cost_price=cost or None,
                    pnl_pct=quote.pct_chg,
                    status="pending",
                    message=(
                        f"全市场条件 {index} {quote.pct_chg:.2%} "
                        f"触及 {threshold:.2%}, 平 {symbol}"
                    ),
                    trading_mode=account_mode,
                    cooldown_seconds=_cooldown(rule),
                )
            )
    return out


def _cooldown(rule: RuleView) -> int | None:
    raw = (rule.parameters or {}).get("cooldown_seconds")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None
