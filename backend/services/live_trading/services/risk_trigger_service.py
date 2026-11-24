"""Risk trigger I/O: quotes, simulation flatten, events, locks."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable, Mapping
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.live_trading.services.risk_lock import (
    already_fired,
    mark_fired,
    normalize_lock_symbol,
    normalize_lock_user,
    write_account_lock,
    write_symbol_lock,
)
from backend.services.live_trading.services.risk_rule_types import is_trigger_rule
from backend.services.live_trading.services.risk_trigger_eval import (
    QuoteView,
    RuleView,
    TriggerCandidate,
    evaluate_account,
    implicit_stop_loss_rule,
    parse_user_id,
)
from backend.services.trade_shared.models.risk_event import RiskEvent
from backend.services.trade_shared.models.risk_rule import RiskRule
from backend.shared.simulation_account_keys import (
    ACTIVE_STRATEGY_KEY_PREFIX,
    parse_active_strategy_key,
    resolve_active_identity,
)
from backend.shared.stock_utils import StockCodeUtil
from backend.shared.symbol_market import market_of_symbol

logger = logging.getLogger(__name__)
_SH_TZ = ZoneInfo("Asia/Shanghai")

ENSURE_RISK_EVENTS_SQL = """
CREATE TABLE IF NOT EXISTS risk_events (
    id              SERIAL PRIMARY KEY,
    rule_id         INTEGER,
    rule_type       VARCHAR(50) NOT NULL,
    tenant_id       VARCHAR(64) NOT NULL DEFAULT 'default',
    user_id         INTEGER NOT NULL,
    trade_date      DATE NOT NULL,
    symbol          VARCHAR(32) NOT NULL DEFAULT '*',
    action          VARCHAR(32) NOT NULL,
    status          VARCHAR(32) NOT NULL,
    trigger_price   DOUBLE PRECISION,
    cost_price      DOUBLE PRECISION,
    pnl_pct         DOUBLE PRECISION,
    quantity        DOUBLE PRECISION,
    order_ids       JSONB,
    message         VARCHAR(500),
    created_at      TIMESTAMP NOT NULL DEFAULT NOW()
)
"""

_QUOTE_PRICE_FIELDS = ("Now", "last_price", "current_price", "price", "close", "Close")
_QUOTE_PCT_FIELDS = ("pct_chg", "pct_change", "change_percent", "change_pct", "pct", "ChgRatio")
QuoteFetcher = Callable[[list[str]], dict[str, QuoteView]]
ExecuteHook = Callable[..., Any]

_EVENT_DEDUP_PREFIX = "risk:event:"
_RETRY_BACKOFF_PREFIX = "risk:retry:"

# 非可执行候选（无行情 / T+1 / 空仓 / 当日已触发）的记录窗口。
# 60s 扫描 × 全天会把同一只票写成上千行（实测单日 1871 条 skipped_no_quote、
# 71 条 skipped_t1），这里按「状态 + 规则 + 标的」窗口去重，只留窗口内第一条。
_NOISE_EVENT_TTL: dict[str, int] = {
    "skipped_no_quote": 1800,
    "skipped_t1": 86400,
    "skipped_empty": 86400,
    "skipped_dedup": 1800,
}


def _env_seconds(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return max(60, int(float(raw)))
    except (TypeError, ValueError):
        return default


def retry_backoff_seconds() -> int:
    """成交失败（跌停封死/无买盘）后的重试退避，默认 10 分钟。"""
    return _env_seconds("RISK_RETRY_BACKOFF_SEC", 600)


def failure_event_ttl_seconds() -> int:
    """同一标的失败事件的记录窗口，默认 1 小时。"""
    return _env_seconds("RISK_FAILURE_EVENT_TTL_SEC", 3600)


def _day_text(trade_date: date | str) -> str:
    return trade_date.isoformat() if isinstance(trade_date, date) else str(trade_date)


def _event_dedup_key(
    *,
    status: str,
    tenant_id: str,
    user_id: Any,
    trade_date: date | str,
    rule_id: int | None,
    symbol: str,
) -> str:
    return (
        f"{_EVENT_DEDUP_PREFIX}{status}:{tenant_id or 'default'}:"
        f"{normalize_lock_user(user_id)}:{_day_text(trade_date)}:"
        f"{int(rule_id or 0)}:{normalize_lock_symbol(symbol)}"
    )


def _retry_backoff_key(
    *, tenant_id: str, user_id: Any, trade_date: date | str, symbol: str
) -> str:
    return (
        f"{_RETRY_BACKOFF_PREFIX}{tenant_id or 'default'}:"
        f"{normalize_lock_user(user_id)}:{_day_text(trade_date)}:"
        f"{normalize_lock_symbol(symbol)}"
    )


def _raw_client(redis: Any) -> Any:
    return getattr(redis, "client", redis)


def _reserve_once(redis: Any, key: str, ttl: int) -> bool:
    """窗口内只放行一次；Redis 不可用时放行（宁可多记，不能漏记）。"""
    client = _raw_client(redis)
    if client is None:
        return True
    try:
        return bool(client.set(key, "1", ex=max(60, int(ttl)), nx=True))
    except Exception as exc:  # noqa: BLE001 - 去重失败不能阻断风控
        logger.debug("risk trigger event throttle failed: %s", exc)
        return True


def retry_backoff_active(
    redis: Any, *, tenant_id: str, user_id: Any, trade_date: date | str, symbol: str
) -> bool:
    client = _raw_client(redis)
    if client is None:
        return False
    try:
        return bool(
            client.get(
                _retry_backoff_key(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    trade_date=trade_date,
                    symbol=symbol,
                )
            )
        )
    except Exception:  # noqa: BLE001
        return False


def set_retry_backoff(
    redis: Any, *, tenant_id: str, user_id: Any, trade_date: date | str, symbol: str
) -> None:
    client = _raw_client(redis)
    if client is None:
        return
    try:
        client.set(
            _retry_backoff_key(
                tenant_id=tenant_id,
                user_id=user_id,
                trade_date=trade_date,
                symbol=symbol,
            ),
            "1",
            ex=retry_backoff_seconds(),
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("risk trigger retry backoff write failed: %s", exc)


def clear_retry_backoff(
    redis: Any, *, tenant_id: str, user_id: Any, trade_date: date | str, symbol: str
) -> None:
    client = _raw_client(redis)
    if client is None:
        return
    try:
        client.delete(
            _retry_backoff_key(
                tenant_id=tenant_id,
                user_id=user_id,
                trade_date=trade_date,
                symbol=symbol,
            )
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("risk trigger retry backoff clear failed: %s", exc)


_remote_quote_client = None


def _get_remote_quote_client():
    """远端行情快照 Redis（同步客户端，与 stream/模拟撮合同源）。"""
    global _remote_quote_client
    if _remote_quote_client is not None:
        return _remote_quote_client
    try:
        import redis as redis_lib

        from backend.shared.quote_redis_config import (
            remote_quote_redis_db,
            remote_quote_redis_host,
            remote_quote_redis_password,
            remote_quote_redis_port,
        )

        _remote_quote_client = redis_lib.Redis(
            host=remote_quote_redis_host(),
            port=remote_quote_redis_port(),
            password=remote_quote_redis_password(),
            db=remote_quote_redis_db(),
            decode_responses=True,
            socket_connect_timeout=3,
            socket_timeout=5,
        )
        _remote_quote_client.ping()
    except Exception as exc:
        logger.debug("risk trigger remote quote redis unavailable: %s", exc)
        _remote_quote_client = None
    return _remote_quote_client


def _fetch_from_client(client: Any, symbols: list[str]) -> dict[str, QuoteView]:
    out: dict[str, QuoteView] = {}
    try:
        pipe = client.pipeline(transaction=False)
        planned: list[tuple[str, str]] = []
        for symbol in symbols:
            for key in _snapshot_keys(symbol):
                pipe.hgetall(key)
                planned.append((symbol, key))
        rows = pipe.execute()
    except Exception as exc:
        logger.debug("risk trigger quote pipeline failed: %s", exc)
        return out
    for (symbol, _key), raw in zip(planned, rows, strict=False):
        if symbol in out:
            continue
        if not isinstance(raw, dict):
            continue
        view = quote_from_hash(symbol, raw)
        if view is None:
            continue
        out[symbol] = view
        suffix = StockCodeUtil.to_suffix(symbol)
        prefix = StockCodeUtil.to_prefix(symbol)
        if suffix:
            out.setdefault(suffix, view)
        if prefix:
            out.setdefault(prefix, view)
    return out


def today_trade_date(now: datetime | None = None) -> date:
    current = now or datetime.now(_SH_TZ)
    if current.tzinfo is None:
        current = current.replace(tzinfo=_SH_TZ)
    else:
        current = current.astimezone(_SH_TZ)
    return current.date()


def is_cn_continuous_auction(now: datetime | None = None) -> bool:
    current = now or datetime.now(_SH_TZ)
    if current.tzinfo is None:
        current = current.replace(tzinfo=_SH_TZ)
    else:
        current = current.astimezone(_SH_TZ)
    if current.weekday() >= 5:
        return False
    hhmm = current.strftime("%H:%M")
    return "09:30" <= hhmm <= "11:30" or "13:00" <= hhmm <= "15:00"


async def ensure_risk_events_table(db: AsyncSession) -> None:
    await db.execute(text(ENSURE_RISK_EVENTS_SQL))
    await db.execute(
        text(
            "CREATE INDEX IF NOT EXISTS idx_risk_events_user_date "
            "ON risk_events (tenant_id, user_id, trade_date)"
        )
    )
    await db.commit()


def _snapshot_keys(symbol: str) -> list[str]:
    prefix = StockCodeUtil.to_prefix(str(symbol or "").strip())
    suffix = StockCodeUtil.to_suffix(str(symbol or "").strip())
    keys: list[str] = []
    if prefix:
        keys.append(f"market:snapshot:{prefix.lower()}")
        keys.append(f"market:snapshot:{prefix}")
        market, code = StockCodeUtil.split_prefix(prefix)
        if not market:
            market, code = prefix[:2], prefix[2:]
        keys.append(f"stock:{code}.{market}")
    if suffix:
        keys.append(f"market:snapshot:{suffix}")
        keys.append(f"stock:{suffix}")
    return list(dict.fromkeys(keys))


def quote_from_hash(symbol: str, data: Mapping[str, Any] | None) -> QuoteView | None:
    if not data:
        return None
    price = None
    for field in _QUOTE_PRICE_FIELDS:
        raw = data.get(field)
        if raw is None:
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if value > 0:
            price = value
            break
    if price is None:
        return None

    pct_chg = None
    for field in _QUOTE_PCT_FIELDS:
        raw = data.get(field)
        if raw is None:
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        pct_chg = value / 100.0 if abs(value) > 1 else value
        break
    if pct_chg is None:
        prev = None
        for field in ("prev_close", "PreClose", "pre_close"):
            raw = data.get(field)
            if raw is None:
                continue
            try:
                prev = float(raw)
            except (TypeError, ValueError):
                continue
            if prev > 0:
                pct_chg = (price - prev) / prev
                break
    return QuoteView(symbol=symbol, price=price, pct_chg=pct_chg)


def fetch_quotes_from_redis(redis: Any, symbols: list[str]) -> dict[str, QuoteView]:
    """本地交易库优先，缺失部分回退远端行情库（quantmindai.cn db3）。

    本地 db2 从不写入 market:snapshot:*，此前直接返回空导致全员
    skipped_no_quote；此处对未命中的 symbol 再查远端一次。
    """
    if not symbols:
        return {}
    out: dict[str, QuoteView] = {}
    client = getattr(redis, "client", redis)
    if client is not None:
        out = _fetch_from_client(client, symbols)
    missing = [s for s in dict.fromkeys(symbols) if s not in out]
    if not missing:
        return out
    remote = _get_remote_quote_client()
    if remote is None:
        return out
    # 避免远端与本地是同一实例时重复打 pipeline
    try:
        local_params = (
            getattr(client, "connection_pool", None).connection_kwargs
            if client is not None
            else {}
        )
        remote_params = getattr(remote, "connection_pool", None).connection_kwargs
        if (
            local_params
            and remote_params
            and local_params.get("host") == remote_params.get("host")
            and local_params.get("port") == remote_params.get("port")
            and local_params.get("db") == remote_params.get("db")
        ):
            return out
    except Exception:
        pass
    remote_out = _fetch_from_client(remote, missing)
    out.update(remote_out)
    return out


def load_implicit_stop_loss(redis: Any, tenant_id: str, user_id: object) -> RuleView | None:
    client = getattr(redis, "client", redis)
    if client is None:
        return None
    uid = parse_user_id(user_id)
    keys = [
        f"{ACTIVE_STRATEGY_KEY_PREFIX}{tenant_id}:{user_id}",
        f"{ACTIVE_STRATEGY_KEY_PREFIX}{tenant_id}:{str(user_id).zfill(8) if str(user_id).isdigit() else user_id}",
    ]
    for key in keys:
        try:
            raw = client.get(key)
        except Exception:
            continue
        if not raw:
            continue
        try:
            payload = json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        exec_config = payload.get("execution_config") or {}
        if not isinstance(exec_config, dict):
            continue
        stop = exec_config.get("stop_loss")
        try:
            pct = float(stop)
        except (TypeError, ValueError):
            continue
        if pct < 0:
            return implicit_stop_loss_rule(pct, uid)
    return None


def collect_needed_symbols(
    positions: Mapping[str, Any], rules: list[RuleView]
) -> list[str]:
    symbols = [str(symbol) for symbol in (positions or {}) if str(symbol).strip()]
    for rule in rules:
        if rule.rule_type == "market_index_move":
            index = str((rule.parameters or {}).get("index") or "000300.SH")
            symbols.append(index)
    return list(dict.fromkeys(symbols))


async def load_trigger_rules(db: AsyncSession) -> list[RuleView]:
    stmt = select(RiskRule).where(RiskRule.is_active.is_(True)).order_by(
        RiskRule.priority.desc(), RiskRule.id
    )
    result = await db.execute(stmt)
    return [
        RuleView.from_orm(rule)
        for rule in result.scalars().all()
        if is_trigger_rule(getattr(rule, "rule_type", ""))
    ]


def evaluate_user_account(
    *,
    positions: Mapping[str, Mapping[str, Any]],
    quotes: Mapping[str, QuoteView],
    rules: list[RuleView],
    user_id: int,
    market: str = "CN",
    account_mode: str = "SIMULATION",
    implicit_rule: RuleView | None = None,
) -> list[TriggerCandidate]:
    merged = list(rules)
    if implicit_rule is not None:
        merged.append(implicit_rule)
    return evaluate_account(
        positions=positions,
        quotes=quotes,
        rules=merged,
        user_id=user_id,
        market=market,
        account_mode=account_mode,
    )


async def persist_event(
    db: AsyncSession,
    candidate: TriggerCandidate,
    *,
    tenant_id: str,
    user_id: int,
    trade_date: date,
    status: str | None = None,
    order_ids: list[str] | None = None,
    message: str | None = None,
) -> RiskEvent:
    event = RiskEvent(
        rule_id=candidate.rule_id or None,
        rule_type=candidate.rule_type,
        tenant_id=tenant_id,
        user_id=user_id,
        trade_date=trade_date,
        symbol=candidate.symbol,
        action=candidate.action,
        status=status or candidate.status,
        trigger_price=candidate.trigger_price,
        cost_price=candidate.cost_price,
        pnl_pct=candidate.pnl_pct,
        quantity=candidate.quantity,
        order_ids=order_ids or [],
        message=message or candidate.message,
    )
    db.add(event)
    await db.flush()
    return event


async def execute_candidate(
    db: AsyncSession,
    redis: Any,
    candidate: TriggerCandidate,
    *,
    tenant_id: str,
    user_id: int,
    trade_date: date,
    dry_run: bool = False,
    execute_order: ExecuteHook | None = None,
) -> TriggerCandidate:
    """Apply one candidate. dry_run / REAL never place simulation orders."""
    if candidate.status not in {"pending"}:
        # 非可执行状态按窗口去重：无行情/T+1 这类事件每分钟都会重新命中，
        # 不去重就是 60s × 全天 × 持仓数的垃圾行。
        ttl = _NOISE_EVENT_TTL.get(str(candidate.status))
        if ttl is not None and not _reserve_once(
            redis,
            _event_dedup_key(
                status=str(candidate.status),
                tenant_id=tenant_id,
                user_id=user_id,
                trade_date=trade_date,
                rule_id=candidate.rule_id,
                symbol=candidate.symbol,
            ),
            ttl,
        ):
            return candidate
        await persist_event(db, candidate, tenant_id=tenant_id, user_id=user_id, trade_date=trade_date)
        return candidate

    if already_fired(
        redis, candidate.rule_id, tenant_id, user_id, trade_date, candidate.symbol
    ):
        candidate.status = "skipped_dedup"
        candidate.message = (candidate.message or "") + " [当日已触发]"
        await persist_event(db, candidate, tenant_id=tenant_id, user_id=user_id, trade_date=trade_date)
        return candidate

    if dry_run:
        candidate.status = "dry_run"
        await persist_event(db, candidate, tenant_id=tenant_id, user_id=user_id, trade_date=trade_date)
        return candidate

    if str(candidate.trading_mode).upper() == "REAL":
        candidate.status = "alert_only"
        await persist_event(db, candidate, tenant_id=tenant_id, user_id=user_id, trade_date=trade_date)
        mark_fired(
            redis,
            candidate.rule_id,
            tenant_id,
            user_id,
            trade_date,
            candidate.symbol,
            candidate.cooldown_seconds,
        )
        _notify(user_id, tenant_id, candidate)
        return candidate

    # 上次成交失败（如跌停封死）后的退避窗内不再下单：否则每 60s 撞一次撮合，
    # simulation_orders 里堆满 rejected 弃单、risk_events 里堆满 failed 记录。
    if retry_backoff_active(
        redis,
        tenant_id=tenant_id,
        user_id=user_id,
        trade_date=trade_date,
        symbol=candidate.symbol,
    ):
        candidate.status = "skipped_backoff"
        candidate.message = (candidate.message or "") + " [成交失败退避中]"
        return candidate

    if execute_order is None:
        execute_order = _default_execute_sim_sell

    try:
        result = await execute_order(
            db=db,
            redis=redis,
            tenant_id=tenant_id,
            user_id=user_id,
            symbol=candidate.symbol,
            quantity=candidate.quantity,
            message=candidate.message,
        )
    except Exception as exc:
        logger.exception("risk trigger execute failed symbol=%s", candidate.symbol)
        candidate.status = "failed"
        candidate.message = f"{candidate.message}; execute error: {exc}"
        set_retry_backoff(
            redis,
            tenant_id=tenant_id,
            user_id=user_id,
            trade_date=trade_date,
            symbol=candidate.symbol,
        )
        await persist_event(db, candidate, tenant_id=tenant_id, user_id=user_id, trade_date=trade_date)
        return candidate

    success = bool(result.get("success")) if isinstance(result, dict) else bool(result)
    order_id = ""
    if isinstance(result, dict):
        order_id = str(result.get("order_id") or "")
        if result.get("message"):
            candidate.message = f"{candidate.message}; {result['message']}"
    if not success:
        candidate.status = "failed"
        set_retry_backoff(
            redis,
            tenant_id=tenant_id,
            user_id=user_id,
            trade_date=trade_date,
            symbol=candidate.symbol,
        )
        # 失败事件按窗口去重（退避 10 分钟、事件 1 小时），保留可追溯但不刷屏。
        if _reserve_once(
            redis,
            _event_dedup_key(
                status="failed",
                tenant_id=tenant_id,
                user_id=user_id,
                trade_date=trade_date,
                rule_id=candidate.rule_id,
                symbol=candidate.symbol,
            ),
            failure_event_ttl_seconds(),
        ):
            await persist_event(
                db,
                candidate,
                tenant_id=tenant_id,
                user_id=user_id,
                trade_date=trade_date,
                order_ids=[order_id] if order_id else [],
            )
        return candidate

    candidate.status = "filled"
    await persist_event(
        db,
        candidate,
        tenant_id=tenant_id,
        user_id=user_id,
        trade_date=trade_date,
        order_ids=[order_id] if order_id else [],
    )
    clear_retry_backoff(
        redis,
        tenant_id=tenant_id,
        user_id=user_id,
        trade_date=trade_date,
        symbol=candidate.symbol,
    )
    if candidate.action == "flatten_all":
        write_account_lock(redis, tenant_id, user_id, trade_date)
    else:
        write_symbol_lock(redis, tenant_id, user_id, trade_date, candidate.symbol)
    mark_fired(
        redis,
        candidate.rule_id,
        tenant_id,
        user_id,
        trade_date,
        candidate.symbol,
        candidate.cooldown_seconds,
    )
    _notify(user_id, tenant_id, candidate)
    return candidate


async def apply_candidates(
    db: AsyncSession,
    redis: Any,
    candidates: list[TriggerCandidate],
    *,
    tenant_id: str,
    user_id: int,
    trade_date: date | None = None,
    dry_run: bool = False,
    execute_order: ExecuteHook | None = None,
) -> list[TriggerCandidate]:
    day = trade_date or today_trade_date()
    applied: list[TriggerCandidate] = []
    for candidate in candidates:
        applied.append(
            await execute_candidate(
                db,
                redis,
                candidate,
                tenant_id=tenant_id,
                user_id=user_id,
                trade_date=day,
                dry_run=dry_run,
                execute_order=execute_order,
            )
        )
    await db.commit()
    return applied


async def _default_execute_sim_sell(
    *,
    db: AsyncSession,
    redis: Any,
    tenant_id: str,
    user_id: int,
    symbol: str,
    quantity: float,
    message: str,
) -> dict[str, Any]:
    from backend.services.simulation.models.order import (
        OrderSide,
        OrderType,
        SimOrder,
    )
    from backend.services.simulation.services.execution_engine import (
        SimulationExecutionEngine,
    )
    from backend.services.simulation.services.order_service import build_order_projection
    from backend.services.trade_shared.simulation_manager import SimulationAccountManager

    manager = SimulationAccountManager(redis)
    engine = SimulationExecutionEngine(db, manager)
    sim_order = SimOrder(
        tenant_id=tenant_id,
        user_id=user_id,
        symbol=symbol,
        side=OrderSide.SELL,
        order_type=OrderType.MARKET,
        quantity=float(quantity),
        remarks=f"risk_rule:{message}"[:500],
    )
    db.add(sim_order)
    await db.flush()
    # 止损单同样写入 v2 投影，否则 simulation_orders/fills 永远缺止损记录，
    # 且 apply_filled 找不到投影会跳过 SimulationFill 写入。
    db.add(build_order_projection(sim_order, trigger_source="risk"))
    await db.flush()
    result = await engine.execute_order(sim_order)
    if result.success:
        await engine.apply_filled(sim_order, result)
        return {
            "success": True,
            "order_id": str(sim_order.order_id or ""),
            "message": result.message,
        }
    await engine.mark_rejected(sim_order, result.message)
    return {
        "success": False,
        "order_id": str(sim_order.order_id or ""),
        "message": result.message,
    }


def _notify(user_id: int, tenant_id: str, candidate: TriggerCandidate) -> None:
    logger.info(
        "risk trigger notify status=%s user=%s tenant=%s symbol=%s msg=%s",
        candidate.status,
        user_id,
        tenant_id,
        candidate.symbol,
        candidate.message,
    )
    try:
        from backend.shared.notification_publisher import publish_notification_async

        import asyncio

        title = "风控触发" if candidate.status == "filled" else "风控告警"
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(
            publish_notification_async(
                user_id=str(user_id),
                tenant_id=str(tenant_id or "default"),
                title=title,
                content=candidate.message or f"{candidate.rule_type} {candidate.symbol}",
                type="trading",
                level="warning",
                market=market_of_symbol(candidate.symbol),
            )
        )
    except Exception:
        logger.debug("risk trigger notification skipped", exc_info=True)


def iter_active_strategy_payloads(redis: Any) -> list[dict[str, Any]]:
    client = getattr(redis, "client", redis)
    rows: list[dict[str, Any]] = []
    if client is None:
        return rows
    try:
        keys = list(client.scan_iter(match=f"{ACTIVE_STRATEGY_KEY_PREFIX}*", count=200))
    except Exception:
        return rows
    for raw_key in keys:
        key = str(raw_key)
        parsed = parse_active_strategy_key(key)
        if parsed is None:
            continue
        try:
            raw = client.get(key)
            payload = json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        tenant_id, user_id = resolve_active_identity(
            tenant_suffix=parsed[0], user_suffix=parsed[1], payload=payload
        )
        rows.append(
            {
                "key": key,
                "tenant_id": tenant_id,
                "user_id": user_id,
                "mode": str(payload.get("mode") or "").upper(),
                "payload": payload,
            }
        )
    return rows
