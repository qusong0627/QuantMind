from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

import pandas as pd
import redis as redis_lib
from fastapi import HTTPException
from sqlalchemy import text, and_, select, delete

try:
    import exchange_calendars as xcals
except ImportError:  # pragma: no cover - optional in local/unit test env
    xcals = None

from backend.services.trade.redis_client import get_redis
from backend.shared.database_manager_v2 import get_session
from backend.shared.strategy_storage import get_strategy_storage_service

from backend.services.trade.portfolio.models import Portfolio
from backend.services.trade.models.order import Order

from .manual_execution_log_stream import manual_execution_log_stream
from .manual_execution_persistence import manual_execution_persistence

logger = logging.getLogger(__name__)

_quote_redis = None
_SH_TZ = ZoneInfo("Asia/Shanghai")


def _get_quote_redis():
    global _quote_redis
    if _quote_redis is None:
        try:
            host = os.getenv("REDIS_HOST", "quantmind-redis")
            port = int(os.getenv("REDIS_PORT", "6379"))
            password = os.getenv("REDIS_PASSWORD", "")
            db = int(os.getenv("REDIS_DB_MARKET", "3"))
            _quote_redis = redis_lib.Redis(
                host=host,
                port=port,
                password=password,
                db=db,
                decode_responses=True,
                socket_timeout=2,
            )
            _quote_redis.ping()
            logger.info(f"[ManualExecution] 已连接行情 Redis: {host}:{port} db={db}")
        except Exception as e:
            logger.warning(f"[ManualExecution] 无法连接行情 Redis: {e}")
            _quote_redis = None
    return _quote_redis


def _get_realtime_price(symbol: str) -> float | None:
    try:
        r = _get_quote_redis()
        if not r:
            return None
        sym = symbol.replace("SH", "").replace("SZ", "").replace("BJ", "")
        if symbol.startswith("SH"):
            key = f"stock:{sym}.SH"
        elif symbol.startswith("SZ"):
            key = f"stock:{sym}.SZ"
        elif symbol.startswith("BJ") or symbol.startswith("920"):
            key = f"stock:{sym}.BJ"
        else:
            key = f"stock:{symbol}"
        price = r.hget(key, "Now")
        if price:
            return float(price)
    except Exception as e:
        logger.debug(f"[ManualExecution] 获取 {symbol} 实时价格失败: {e}")
    return None


_TOPK_STYLE_STRATEGIES = {
    "topkdropout",
    "standard_topk",
    "alpha_cross_section",
    "momentum",
    "adaptive_drift",
    "deep_time_series",
    "volatilityweighted",
    "score_weighted",
    "weightstrategy",
}


@dataclass
class PreparedManualExecution:
    task_id: str
    tenant_id: str
    user_id: str
    strategy_id: str
    strategy_name: str
    run_id: str
    model_id: str
    prediction_trade_date: date
    trading_mode: str
    request_payload: dict[str, Any]
    run: dict[str, Any]
    strategy: dict[str, Any]


def _parse_iso_date(value: Any) -> date:
    if isinstance(value, date):
        return value
    if isinstance(value, datetime):
        return value.date()
    text_value = str(value or "").strip()
    if not text_value:
        raise HTTPException(status_code=400, detail="prediction_trade_date 缺失")
    try:
        return date.fromisoformat(text_value[:10])
    except Exception as exc:
        raise HTTPException(
            status_code=400, detail=f"prediction_trade_date 非法: {exc}"
        ) from exc


def _normalize_trading_mode(value: Any) -> str:
    mode = str(getattr(value, "value", value) or "").strip().upper()
    if mode not in {"REAL", "SHADOW", "SIMULATION"}:
        raise HTTPException(status_code=400, detail=f"unsupported trading_mode: {mode}")
    return mode


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except Exception:
        return default


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except Exception:
        return default


def _normalize_to_broker_symbol(sym: str) -> str:
    s = str(sym or "").strip().upper()
    if s.startswith("SH") and len(s) > 2:
        return f"{s[2:]}.SH"
    elif s.startswith("SZ") and len(s) > 2:
        return f"{s[2:]}.SZ"
    elif s.startswith("BJ") and len(s) > 2:
        return f"{s[2:]}.BJ"
    return s


def _manual_task_agent_protect_price_ratio() -> float:
    ratio = _to_float(
        os.getenv("MANUAL_TASK_AGENT_PROTECT_PRICE_RATIO", "0.002"),
        0.002,
    )
    if ratio <= 0:
        return 0.002
    return min(ratio, 0.1)


def _manual_task_sell_wait_timeout_sec() -> int:
    timeout = _to_int(
        os.getenv("MANUAL_TASK_SELL_WAIT_TIMEOUT_SEC", "300"),
        300,
    )
    if timeout <= 0:
        return 0
    return min(timeout, 900)


def _manual_task_sell_wait_poll_sec() -> float:
    poll = _to_float(
        os.getenv("MANUAL_TASK_SELL_WAIT_POLL_SEC", "3"),
        3.0,
    )
    if poll <= 0:
        return 3.0
    return min(max(poll, 1.0), 10.0)


def _order_status_text(value: Any) -> str:
    raw = getattr(value, "value", value)
    return str(raw or "").strip().lower()


def _rebuild_buy_orders_with_budget(
    *,
    buy_orders: list[dict[str, Any]],
    buy_budget: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], float]:
    """
    根据“卖出成交后可用资金”重算买单数量。
    返回: (可执行买单, 新增跳过项, 预估剩余现金)
    """
    normalized_rows: list[dict[str, Any]] = []
    skipped_items: list[dict[str, Any]] = []
    remaining_budget = max(0.0, _to_float(buy_budget, 0.0))

    for row in buy_orders:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        normalized_rows.append(dict(row, symbol=symbol))

    recalculated: list[dict[str, Any]] = []
    for index, row in enumerate(normalized_rows):
        slots = max(1, len(normalized_rows) - index)
        per_slot_budget = remaining_budget / slots

        symbol = row["symbol"]
        reference_price = _to_float(
            row.get("reference_price") or row.get("price"),
            0.0,
        )
        if reference_price <= 0:
            reference_price = _to_float(_get_realtime_price(symbol), 0.0)
        if reference_price <= 0:
            skipped_items.append(
                {
                    "symbol": symbol,
                    "action": "BUY",
                    "reason": "卖后买阶段缺少实时价格，无法重算买入数量",
                    "source": "buy_signal",
                }
            )
            continue

        quantity = _floor_board_lot(per_slot_budget / reference_price)
        if quantity <= 0:
            skipped_items.append(
                {
                    "symbol": symbol,
                    "action": "BUY",
                    "reason": "卖后买阶段可用资金不足以买入 100 股",
                    "source": "buy_signal",
                }
            )
            continue

        estimated_notional = round(quantity * reference_price, 2)
        remaining_budget = max(0.0, remaining_budget - estimated_notional)

        updated = dict(row)
        updated["quantity"] = quantity
        updated["reference_price"] = reference_price
        updated["price"] = reference_price
        updated["estimated_notional"] = estimated_notional
        updated["reason"] = (
            f"{str(row.get('reason') or '').strip()}；卖后买资金门控重算"
        ).strip("；")
        recalculated.append(updated)

    return recalculated, skipped_items, remaining_budget


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _floor_board_lot(quantity: float, lot_size: int = 100) -> int:
    if quantity <= 0:
        return 0
    return int(quantity // lot_size) * lot_size


def _build_preview_hash(payload: dict[str, Any]) -> str:
    # 只对"执行意图"部分做哈希：策略上下文 + 卖出/买入预案。
    # 故意排除 account_snapshot，避免账户快照时间戳在 preview→submit 的短暂间隔内
    # 因行情更新而变化，导致 submit 侧重算哈希不匹配从而误报 409。
    stable = {
        "strategy_context": payload.get("strategy_context"),
        "sell_orders": payload.get("sell_orders"),
        "buy_orders": payload.get("buy_orders"),
    }
    return hashlib.sha256(_stable_json(stable).encode("utf-8")).hexdigest()


def _normalize_strategy_params(strategy: dict[str, Any] | None) -> dict[str, Any]:
    params = strategy.get("parameters") if isinstance(strategy, dict) else {}
    return params if isinstance(params, dict) else {}


def _normalize_positions(raw_positions: Any) -> dict[str, dict[str, Any]]:
    if isinstance(raw_positions, dict):
        iterable = []
        for key, value in raw_positions.items():
            item = dict(value or {}) if isinstance(value, dict) else {}
            item.setdefault("symbol", key)
            iterable.append(item)
    elif isinstance(raw_positions, list):
        iterable = [
            dict(item or {}) for item in raw_positions if isinstance(item, dict)
        ]
    else:
        iterable = []

    positions: dict[str, dict[str, Any]] = {}
    for item in iterable:
        symbol = (
            str(item.get("symbol") or item.get("stock_code") or item.get("code") or "")
            .strip()
            .upper()
        )
        if not symbol:
            continue
        total_volume = _to_int(item.get("volume") or item.get("quantity"), 0)
        available_volume = _to_int(
            item.get("available_volume")
            or item.get("available_quantity")
            or total_volume,
            total_volume,
        )
        last_price = _to_float(
            item.get("last_price") or item.get("current_price") or item.get("price"),
            0.0,
        )
        market_value = _to_float(
            item.get("market_value"),
            last_price * total_volume if last_price > 0 else 0.0,
        )
        cost_price = _to_float(
            item.get("cost_price") or item.get("avg_cost") or item.get("avg_price"), 0.0
        )
        positions[symbol] = {
            "symbol": symbol,
            "name": str(item.get("symbol_name") or item.get("name") or "").strip(),
            "volume": total_volume,
            "available_volume": max(0, available_volume),
            "last_price": last_price,
            "cost_price": cost_price,
            "market_value": market_value,
        }
    return positions


def _filter_signal_rows(
    rows: list[dict[str, Any]],
    strategy_params: dict[str, Any],
    current_positions: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    sorted_rows = sorted(
        [dict(row or {}) for row in rows if isinstance(row, dict)],
        key=lambda item: (
            -_to_float(item.get("fusion_score"), float("-inf")),
            str(item.get("symbol") or ""),
        ),
    )
    strategy_type = str(strategy_params.get("strategy_type") or "").strip().lower()
    topk = max(1, _to_int(strategy_params.get("topk"), 50))
    n_drop = max(0, _to_int(strategy_params.get("n_drop"), 0))
    current_symbols = set(current_positions.keys())

    explicit_buys = [
        row
        for row in sorted_rows
        if str(row.get("signal_side") or "").strip().lower() == "buy"
    ]
    explicit_sells = [
        row
        for row in sorted_rows
        if str(row.get("signal_side") or "").strip().lower() == "sell"
    ]

    score_lookup = {
        str(row.get("symbol") or "").strip().upper(): _to_float(
            row.get("fusion_score"), -999999.0
        )
        for row in sorted_rows
    }
    inferred = False
    buy_candidates: list[dict[str, Any]]
    sell_candidates: list[dict[str, Any]] = []

    if explicit_buys or explicit_sells:
        buy_candidates = explicit_buys[:topk]
        sell_candidates = explicit_sells
        if (
            not explicit_sells
            and strategy_type in _TOPK_STYLE_STRATEGIES
            and buy_candidates
        ):
            target_symbols = {
                str(row.get("symbol") or "").strip().upper() for row in buy_candidates
            }
            inferred_sell_symbols = sorted(
                [symbol for symbol in current_symbols if symbol not in target_symbols],
                key=lambda symbol: (score_lookup.get(symbol, -999999.0), symbol),
            )
            if n_drop > 0:
                inferred_sell_symbols = inferred_sell_symbols[:n_drop]
            sell_candidates = [
                {
                    "symbol": symbol,
                    "signal_side": "sell",
                    "reason": "不在当前策略目标持仓",
                }
                for symbol in inferred_sell_symbols
            ]
    else:
        inferred = True
        ranked = sorted_rows[:topk]
        target_symbols = {
            str(row.get("symbol") or "").strip().upper() for row in ranked
        }
        inferred_sell_symbols = sorted(
            [symbol for symbol in current_symbols if symbol not in target_symbols],
            key=lambda symbol: (score_lookup.get(symbol, -999999.0), symbol),
        )
        if n_drop > 0:
            inferred_sell_symbols = inferred_sell_symbols[:n_drop]
        sell_candidates = [
            {
                "symbol": symbol,
                "signal_side": "sell",
                "reason": "TopK 调仓卖出非目标持仓",
            }
            for symbol in inferred_sell_symbols
        ]
        buy_pool = [
            row
            for row in ranked
            if str(row.get("symbol") or "").strip().upper() not in current_symbols
        ]
        if current_symbols and n_drop > 0:
            buy_pool = buy_pool[: len(sell_candidates)]
        buy_candidates = buy_pool if buy_pool else ranked

    return {
        "strategy_type": strategy_type,
        "topk": topk,
        "n_drop": n_drop,
        "rows": sorted_rows,
        "buy_candidates": buy_candidates,
        "sell_candidates": sell_candidates,
        "inferred": inferred,
    }


def _build_execution_plan_from_signals(
    *,
    signal_rows: list[dict[str, Any]],
    strategy_params: dict[str, Any],
    account_snapshot: dict[str, Any],
) -> dict[str, Any]:
    positions = _normalize_positions((account_snapshot or {}).get("positions"))
    cash = _to_float(
        account_snapshot.get("available_cash") or account_snapshot.get("cash"), 0.0
    )
    signal_plan = _filter_signal_rows(signal_rows, strategy_params, positions)
    skipped_items: list[dict[str, Any]] = []
    sell_orders: list[dict[str, Any]] = []
    buy_orders: list[dict[str, Any]] = []

    for row in signal_plan["sell_candidates"]:
        symbol = str(row.get("symbol") or "").strip().upper()
        position = positions.get(symbol)
        if not position or _to_int(position.get("available_volume"), 0) <= 0:
            skipped_items.append(
                {
                    "symbol": symbol,
                    "action": "SELL",
                    "reason": "当前无可卖持仓",
                    "source": "sell_signal",
                }
            )
            continue
        quantity = max(0, _to_int(position.get("available_volume"), 0))
        expected_price = _to_float(row.get("expected_price"), 0.0)
        reference_price = expected_price or _to_float(position.get("last_price"), 0.0)
        if reference_price <= 0:
            skipped_items.append(
                {
                    "symbol": symbol,
                    "action": "SELL",
                    "reason": "缺少卖出参考价格",
                    "source": "sell_signal",
                }
            )
            continue
        # 始终使用限价单：优先用信号预期价，无预期价则用持仓最新价。
        # 避免市价单因无法获取实时行情被风控拒绝。
        limit_price = expected_price if expected_price > 0 else reference_price
        sell_orders.append(
            {
                "symbol": symbol,
                "name": position.get("name") or "",
                "side": "SELL",
                "trade_action": "SELL_TO_CLOSE",
                "quantity": quantity,
                "order_type": "LIMIT",
                "price": limit_price,
                "reference_price": reference_price,
                "estimated_notional": round(quantity * reference_price, 2),
                "current_volume": _to_int(position.get("volume"), 0),
                "current_market_value": round(
                    _to_float(position.get("market_value"), 0.0), 2
                ),
                "reason": str(row.get("reason") or "卖出信号触发调仓"),
                "fusion_score": _to_float(row.get("fusion_score"), 0.0),
            }
        )

    estimated_sell_proceeds = sum(
        _to_float(item.get("estimated_notional"), 0.0) for item in sell_orders
    )
    buy_budget = cash + estimated_sell_proceeds
    sequential_budget = buy_budget

    raw_buy_candidates = signal_plan["buy_candidates"]
    valid_candidates: list[dict[str, Any]] = []
    for row in raw_buy_candidates:
        symbol = str(row.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        if symbol in positions:
            skipped_items.append(
                {
                    "symbol": symbol,
                    "action": "BUY",
                    "reason": "当前已持仓，首版预案不做加仓",
                    "source": "buy_signal",
                }
            )
            continue
        reference_price = _to_float(row.get("expected_price"), 0.0)
        if reference_price <= 0:
            reference_price = _get_realtime_price(symbol) or 0.0
        valid_candidates.append(
            dict(row, symbol=symbol, reference_price=reference_price)
        )

    for index, row in enumerate(valid_candidates):
        slots = max(1, len(valid_candidates) - index)
        per_slot_budget = sequential_budget / slots

        ref_price = row.get("reference_price", 0.0)
        if ref_price <= 0:
            quantity = 0
            estimated_notional = 0.0
            reason = "缺少实时价格，无法估算买入数量"
        else:
            quantity = _floor_board_lot(per_slot_budget / ref_price)
            if quantity <= 0:
                skipped_items.append(
                    {
                        "symbol": row["symbol"],
                        "action": "BUY",
                        "reason": "预算不足以买入 100 股",
                        "source": "buy_signal",
                    }
                )
                continue
            estimated_notional = round(quantity * ref_price, 2)
            sequential_budget = max(0.0, sequential_budget - estimated_notional)
            reason = "按预估可用资金等额分配买入预算"

        buy_orders.append(
            {
                "symbol": row["symbol"],
                "name": "",
                "side": "BUY",
                "trade_action": "BUY_TO_OPEN",
                "quantity": quantity,
                "order_type": "LIMIT",
                "price": ref_price,
                "reference_price": ref_price,
                "estimated_notional": estimated_notional,
                "current_volume": 0,
                "current_market_value": 0.0,
                "reason": reason,
                "fusion_score": _to_float(row.get("fusion_score"), 0.0),
            }
        )

    return {
        "sell_orders": sell_orders,
        "buy_orders": buy_orders,
        "skipped_items": skipped_items,
        "summary": {
            "signal_count": len(signal_rows),
            "buy_candidate_count": len(raw_buy_candidates),
            "sell_candidate_count": len(signal_plan["sell_candidates"]),
            "sell_order_count": len(sell_orders),
            "buy_order_count": len(buy_orders),
            "skipped_count": len(skipped_items),
            "estimated_sell_proceeds": round(estimated_sell_proceeds, 2),
            "estimated_buy_amount": round(
                sum(
                    _to_float(item.get("estimated_notional"), 0.0)
                    for item in buy_orders
                ),
                2,
            ),
            "estimated_remaining_cash": round(sequential_budget, 2),
            "available_cash": round(cash, 2),
            "inferred_signal_plan": bool(signal_plan["inferred"]),
            "strategy_type": signal_plan["strategy_type"],
            "topk": signal_plan["topk"],
            "n_drop": signal_plan["n_drop"],
        },
    }


def _normalize_hosted_signal_rows(
    raw_signals: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for item in raw_signals or []:
        if not isinstance(item, dict):
            continue
        symbol = _normalize_to_broker_symbol(item.get("symbol"))
        side = (
            str(item.get("action") or item.get("side") or item.get("signal_side") or "")
            .strip()
            .upper()
        )
        if not symbol or side not in {"BUY", "SELL"}:
            continue
        normalized.append(
            {
                "symbol": symbol,
                "signal_side": side.lower(),
                "expected_price": _to_float(
                    item.get("expected_price") or item.get("price"), 0.0
                ),
                "fusion_score": _to_float(
                    item.get("fusion_score") or item.get("score"), 0.0
                ),
                "reason": str(item.get("reason") or item.get("remark") or "").strip(),
                "trade_action": str(item.get("trade_action") or "").strip().upper(),
                "position_side": str(item.get("position_side") or "").strip().upper(),
                "is_margin_trade": bool(item.get("is_margin_trade")),
                "source_signal": dict(item),
            }
        )
    return normalized


def _stage_label(stage: str) -> str:
    mapping = {
        "queued": "排队中",
        "validating": "校验中",
        "signal_loading": "加载信号",
        "dispatching": "派发订单",
        "running": "执行中",
        "completed": "已完成",
        "failed": "已失败",
    }
    return mapping.get(str(stage or "").strip().lower(), str(stage or ""))


def _error_stage_label(stage: str) -> str:
    mapping = {
        "validating": "任务校验",
        "signal_loading": "信号加载",
        "dispatching": "信号派发",
        "risk_check": "风控校验",
        "order_submit": "订单提交",
        "portfolio_lookup": "账户/组合查询",
        "strategy_loading": "策略加载",
        "unexpected": "未知异常",
    }
    return mapping.get(str(stage or "").strip().lower(), str(stage or ""))


def _shift_trading_sessions(start_date: date, sessions: int) -> date:
    if xcals is None:
        offset = pd.tseries.offsets.BDay(max(0, sessions))
        return (pd.Timestamp(start_date) + offset).date()
    cal = xcals.get_calendar("XSHG")
    session = cal.date_to_session(pd.Timestamp(start_date), direction="previous")
    for _ in range(max(0, sessions)):
        session = cal.next_session(session)
    return session.date() if hasattr(session, "date") else session


class ManualExecutionService:
    """手动执行任务服务。"""

    def __init__(self) -> None:
        self._strategy_storage = get_strategy_storage_service()

    @staticmethod
    def _build_task_label(task_type: str) -> str:
        return (
            "自动托管任务"
            if str(task_type or "").strip().lower() == "hosted"
            else "手动执行任务"
        )

    @staticmethod
    def _build_strategy_snapshot(
        *,
        strategy: dict[str, Any] | None,
        execution_config: dict[str, Any] | None = None,
        live_trade_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        snapshot: dict[str, Any] = {}
        if isinstance(strategy, dict):
            snapshot["strategy"] = {
                "id": strategy.get("id"),
                "name": strategy.get("name"),
                "is_verified": bool(strategy.get("is_verified")),
                "parameters": strategy.get("parameters") or {},
            }
        if isinstance(execution_config, dict):
            snapshot["execution_config"] = execution_config
        if isinstance(live_trade_config, dict):
            snapshot["live_trade_config"] = live_trade_config
        return snapshot

    async def _load_latest_account_snapshot(
        self, *, tenant_id: str, user_id: str
    ) -> dict[str, Any] | None:
        async with get_session(read_only=True) as session:
            from backend.services.trade.routers.real_trading_utils import (
                _fetch_latest_real_account_snapshot,
            )

            return await _fetch_latest_real_account_snapshot(
                session,
                tenant_id=tenant_id,
                user_id=user_id,
            )

    async def _load_user_default_model_record(
        self, *, tenant_id: str, user_id: str
    ) -> dict[str, Any] | None:
        async with get_session(read_only=True) as session:
            row = (
                (
                    await session.execute(
                        text(
                            """
                        SELECT model_id, metadata_json, status, activated_at, updated_at
                        FROM qm_user_models
                        WHERE tenant_id = :tenant_id
                          AND user_id = :user_id
                          AND is_default = TRUE
                          AND status IN ('ready', 'active')
                          AND COALESCE((metadata_json->>'system_default')::boolean, FALSE) = FALSE
                        ORDER BY activated_at DESC NULLS LAST, updated_at DESC
                        LIMIT 1
                        """
                        ),
                        {"tenant_id": tenant_id, "user_id": user_id},
                    )
                )
                .mappings()
                .first()
            )
        return dict(row) if row else None

    async def _load_latest_default_model_inference_run(
        self, *, tenant_id: str, user_id: str, model_id: str
    ) -> dict[str, Any] | None:
        async with get_session(read_only=True) as session:
            row = (
                (
                    await session.execute(
                        text(
                            """
                        SELECT *
                        FROM qm_model_inference_runs
                        WHERE tenant_id = :tenant_id
                          AND user_id = :user_id
                          AND model_id = :model_id
                          AND status = 'completed'
                        ORDER BY prediction_trade_date DESC, created_at DESC
                        LIMIT 1
                        """
                        ),
                        {
                            "tenant_id": tenant_id,
                            "user_id": user_id,
                            "model_id": model_id,
                        },
                    )
                )
                .mappings()
                .first()
            )
        return dict(row) if row else None

    def _resolve_hosted_execution_window(
        self,
        *,
        data_trade_date: date,
        target_horizon_days: int,
    ) -> tuple[date, date]:
        start_date = _shift_trading_sessions(data_trade_date, 1)
        deadline = _shift_trading_sessions(data_trade_date, max(1, target_horizon_days))
        return start_date, deadline

    async def _persist_task(
        self,
        *,
        prepared: PreparedManualExecution,
        task_id: str,
        request_payload: dict[str, Any],
        created_at: datetime,
        task_type: str,
        task_source: str,
        trigger_mode: str,
        trigger_context: dict[str, Any] | None = None,
        strategy_snapshot: dict[str, Any] | None = None,
        parent_runtime_id: str | None = None,
        initial_summary: dict[str, Any] | None = None,
        initial_line: str | None = None,
        status: str = "queued",
        stage: str = "queued",
        result_payload: dict[str, Any] | None = None,
        progress: int = 0,
    ) -> dict[str, Any]:
        await manual_execution_persistence.create_task(
            task_id=task_id,
            tenant_id=prepared.tenant_id,
            user_id=prepared.user_id,
            strategy_id=prepared.strategy_id,
            strategy_name=prepared.strategy_name,
            run_id=prepared.run_id,
            model_id=prepared.model_id,
            prediction_trade_date=prepared.prediction_trade_date,
            trading_mode=prepared.trading_mode,
            request_payload=request_payload,
            created_at=created_at,
            task_type=task_type,
            task_source=task_source,
            trigger_mode=trigger_mode,
            trigger_context=trigger_context,
            strategy_snapshot=strategy_snapshot,
            parent_runtime_id=parent_runtime_id,
        )
        if status != "queued" or stage != "queued" or result_payload is not None:
            await manual_execution_persistence.update_task(
                task_id=task_id,
                status=status,
                stage=stage,
                progress=progress,
                signal_count=_to_int((initial_summary or {}).get("signal_count"), 0),
                order_count=_to_int((initial_summary or {}).get("sell_order_count"), 0)
                + _to_int((initial_summary or {}).get("buy_order_count"), 0),
                success_count=_to_int(
                    result_payload.get("success_count")
                    if isinstance(result_payload, dict)
                    else 0,
                    0,
                ),
                failed_count=_to_int(
                    result_payload.get("failed_count")
                    if isinstance(result_payload, dict)
                    else 0,
                    0,
                ),
                result_payload=result_payload,
            )
        manual_execution_log_stream.update_state(
            task_id=task_id,
            tenant_id=prepared.tenant_id,
            user_id=prepared.user_id,
            stage=stage,
            status=status,
            progress=progress,
            signal_count=_to_int((initial_summary or {}).get("signal_count"), 0),
            order_count=_to_int((initial_summary or {}).get("sell_order_count"), 0)
            + _to_int((initial_summary or {}).get("buy_order_count"), 0),
            success_count=_to_int(
                result_payload.get("success_count")
                if isinstance(result_payload, dict)
                else 0,
                0,
            ),
            failed_count=_to_int(
                result_payload.get("failed_count")
                if isinstance(result_payload, dict)
                else 0,
                0,
            ),
            summary=initial_summary or {},
            last_line=initial_line,
            error_stage=result_payload.get("error_stage")
            if isinstance(result_payload, dict)
            else None,
            error_message=result_payload.get("error")
            if isinstance(result_payload, dict)
            else None,
        )
        if initial_line:
            manual_execution_log_stream.append_log(
                task_id=task_id,
                tenant_id=prepared.tenant_id,
                user_id=prepared.user_id,
                level="info" if status != "failed" else "warning",
                stage=stage,
                status=status,
                progress=progress,
                line=initial_line,
                summary=initial_summary or {},
            )
        task = await manual_execution_persistence.get_task(
            task_id,
            user_id=prepared.user_id,
            tenant_id=prepared.tenant_id,
        )
        return task or {}

    async def _load_inference_run(
        self, *, tenant_id: str, user_id: str, run_id: str
    ) -> dict[str, Any] | None:
        async with get_session(read_only=True) as session:
            row = (
                (
                    await session.execute(
                        text(
                            """
                        SELECT *
                        FROM qm_model_inference_runs
                        WHERE run_id = :run_id
                          AND tenant_id = :tenant_id
                          AND user_id = :user_id
                        LIMIT 1
                        """
                        ),
                        {"run_id": run_id, "tenant_id": tenant_id, "user_id": user_id},
                    )
                )
                .mappings()
                .first()
            )
        return dict(row) if row else None

    async def get_default_model_hosted_status(
        self, *, tenant_id: str, user_id: str
    ) -> dict[str, Any]:
        tenant = (tenant_id or "").strip() or "default"
        uid = str(user_id or "").strip()
        default_model = await self._load_user_default_model_record(
            tenant_id=tenant, user_id=uid
        )
        if not default_model:
            return {
                "available": False,
                "source": "missing",
                "reason_code": "missing_default_model",
                "message": "未找到当前用户的默认模型，请先设置可用的用户默认模型",
            }

        default_model_id = str(default_model.get("model_id") or "").strip()
        model_meta_raw = default_model.get("metadata_json")
        if isinstance(model_meta_raw, str):
            try:
                model_meta = json.loads(model_meta_raw)
            except Exception:
                model_meta = {}
        elif isinstance(model_meta_raw, dict):
            model_meta = model_meta_raw
        else:
            model_meta = {}
        target_horizon_days = _to_int(
            model_meta.get("target_horizon_days")
            if isinstance(model_meta, dict)
            else None,
            5,
        )
        if target_horizon_days <= 0:
            target_horizon_days = 5

        latest_run = await self._load_latest_default_model_inference_run(
            tenant_id=tenant, user_id=uid, model_id=default_model_id
        )
        if not latest_run:
            return {
                "available": False,
                "source": "missing",
                "reason_code": "missing_latest_run",
                "message": "未找到当前用户默认模型的最新完成推理数据",
                "latest_default_model_id": default_model_id,
                "target_horizon_days": target_horizon_days,
            }

        if bool(latest_run.get("fallback_used")):
            return {
                "available": False,
                "source": "fallback",
                "reason_code": "fallback_used",
                "message": "当前默认模型最新推理数据来自兜底结果，自动托管已禁止使用兜底数据",
                "latest_default_model_id": default_model_id,
                "latest_run_id": str(latest_run.get("run_id") or "").strip() or None,
                "target_horizon_days": target_horizon_days,
                "data_trade_date": str(latest_run.get("data_trade_date") or ""),
                "prediction_trade_date": str(
                    latest_run.get("prediction_trade_date") or ""
                ),
            }
        latest_model_source = str(latest_run.get("model_source") or "").strip()
        allowed_sources = {"user_default", "explicit_system_model"}
        if latest_model_source not in allowed_sources:
            return {
                "available": False,
                "source": "mismatch",
                "reason_code": "source_mismatch",
                "message": "当前默认模型最新推理数据不是用户默认模型来源，自动托管已拒绝",
                "latest_default_model_id": default_model_id,
                "latest_run_id": str(latest_run.get("run_id") or "").strip() or None,
                "target_horizon_days": target_horizon_days,
                "data_trade_date": str(latest_run.get("data_trade_date") or ""),
                "prediction_trade_date": str(
                    latest_run.get("prediction_trade_date") or ""
                ),
            }

        data_trade_date = _parse_iso_date(latest_run.get("data_trade_date"))
        prediction_trade_date = _parse_iso_date(latest_run.get("prediction_trade_date"))
        generation_start, execution_deadline = self._resolve_hosted_execution_window(
            data_trade_date=data_trade_date,
            target_horizon_days=target_horizon_days,
        )
        current_trade_date = datetime.now(_SH_TZ).date()
        if current_trade_date < generation_start:
            return {
                "available": False,
                "source": "window_pending",
                "reason_code": "window_pending",
                "message": (
                    "当前默认模型最新推理结果尚未进入可执行窗口，"
                    f"开始日期={generation_start.isoformat()}"
                ),
                "latest_default_model_id": default_model_id,
                "latest_run_id": str(latest_run.get("run_id") or "").strip() or None,
                "target_horizon_days": target_horizon_days,
                "data_trade_date": data_trade_date.isoformat(),
                "prediction_trade_date": prediction_trade_date.isoformat(),
                "execution_window_start": generation_start.isoformat(),
                "execution_window_end": execution_deadline.isoformat(),
            }
        if current_trade_date > execution_deadline:
            return {
                "available": False,
                "source": "expired",
                "reason_code": "window_expired",
                "message": (
                    "当前默认模型最新推理结果已超过可执行窗口，"
                    f"截止日期={execution_deadline.isoformat()}"
                ),
                "latest_default_model_id": default_model_id,
                "latest_run_id": str(latest_run.get("run_id") or "").strip() or None,
                "target_horizon_days": target_horizon_days,
                "data_trade_date": data_trade_date.isoformat(),
                "prediction_trade_date": prediction_trade_date.isoformat(),
                "execution_window_start": generation_start.isoformat(),
                "execution_window_end": execution_deadline.isoformat(),
            }

        return {
            "available": True,
            "source": latest_model_source or "user_default",
            "reason_code": "ready",
            "message": "当前默认模型最新推理结果可用于自动托管",
            "latest_default_model_id": default_model_id,
            "latest_run_id": str(latest_run.get("run_id") or "").strip() or None,
            "target_horizon_days": target_horizon_days,
            "data_trade_date": data_trade_date.isoformat(),
            "prediction_trade_date": prediction_trade_date.isoformat(),
            "execution_window_start": generation_start.isoformat(),
            "execution_window_end": execution_deadline.isoformat(),
            "fallback_used": bool(latest_run.get("fallback_used")),
            "model_source": latest_model_source or None,
        }

    async def _load_signal_rows(
        self, *, tenant_id: str, user_id: str, run_id: str
    ) -> list[dict[str, Any]]:
        async with get_session(read_only=True) as session:
            rows = (
                (
                    await session.execute(
                        text(
                            """
                        SELECT symbol, fusion_score, light_score, tft_score, score_rank,
                               signal_side, expected_price, quality, created_at
                        FROM engine_signal_scores
                        WHERE run_id = :run_id
                          AND tenant_id = :tenant_id
                          AND user_id = :user_id
                        ORDER BY fusion_score DESC NULLS LAST, symbol ASC
                        """
                        ),
                        {"run_id": run_id, "tenant_id": tenant_id, "user_id": user_id},
                    )
                )
                .mappings()
                .all()
            )
        normalized: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row or {})
            if item.get("symbol"):
                item["symbol"] = _normalize_to_broker_symbol(item["symbol"])
            if item.get("created_at") is not None:
                item["created_at"] = item["created_at"].isoformat()
            normalized.append(item)
        return normalized

    async def _ensure_active_portfolio_exists(
        self, *, tenant_id: str, user_id: str, strategy_id: str
    ) -> dict[str, Any]:
        from backend.services.trade.routers.real_trading_utils import (
            _fetch_active_portfolio_snapshot,
        )

        async with get_session(read_only=True) as session:
            snapshot = await _fetch_active_portfolio_snapshot(
                session,
                tenant_id=tenant_id,
                user_id=user_id,
                strategy_id=strategy_id,
                mode="REAL",
            )
        if not snapshot:
            raise HTTPException(
                status_code=400,
                detail="当前未发现可用的实盘组合，请先启动实盘策略或完成组合初始化",
            )
        return snapshot

    async def prepare_manual_execution(
        self,
        *,
        tenant_id: str,
        user_id: str,
        run_id: str,
        strategy_id: str,
        model_id: str | None = None,
        trading_mode: Any,
        note: str | None = None,
    ) -> PreparedManualExecution:
        tenant = (tenant_id or "").strip() or "default"
        uid = str(user_id or "").strip()
        rid = str(run_id or "").strip()
        sid = str(strategy_id or "").strip()
        mode = _normalize_trading_mode(trading_mode)

        if not rid:
            raise HTTPException(status_code=400, detail="run_id 不能为空")
        if not sid:
            raise HTTPException(status_code=400, detail="strategy_id 不能为空")

        run = await self._load_inference_run(tenant_id=tenant, user_id=uid, run_id=rid)
        if not run:
            raise HTTPException(status_code=404, detail="推理批次不存在")
        if str(run.get("status") or "").lower() != "completed":
            raise HTTPException(status_code=400, detail="仅允许执行已完成的推理批次")
        if model_id and str(run.get("model_id") or "").strip() != str(model_id).strip():
            raise HTTPException(status_code=400, detail="run_id 与当前选择模型不匹配")

        strategy = await self._strategy_storage.get(sid, uid)
        if not strategy:
            raise HTTPException(status_code=404, detail="策略不存在")
        if not bool(strategy.get("is_verified")):
            raise HTTPException(status_code=400, detail="仅允许执行已验证策略")

        strategy_name = (
            str(strategy.get("name") or f"strategy_{sid}").strip() or f"strategy_{sid}"
        )
        request_payload = {
            "tenant_id": tenant,
            "user_id": uid,
            "run_id": rid,
            "strategy_id": sid,
            "strategy_name": strategy_name,
            "trading_mode": mode,
            "note": note,
        }

        return PreparedManualExecution(
            task_id="",
            tenant_id=tenant,
            user_id=uid,
            strategy_id=sid,
            strategy_name=strategy_name,
            run_id=rid,
            model_id=str(run.get("model_id") or ""),
            prediction_trade_date=_parse_iso_date(run.get("prediction_trade_date")),
            trading_mode=mode,
            request_payload=request_payload,
            run=run,
            strategy=strategy,
        )

    async def build_execution_preview(
        self,
        *,
        tenant_id: str,
        user_id: str,
        model_id: str,
        run_id: str,
        strategy_id: str,
        trading_mode: Any,
        note: str | None = None,
    ) -> dict[str, Any]:
        mode = _normalize_trading_mode(trading_mode)
        if mode != "REAL":
            raise HTTPException(
                status_code=400, detail="引导式手动任务首版仅支持 REAL 模式"
            )

        prepared = await self.prepare_manual_execution(
            tenant_id=tenant_id,
            user_id=user_id,
            run_id=run_id,
            strategy_id=strategy_id,
            model_id=model_id,
            trading_mode=mode,
            note=note,
        )
        async with get_session(read_only=True) as session:
            from backend.services.trade.routers.real_trading_utils import (
                _fetch_latest_real_account_snapshot,
            )

            account_snapshot = await _fetch_latest_real_account_snapshot(
                session,
                tenant_id=prepared.tenant_id,
                user_id=prepared.user_id,
            )
        if not account_snapshot:
            raise HTTPException(
                status_code=400,
                detail="未检测到最新实盘账户快照，请先确认 QMT Agent 已上报账户数据",
            )

        signal_rows = await self._load_signal_rows(
            tenant_id=prepared.tenant_id,
            user_id=prepared.user_id,
            run_id=prepared.run_id,
        )
        if not signal_rows:
            raise HTTPException(status_code=400, detail="当前推理批次无可用信号明细")

        strategy_params = _normalize_strategy_params(prepared.strategy)
        plan = _build_execution_plan_from_signals(
            signal_rows=signal_rows,
            strategy_params=strategy_params,
            account_snapshot=account_snapshot,
        )
        if not plan["sell_orders"] and not plan["buy_orders"]:
            raise HTTPException(status_code=400, detail="策略计算后无可执行调仓动作")

        preview = {
            "account_snapshot": {
                "account_id": str(account_snapshot.get("account_id") or ""),
                "snapshot_at": account_snapshot.get("snapshot_at"),
                "total_asset": round(
                    _to_float(account_snapshot.get("total_asset"), 0.0), 2
                ),
                "available_cash": round(
                    _to_float(
                        account_snapshot.get("available_cash")
                        or account_snapshot.get("cash"),
                        0.0,
                    ),
                    2,
                ),
                "market_value": round(
                    _to_float(account_snapshot.get("market_value"), 0.0), 2
                ),
                "position_count": _to_int(
                    account_snapshot.get("position_count"),
                    len(_normalize_positions(account_snapshot.get("positions")).keys()),
                ),
            },
            "strategy_context": {
                "model_id": prepared.model_id,
                "run_id": prepared.run_id,
                "prediction_trade_date": prepared.prediction_trade_date.isoformat(),
                "strategy_id": prepared.strategy_id,
                "strategy_name": prepared.strategy_name,
                "trading_mode": prepared.trading_mode,
                "strategy_params": strategy_params,
                "note": note,
            },
            "sell_orders": plan["sell_orders"],
            "buy_orders": plan["buy_orders"],
            "skipped_items": plan["skipped_items"],
            "summary": plan["summary"],
        }
        preview["preview_hash"] = _build_preview_hash(preview)
        return preview

    async def create_manual_task(
        self,
        *,
        tenant_id: str,
        user_id: str,
        model_id: str,
        run_id: str,
        strategy_id: str,
        trading_mode: Any,
        preview_hash: str | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        if preview_hash:
            return await self.submit_execution_plan(
                tenant_id=tenant_id,
                user_id=user_id,
                model_id=model_id,
                run_id=run_id,
                strategy_id=strategy_id,
                trading_mode=trading_mode,
                preview_hash=preview_hash,
                note=note,
            )

        prepared = await self.prepare_manual_execution(
            tenant_id=tenant_id,
            user_id=user_id,
            run_id=run_id,
            strategy_id=strategy_id,
            model_id=model_id,
            trading_mode=trading_mode,
            note=note,
        )
        task_id = f"manual_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}_{uuid4().hex[:8]}"
        created_at = datetime.now(timezone.utc)
        task = await self._persist_task(
            prepared=prepared,
            task_id=task_id,
            request_payload=prepared.request_payload,
            created_at=created_at,
            task_type="manual",
            task_source="manual_page",
            trigger_mode="manual",
            strategy_snapshot=self._build_strategy_snapshot(strategy=prepared.strategy),
            initial_summary={
                "run_id": prepared.run_id,
                "model_id": prepared.model_id,
                "strategy_id": prepared.strategy_id,
                "strategy_name": prepared.strategy_name,
                "prediction_trade_date": prepared.prediction_trade_date.isoformat(),
                "trading_mode": prepared.trading_mode,
                "signal_count": int(prepared.run.get("signals_count") or 0),
            },
            initial_line=(
                f"手动执行任务已创建: run_id={prepared.run_id} "
                f"strategy={prepared.strategy_name} mode={prepared.trading_mode}"
            ),
        )
        return {
            "task_id": task_id,
            "status": "queued",
            "task": task,
        }

    async def _cancel_previous_manual_tasks(
        self,
        *,
        user_id: str,
        tenant_id: str,
        run_id: str,
        strategy_id: str,
    ) -> None:
        """新任务提交前，取消同 run_id+strategy_id 的历史任务并删除其 pending 订单。
        只删除尚未提交到券商的订单（status=pending），已提交/成交的订单保留审计记录。
        """
        try:
            async with get_session() as db:
                # 1. 查询同 run_id + strategy_id 的历史任务（所有状态）
                rows = await db.execute(
                    text("""
                        SELECT task_id, status
                        FROM trade_manual_execution_tasks
                        WHERE user_id = :user_id
                          AND tenant_id = :tenant_id
                          AND run_id = :run_id
                          AND strategy_id = :strategy_id
                    """),
                    {
                        "user_id": user_id,
                        "tenant_id": tenant_id,
                        "run_id": run_id,
                        "strategy_id": strategy_id,
                    },
                )
                prev_tasks = rows.fetchall()
                if not prev_tasks:
                    return

                prev_task_ids = [str(r[0]) for r in prev_tasks]
                non_terminal = [
                    str(r[0])
                    for r in prev_tasks
                    if str(r[1] or "") not in {"completed", "failed", "cancelled"}
                ]

                # 2. 取消非终态历史任务
                if non_terminal:
                    await db.execute(
                        text("""
                            UPDATE trade_manual_execution_tasks
                            SET status = 'cancelled', stage = 'cancelled',
                                error_stage = 'cancelled',
                                error_message = '已被新提交任务自动取消'
                            WHERE task_id = ANY(:ids)
                        """),
                        {"ids": non_terminal},
                    )

                # 3. 删除历史任务的 pending 订单（client_order_id 以 manual- 开头，确保只删手动任务单）
                # 保留已提交/成交订单的审计记录
                prefix_conditions = " OR ".join(
                    [
                        f"client_order_id LIKE :prefix_{i}"
                        for i in range(len(prev_task_ids))
                    ]
                )
                prefix_params = {
                    f"prefix_{i}": f"manual-{tid[-8:]}%"
                    for i, tid in enumerate(prev_task_ids)
                }
                await db.execute(
                    text(f"""
                        DELETE FROM orders
                        WHERE user_id = :user_id
                          AND tenant_id = :tenant_id
                          AND status = 'pending'
                          AND ({prefix_conditions})
                    """),
                    {"user_id": user_id, "tenant_id": tenant_id, **prefix_params},
                )

                await db.commit()
                logger.info(
                    "清理历史手动任务: user=%s run_id=%s strategy_id=%s "
                    "cancelled=%d total_prev=%d",
                    user_id,
                    run_id,
                    strategy_id,
                    len(non_terminal),
                    len(prev_task_ids),
                )
        except Exception as exc:
            # 清理失败不阻断新任务创建，只记录警告
            logger.warning("清理历史手动任务失败（不影响新任务）: %s", exc)

    async def submit_execution_plan(
        self,
        *,
        tenant_id: str,
        user_id: str,
        model_id: str,
        run_id: str,
        strategy_id: str,
        trading_mode: Any,
        preview_hash: str,
        note: str | None = None,
    ) -> dict[str, Any]:
        preview = await self.build_execution_preview(
            tenant_id=tenant_id,
            user_id=user_id,
            model_id=model_id,
            run_id=run_id,
            strategy_id=strategy_id,
            trading_mode=trading_mode,
            note=note,
        )
        if (
            str(preview.get("preview_hash") or "").strip()
            != str(preview_hash or "").strip()
        ):
            raise HTTPException(
                status_code=409, detail="预览结果已失效，请重新计算调仓预案后再提交"
            )

        prepared = await self.prepare_manual_execution(
            tenant_id=tenant_id,
            user_id=user_id,
            run_id=run_id,
            strategy_id=strategy_id,
            model_id=model_id,
            trading_mode=trading_mode,
            note=note,
        )
        task_id = f"manual_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}_{uuid4().hex[:8]}"
        created_at = datetime.now(timezone.utc)
        preview_summary = preview.get("summary") or {}
        request_payload = {
            **prepared.request_payload,
            "preview_hash": preview_hash,
            "execution_plan": {
                "sell_orders": preview.get("sell_orders") or [],
                "buy_orders": preview.get("buy_orders") or [],
                "skipped_items": preview.get("skipped_items") or [],
                "summary": preview_summary,
            },
        }

        # 新任务下达前清理同用户同 run_id+strategy_id 的历史任务及其 pending 订单
        await self._cancel_previous_manual_tasks(
            user_id=prepared.user_id,
            tenant_id=prepared.tenant_id,
            run_id=prepared.run_id,
            strategy_id=prepared.strategy_id,
        )

        task = await self._persist_task(
            prepared=prepared,
            task_id=task_id,
            request_payload=request_payload,
            created_at=created_at,
            task_type="manual",
            task_source="manual_page",
            trigger_mode="manual",
            strategy_snapshot=self._build_strategy_snapshot(strategy=prepared.strategy),
            initial_summary={
                "run_id": prepared.run_id,
                "model_id": prepared.model_id,
                "strategy_id": prepared.strategy_id,
                "strategy_name": prepared.strategy_name,
                "prediction_trade_date": prepared.prediction_trade_date.isoformat(),
                "trading_mode": prepared.trading_mode,
                "preview_summary": preview_summary,
                "signal_count": _to_int(
                    preview_summary.get("signal_count"),
                    int(prepared.run.get("signals_count") or 0),
                ),
                "buy_order_count": _to_int(preview_summary.get("buy_order_count"), 0),
                "sell_order_count": _to_int(preview_summary.get("sell_order_count"), 0),
            },
            initial_line=(
                f"调仓预案已确认并创建执行任务: run_id={prepared.run_id} "
                f"strategy={prepared.strategy_name} "
                f"buy={_to_int(preview_summary.get('buy_order_count'), 0)} "
                f"sell={_to_int(preview_summary.get('sell_order_count'), 0)}"
            ),
        )
        return {
            "task_id": task_id,
            "status": "queued",
            "task": task,
            "preview_summary": preview_summary,
        }

    async def list_tasks(
        self,
        *,
        tenant_id: str,
        user_id: str,
        limit: int = 20,
        task_type: str | None = None,
        task_source: str | None = None,
        active_runtime_id: str | None = None,
    ) -> dict[str, Any]:
        items = await manual_execution_persistence.list_tasks(
            tenant_id=tenant_id,
            user_id=user_id,
            limit=limit,
            task_type=task_type,
            task_source=task_source,
            parent_runtime_id=active_runtime_id,
        )
        return {
            "items": items,
            "total": len(items),
            "limit": limit,
        }

    async def get_latest_hosted_task(
        self,
        *,
        tenant_id: str,
        user_id: str,
        active_runtime_id: str | None = None,
    ) -> dict[str, Any] | None:
        items = await manual_execution_persistence.list_tasks(
            tenant_id=tenant_id,
            user_id=user_id,
            limit=1,
            task_type="hosted",
            parent_runtime_id=active_runtime_id,
        )
        if items:
            return items[0]
        if active_runtime_id:
            fallback = await manual_execution_persistence.list_tasks(
                tenant_id=tenant_id,
                user_id=user_id,
                limit=1,
                task_type="hosted",
            )
            return fallback[0] if fallback else None
        return None

    async def create_hosted_task(
        self,
        *,
        tenant_id: str,
        user_id: str,
        run_id: str | None = None,
        strategy_id: str,
        trading_mode: Any,
        execution_config: dict[str, Any] | None = None,
        live_trade_config: dict[str, Any] | None = None,
        trigger_context: dict[str, Any] | None = None,
        parent_runtime_id: str | None = None,
        note: str | None = None,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        tenant = (tenant_id or "").strip() or "default"
        uid = str(user_id or "").strip()
        sid = str(strategy_id or "").strip()
        mode = _normalize_trading_mode(trading_mode)
        provided_task_id = str(task_id or "").strip()
        if provided_task_id:
            existing_task = await manual_execution_persistence.get_task_any(
                provided_task_id
            )
            if existing_task:
                return {
                    "task_id": provided_task_id,
                    "status": str(existing_task.get("status") or "completed"),
                    "task": existing_task,
                    "noop": True,
                    "duplicate": True,
                }

        hosted_status = await self.get_default_model_hosted_status(
            tenant_id=tenant, user_id=uid
        )
        if not bool(hosted_status.get("available")):
            raise HTTPException(
                status_code=409,
                detail=str(
                    hosted_status.get("message")
                    or "当前默认模型最新推理不可用于自动托管"
                ),
            )

        default_model_id = str(
            hosted_status.get("latest_default_model_id") or ""
        ).strip()
        target_horizon_days = _to_int(hosted_status.get("target_horizon_days"), 5)
        generation_start = _parse_iso_date(hosted_status.get("execution_window_start"))
        execution_deadline = _parse_iso_date(hosted_status.get("execution_window_end"))
        latest_run_id = str(hosted_status.get("latest_run_id") or "").strip()
        task_id = (
            provided_task_id
            or f"hosted_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}_{uuid4().hex[:8]}"
        )

        prepared = await self.prepare_manual_execution(
            tenant_id=tenant,
            user_id=uid,
            run_id=latest_run_id,
            strategy_id=sid,
            model_id=default_model_id,
            trading_mode=mode,
            note=note,
        )
        latest_snapshot = await self._load_latest_account_snapshot(
            tenant_id=prepared.tenant_id,
            user_id=prepared.user_id,
        )
        if not latest_snapshot:
            raise HTTPException(
                status_code=400,
                detail="未检测到最新实盘账户快照，请先确认 QMT Agent 已上报账户数据",
            )
        normalized_signals = await self._load_signal_rows(
            tenant_id=prepared.tenant_id,
            user_id=prepared.user_id,
            run_id=prepared.run_id,
        )
        strategy_params = _normalize_strategy_params(prepared.strategy)
        execution_plan = _build_execution_plan_from_signals(
            signal_rows=normalized_signals,
            strategy_params=strategy_params,
            account_snapshot=latest_snapshot,
        )
        created_at = datetime.now(timezone.utc)
        plan_summary = execution_plan.get("summary") or {}
        request_payload = {
            **prepared.request_payload,
            "signals": normalized_signals,
            "trigger_context": trigger_context or {},
            "execution_plan": execution_plan,
            "note": note,
            "source_run_id": prepared.run_id,
            "target_horizon_days": target_horizon_days,
            "execution_window": {
                "start": generation_start.isoformat(),
                "end": execution_deadline.isoformat(),
            },
        }
        strategy_snapshot = self._build_strategy_snapshot(
            strategy=prepared.strategy,
            execution_config=execution_config,
            live_trade_config=live_trade_config,
        )
        actionable_orders = list(execution_plan.get("sell_orders") or []) + list(
            execution_plan.get("buy_orders") or []
        )
        common_summary = {
            "task_type": "hosted",
            "task_source": "hosted_runner",
            "trigger_mode": "schedule",
            "run_id": prepared.run_id,
            "model_id": prepared.model_id,
            "strategy_id": prepared.strategy_id,
            "strategy_name": prepared.strategy_name,
            "prediction_trade_date": prepared.prediction_trade_date.isoformat(),
            "trading_mode": prepared.trading_mode,
            "trigger_context": trigger_context or {},
            "preview_summary": plan_summary,
            "signal_count": _to_int(
                plan_summary.get("signal_count"), len(normalized_signals)
            ),
            "buy_order_count": _to_int(plan_summary.get("buy_order_count"), 0),
            "sell_order_count": _to_int(plan_summary.get("sell_order_count"), 0),
            "skipped_count": _to_int(plan_summary.get("skipped_count"), 0),
            "target_horizon_days": target_horizon_days,
            "execution_window_start": generation_start.isoformat(),
            "execution_window_end": execution_deadline.isoformat(),
            "latest_default_model_id": default_model_id,
        }
        if not actionable_orders:
            result_payload = {
                "success": True,
                "task_id": task_id,
                "run_id": prepared.run_id,
                "model_id": prepared.model_id,
                "strategy_id": prepared.strategy_id,
                "strategy_name": prepared.strategy_name,
                "prediction_trade_date": prepared.prediction_trade_date.isoformat(),
                "trading_mode": prepared.trading_mode,
                "signal_count": _to_int(
                    plan_summary.get("signal_count"), len(normalized_signals)
                ),
                "order_count": 0,
                "success_count": 0,
                "failed_count": 0,
                "noop": True,
                "preview_summary": plan_summary,
                "stage_label": _stage_label("completed"),
            }
            task = await self._persist_task(
                prepared=prepared,
                task_id=task_id,
                request_payload=request_payload,
                created_at=created_at,
                task_type="hosted",
                task_source="hosted_runner",
                trigger_mode="schedule",
                trigger_context=trigger_context,
                strategy_snapshot=strategy_snapshot,
                parent_runtime_id=parent_runtime_id,
                initial_summary=common_summary,
                initial_line=(
                    f"自动托管任务已创建但本轮无可执行委托: run_id={prepared.run_id} "
                    f"strategy={prepared.strategy_name}"
                ),
                status="completed",
                stage="completed",
                result_payload=result_payload,
                progress=100,
            )
            return {
                "task_id": task_id,
                "status": "completed",
                "task": task,
                "noop": True,
            }

        task = await self._persist_task(
            prepared=prepared,
            task_id=task_id,
            request_payload=request_payload,
            created_at=created_at,
            task_type="hosted",
            task_source="hosted_runner",
            trigger_mode="schedule",
            trigger_context=trigger_context,
            strategy_snapshot=strategy_snapshot,
            parent_runtime_id=parent_runtime_id,
            initial_summary=common_summary,
            initial_line=(
                f"自动托管任务已创建: run_id={prepared.run_id} strategy={prepared.strategy_name} "
                f"buy={_to_int(plan_summary.get('buy_order_count'), 0)} "
                f"sell={_to_int(plan_summary.get('sell_order_count'), 0)}"
            ),
        )
        return {"task_id": task_id, "status": "queued", "task": task, "noop": False}

    async def clear_history(self, *, tenant_id: str, user_id: str) -> dict[str, Any]:
        """清除该用户的所有手动执行历史。"""
        count = await manual_execution_persistence.clear_tasks(
            tenant_id=tenant_id, user_id=user_id
        )
        logger.info(
            "[%s:%s] Cleared %d manual execution tasks", tenant_id, user_id, count
        )
        return {"cleared_count": count}

    async def get_task(
        self, *, tenant_id: str, user_id: str, task_id: str
    ) -> dict[str, Any] | None:
        return await manual_execution_persistence.get_task(
            task_id, user_id=user_id, tenant_id=tenant_id
        )

    async def get_logs(
        self,
        *,
        tenant_id: str,
        user_id: str,
        task_id: str,
        after_id: str = "0-0",
        limit: int = 200,
    ) -> dict[str, Any]:
        task = await manual_execution_persistence.get_task(
            task_id, user_id=user_id, tenant_id=tenant_id
        )
        if not task:
            raise HTTPException(status_code=404, detail="手动执行任务不存在")
        data = manual_execution_log_stream.fetch_entries(
            task_id, after_id=after_id, limit=limit
        )
        data["task"] = task
        return data

    async def process_task(self, task: dict[str, Any]) -> None:
        task_id = str(task.get("task_id") or "").strip()
        tenant_id = str(task.get("tenant_id") or "default").strip() or "default"
        user_id = str(task.get("user_id") or "").strip()
        if not task_id or not user_id:
            return

        status = str(task.get("status") or "")
        if status not in {"queued", "validating", "dispatching", "running"}:
            return

        request_json = task.get("request_json") or {}
        task_type = str(task.get("task_type") or "manual").strip().lower()
        task_label = self._build_task_label(task_type)
        run_id = str(task.get("run_id") or request_json.get("run_id") or "").strip()
        strategy_id = str(
            task.get("strategy_id") or request_json.get("strategy_id") or ""
        ).strip()
        trading_mode = _normalize_trading_mode(
            task.get("trading_mode") or request_json.get("trading_mode") or "REAL"
        )

        manual_execution_log_stream.update_state(
            task_id=task_id,
            tenant_id=tenant_id,
            user_id=user_id,
            stage="validating",
            status="validating",
            progress=0,
            signal_count=int(task.get("signal_count") or 0),
            order_count=int(task.get("order_count") or 0),
            success_count=int(task.get("success_count") or 0),
            failed_count=int(task.get("failed_count") or 0),
            summary=request_json,
        )
        manual_execution_log_stream.append_log(
            task_id=task_id,
            tenant_id=tenant_id,
            user_id=user_id,
            level="info",
            stage="validating",
            status="validating",
            line=f"开始验证{task_label}: run_id={run_id}, strategy_id={strategy_id}, mode={trading_mode}",
        )

        try:
            prepared = await self.prepare_manual_execution(
                tenant_id=tenant_id,
                user_id=user_id,
                run_id=run_id,
                strategy_id=strategy_id,
                trading_mode=trading_mode,
                note=request_json.get("note"),
            )
        except HTTPException as exc:
            await manual_execution_persistence.update_task(
                task_id=task_id,
                status="failed",
                stage="validating",
                error_stage="validating",
                error_message=str(exc.detail or exc),
                result_payload={
                    "success": False,
                    "stage": "validating",
                    "stage_label": _stage_label("validating"),
                    "error_stage": "validating",
                    "error_stage_label": _error_stage_label("validating"),
                    "error": str(exc.detail or exc),
                },
            )
            manual_execution_log_stream.append_log(
                task_id=task_id,
                tenant_id=tenant_id,
                user_id=user_id,
                level="error",
                stage="validating",
                status="failed",
                line=f"任务验证失败: {exc.detail}",
            )
            manual_execution_log_stream.update_state(
                task_id=task_id,
                tenant_id=tenant_id,
                user_id=user_id,
                stage="validating",
                status="failed",
                error_stage="validating",
                error_message=str(exc.detail or exc),
            )
            return

        async with get_session() as db:
            manual_execution_log_stream.append_log(
                task_id=task_id,
                tenant_id=tenant_id,
                user_id=user_id,
                level="info",
                stage="validating",
                line="[链路诊断] 正在检查活跃实盘组合...",
            )

            stmt = (
                select(Portfolio)
                .where(
                    and_(
                        Portfolio.tenant_id == tenant_id,
                        Portfolio.user_id == user_id,
                        Portfolio.status == "active",
                        Portfolio.is_deleted.is_(False),
                    )
                )
                .order_by(Portfolio.updated_at.desc())
                .limit(1)
            )
            portfolio = (await db.execute(stmt)).scalar_one_or_none()
            if not portfolio:
                error_msg = "当前未发现可用的实盘组合，请先启动实盘策略或完成组合初始化"
                manual_execution_log_stream.append_log(
                    task_id=task_id,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    level="error",
                    stage="validating",
                    line=error_msg,
                )
                await manual_execution_persistence.update_task(
                    task_id=task_id,
                    status="failed",
                    stage="validating",
                    error_stage="portfolio_lookup",
                    error_message=error_msg,
                )
                return

            manual_execution_log_stream.append_log(
                task_id=task_id,
                tenant_id=tenant_id,
                user_id=user_id,
                level="info",
                stage="validating",
                line=f"[链路诊断] 发现活跃组合: {portfolio.name} (ID: {portfolio.id})",
            )

            execution_plan = (
                request_json.get("execution_plan")
                if isinstance(request_json.get("execution_plan"), dict)
                else None
            )
            if not execution_plan:
                async with get_session(read_only=True) as account_session:
                    from backend.services.trade.routers.real_trading_utils import (
                        _fetch_latest_real_account_snapshot,
                    )

                    latest_snapshot = await _fetch_latest_real_account_snapshot(
                        account_session,
                        tenant_id=tenant_id,
                        user_id=user_id,
                    )
                if not latest_snapshot:
                    error_msg = (
                        "未检测到最新实盘账户快照，请先确认 QMT Agent 已上报账户数据"
                    )
                    await manual_execution_persistence.update_task(
                        task_id=task_id,
                        status="failed",
                        stage="validating",
                        error_stage="portfolio_lookup",
                        error_message=error_msg,
                    )
                    manual_execution_log_stream.append_log(
                        task_id=task_id,
                        tenant_id=tenant_id,
                        user_id=user_id,
                        level="error",
                        stage="validating",
                        line=error_msg,
                    )
                    return
                signal_rows = await self._load_signal_rows(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    run_id=run_id,
                )
                if not signal_rows:
                    error_msg = "推理结果无可执行信号"
                    await manual_execution_persistence.update_task(
                        task_id=task_id,
                        status="failed",
                        stage="signal_loading",
                        error_stage="signal_loading",
                        error_message=error_msg,
                    )
                    manual_execution_log_stream.append_log(
                        task_id=task_id,
                        tenant_id=tenant_id,
                        user_id=user_id,
                        level="error",
                        stage="signal_loading",
                        status="failed",
                        line=error_msg,
                    )
                    return
                strategy_params = _normalize_strategy_params(prepared.strategy)
                execution_plan = _build_execution_plan_from_signals(
                    signal_rows=signal_rows,
                    strategy_params=strategy_params,
                    account_snapshot=latest_snapshot,
                )

            sell_orders = list(execution_plan.get("sell_orders") or [])
            buy_orders = list(execution_plan.get("buy_orders") or [])
            skipped_items = list(execution_plan.get("skipped_items") or [])
            plan_summary = dict(execution_plan.get("summary") or {})
            actionable_orders = sell_orders + buy_orders
            if not actionable_orders:
                error_msg = "调仓预案无可执行委托"
                await manual_execution_persistence.update_task(
                    task_id=task_id,
                    status="failed",
                    stage="signal_loading",
                    error_stage="signal_loading",
                    error_message=error_msg,
                )
                manual_execution_log_stream.append_log(
                    task_id=task_id,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    level="error",
                    stage="signal_loading",
                    status="failed",
                    line=error_msg,
                )
                return

            processed = 0
            success_count = 0
            failed_count = 0
            first_error = ""
            total = len(actionable_orders)
            submit_index = 0
            submitted_sell_orders: list[dict[str, Any]] = []
            runtime_plan_summary = dict(plan_summary)

            await manual_execution_persistence.update_task(
                task_id=task_id,
                status="dispatching",
                stage="dispatching",
                signal_count=total,
                order_count=0,
                success_count=0,
                failed_count=0,
                progress=0,
            )
            manual_execution_log_stream.update_state(
                task_id=task_id,
                tenant_id=tenant_id,
                user_id=user_id,
                stage="dispatching",
                status="dispatching",
                progress=0,
                signal_count=total,
                order_count=0,
                success_count=0,
                failed_count=0,
            )

            # ─── 链路自检：Agent 连通性探测 (仅 REAL 模式必需) ──────────────────
            if trading_mode == "REAL":
                manual_execution_log_stream.append_log(
                    task_id=task_id,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    level="info",
                    stage="dispatching",
                    line="[链路诊断] 正在探测 QMT Agent 连通性...",
                )
                heartbeat_key = (
                    f"trade:agent:heartbeat:{tenant_id}:{str(user_id).zfill(8)}"
                )
                redis_client = get_redis().client

                hb_raw = redis_client.get(heartbeat_key)

                if not hb_raw:
                    error_msg = f"诊断失败: QMT Agent 离线 (未发现心跳: {heartbeat_key})，请检查 Windows 端服务是否启动"
                    manual_execution_log_stream.append_log(
                        task_id=task_id,
                        tenant_id=tenant_id,
                        user_id=user_id,
                        level="error",
                        stage="dispatching",
                        line=error_msg,
                    )
                    await manual_execution_persistence.update_task(
                        task_id=task_id,
                        status="failed",
                        stage="dispatching",
                        error_stage="dispatching",
                        error_message=error_msg,
                    )
                    return

                manual_execution_log_stream.append_log(
                    task_id=task_id,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    level="info",
                    stage="dispatching",
                    line="[链路诊断] QMT Agent 在线，心跳正常",
                )
                async with get_session(read_only=True) as account_session:
                    from backend.services.trade.routers.real_trading_utils import (
                        _fetch_latest_real_account_snapshot,
                    )

                    latest_snapshot = await _fetch_latest_real_account_snapshot(
                        account_session,
                        tenant_id=tenant_id,
                        user_id=user_id,
                    )
                if not latest_snapshot:
                    manual_execution_log_stream.append_log(
                        task_id=task_id,
                        tenant_id=tenant_id,
                        user_id=user_id,
                        level="warning",
                        stage="dispatching",
                        line="[注意] 未发现 PostgreSQL 账户快照，Agent 可能尚未完成柜台初始化",
                    )
                else:
                    manual_execution_log_stream.append_log(
                        task_id=task_id,
                        tenant_id=tenant_id,
                        user_id=user_id,
                        level="info",
                        stage="dispatching",
                        line="[链路诊断] PostgreSQL 账户快照已就绪，准备派发指令",
                    )

            manual_execution_log_stream.append_log(
                task_id=task_id,
                tenant_id=tenant_id,
                user_id=user_id,
                level="info",
                stage="dispatching",
                status="dispatching",
                line=(
                    f"开始派发{task_label}的调仓预案，共 {total} 条委托 "
                    f"(sell={len(sell_orders)}, buy={len(buy_orders)}, skipped={len(skipped_items)})"
                ),
            )

            async def _submit_one_order(row: dict[str, Any], index: int) -> None:
                nonlocal processed
                nonlocal success_count
                nonlocal failed_count
                nonlocal first_error
                nonlocal submitted_sell_orders

                symbol = str(row.get("symbol") or "").strip().upper()
                fusion_score = _to_float(row.get("fusion_score"), 0.0)
                expected_price = _to_float(row.get("price"), 0.0)
                reference_price = _to_float(row.get("reference_price"), 0.0)
                preview_price = (
                    expected_price if expected_price > 0 else reference_price
                )
                side = str(row.get("side") or "").strip().upper()
                trade_action = (
                    str(
                        row.get("trade_action")
                        or ("BUY_TO_OPEN" if side == "BUY" else "SELL_TO_CLOSE")
                    )
                    .strip()
                    .upper()
                )
                # 手动任务统一改为 Agent 端临门查价，再转成保护限价单送入 QMT。
                protect_price_ratio = _manual_task_agent_protect_price_ratio()
                order_type = "MARKET"
                quantity = _to_int(row.get("quantity"), 0)

                order_payload = {
                    "symbol": symbol,
                    "side": side,
                    "quantity": quantity,
                    "price": 0.0,
                    "client_order_id": f"manual-{task_id[-8:]}-{index:04d}",
                    "order_type": order_type,
                    "trading_mode": trading_mode,
                    "agent_price_mode": "protect_limit",
                    "protect_price_ratio": protect_price_ratio,
                    "remarks": (
                        f"manual_task={task_id} run_id={run_id} "
                        f"fusion_score={fusion_score:.6f} "
                        f"agent_price_mode=protect_limit protect_ratio={protect_price_ratio:.6f} "
                        f"preview_price={preview_price:.4f} "
                        f"reason={str(row.get('reason') or '').strip()}"
                    ),
                    "strategy_id": strategy_id,
                    "trade_action": trade_action,
                    "position_side": "LONG",
                    "is_margin_trade": False,
                }

                manual_execution_log_stream.append_log(
                    task_id=task_id,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    level="info",
                    stage="dispatching",
                    status="running",
                    progress=int((index - 1) * 100 / total),
                    line=(
                        f"[{index}/{total}] 正在提交委托: {side} {symbol} "
                        f"qty={quantity} preview={preview_price:.2f} "
                        f"agent_price_mode=protect_limit protect_ratio={protect_price_ratio:.4f}"
                    ),
                )

                try:
                    # 策略计算与下单
                    from backend.services.trade.services.internal_strategy_dispatcher import (
                        dispatch_internal_strategy_order,
                    )

                    result = await dispatch_internal_strategy_order(
                        order_data=order_payload,
                        user_id=user_id,
                        tenant_id=tenant_id,
                        redis=get_redis(),
                        db=db,
                    )

                    # 严格的状态判断：必须 result["status"] == "success" 且其内部 result["success"] 也是 True
                    # 针对 internal_strategy_dispatcher 的结构进行优化
                    submit_inner_res = result.get("result", {})
                    is_success = (result.get("status") == "success") and (
                        submit_inner_res.get("success") is not False
                    )

                    execution = result.get("execution")
                    order_id = result.get("order_id", "-")

                    if is_success:
                        success_count += 1
                        if side == "SELL":
                            submitted_sell_orders.append(
                                {
                                    "symbol": symbol,
                                    "client_order_id": str(
                                        order_payload.get("client_order_id") or ""
                                    ),
                                    "order_id": str(order_id or ""),
                                    "quantity": quantity,
                                }
                            )
                        line = f"  >> 提交完成: {symbol} | 订单ID: {order_id} | 派发类型: {execution}"
                        level = "info"
                    elif result.get("status") == "rejected":
                        failed_count += 1
                        violations = result.get("violations", [])
                        line = f"  >> [拦截] 风控拒绝: {symbol} | 原因: {violations}"
                        level = "warning"
                        if not first_error:
                            first_error = f"{symbol}: 风控拦截({violations})"
                    else:
                        failed_count += 1
                        error_detail = (
                            submit_inner_res.get("message")
                            or result.get("detail")
                            or "柜台拒绝或连接断开"
                        )
                        line = f"  >> [失败] 执行异常: {symbol} | 详情: {error_detail}"
                        level = "error"
                        if not first_error:
                            first_error = f"{symbol}: {error_detail}"

                    manual_execution_log_stream.append_log(
                        task_id=task_id,
                        tenant_id=tenant_id,
                        user_id=user_id,
                        level=level,
                        stage="dispatching",
                        line=line,
                    )

                except Exception as e:
                    failed_count += 1
                    error_msg = (
                        f"  >> [崩溃] 处理标的 {symbol} 时发生程序异常: {str(e)}"
                    )
                    manual_execution_log_stream.append_log(
                        task_id=task_id,
                        tenant_id=tenant_id,
                        user_id=user_id,
                        level="error",
                        stage="dispatching",
                        line=error_msg,
                    )
                    if not first_error:
                        first_error = str(e)

                processed = index
                progress = int((processed / max(total, 1)) * 100)

                # 实时更新进度
                if index % 5 == 0 or index == total:
                    await manual_execution_persistence.update_task(
                        task_id=task_id,
                        status="running" if index < total else "completed",
                        progress=progress,
                        order_count=index,
                        success_count=success_count,
                        failed_count=failed_count,
                    )

                # 给日志流一点喘息时间，避免瞬间冲刷
                await asyncio.sleep(0.05)

            # Phase 1: 先提交卖单
            if sell_orders:
                manual_execution_log_stream.append_log(
                    task_id=task_id,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    level="info",
                    stage="dispatching",
                    status="running",
                    line=f"进入卖单执行阶段，共 {len(sell_orders)} 笔卖单",
                )
                for row in sell_orders:
                    submit_index += 1
                    await _submit_one_order(row, submit_index)

                sell_wait_timeout_sec = _manual_task_sell_wait_timeout_sec()
                sell_wait_poll_sec = _manual_task_sell_wait_poll_sec()
                actual_sell_filled_value = 0.0
                actual_sell_filled_quantity = 0.0
                sell_settled_count = 0
                sell_timed_out = False

                if submitted_sell_orders and sell_wait_timeout_sec > 0:
                    manual_execution_log_stream.append_log(
                        task_id=task_id,
                        tenant_id=tenant_id,
                        user_id=user_id,
                        level="info",
                        stage="dispatching",
                        status="running",
                        line=(
                            "卖单提交完成，进入成交等待阶段："
                            f"submitted={len(submitted_sell_orders)}，"
                            f"timeout={sell_wait_timeout_sec}s，poll={sell_wait_poll_sec:.1f}s"
                        ),
                    )
                    loop = asyncio.get_running_loop()
                    start_ts = loop.time()
                    deadline_ts = start_ts + float(sell_wait_timeout_sec)
                    last_log_elapsed = -1

                    sell_client_ids = [
                        str(item.get("client_order_id") or "")
                        for item in submitted_sell_orders
                        if str(item.get("client_order_id") or "")
                    ]
                    while True:
                        sell_status_rows = (
                            (
                                await db.execute(
                                    select(
                                        Order.client_order_id,
                                        Order.status,
                                        Order.filled_quantity,
                                        Order.filled_value,
                                    ).where(
                                        and_(
                                            Order.tenant_id == tenant_id,
                                            Order.user_id == user_id,
                                            Order.client_order_id.in_(sell_client_ids),
                                        )
                                    )
                                )
                            )
                            .mappings()
                            .all()
                        )
                        status_map = {
                            str(row.get("client_order_id") or ""): row
                            for row in sell_status_rows
                        }

                        settled_count = 0
                        filled_qty_sum = 0.0
                        filled_value_sum = 0.0
                        for item in submitted_sell_orders:
                            cid = str(item.get("client_order_id") or "")
                            row = status_map.get(cid) or {}
                            status_text = _order_status_text(row.get("status"))
                            if status_text in {"filled", "cancelled", "rejected", "expired"}:
                                settled_count += 1
                            fill_qty = max(0.0, _to_float(row.get("filled_quantity"), 0.0))
                            fill_value = max(0.0, _to_float(row.get("filled_value"), 0.0))
                            filled_qty_sum += fill_qty
                            filled_value_sum += fill_value

                        actual_sell_filled_quantity = filled_qty_sum
                        actual_sell_filled_value = filled_value_sum
                        sell_settled_count = settled_count

                        if settled_count >= len(submitted_sell_orders):
                            manual_execution_log_stream.append_log(
                                task_id=task_id,
                                tenant_id=tenant_id,
                                user_id=user_id,
                                level="info",
                                stage="dispatching",
                                status="running",
                                line=(
                                    "卖单成交等待完成："
                                    f"settled={settled_count}/{len(submitted_sell_orders)}，"
                                    f"filled_qty={filled_qty_sum:.0f}，"
                                    f"filled_amount={filled_value_sum:.2f}"
                                ),
                            )
                            break

                        now_ts = loop.time()
                        elapsed = int(now_ts - start_ts)
                        if now_ts >= deadline_ts:
                            sell_timed_out = True
                            manual_execution_log_stream.append_log(
                                task_id=task_id,
                                tenant_id=tenant_id,
                                user_id=user_id,
                                level="warning",
                                stage="dispatching",
                                status="running",
                                line=(
                                    "卖单成交等待超时，按当前已成交金额进入买单阶段："
                                    f"settled={settled_count}/{len(submitted_sell_orders)}，"
                                    f"filled_qty={filled_qty_sum:.0f}，"
                                    f"filled_amount={filled_value_sum:.2f}"
                                ),
                            )
                            break

                        if elapsed // 30 > last_log_elapsed:
                            last_log_elapsed = elapsed // 30
                            manual_execution_log_stream.append_log(
                                task_id=task_id,
                                tenant_id=tenant_id,
                                user_id=user_id,
                                level="info",
                                stage="dispatching",
                                status="running",
                                line=(
                                    "卖单等待中："
                                    f"elapsed={elapsed}s，"
                                    f"settled={settled_count}/{len(submitted_sell_orders)}，"
                                    f"filled_amount={filled_value_sum:.2f}"
                                ),
                            )

                        await asyncio.sleep(
                            min(
                                sell_wait_poll_sec,
                                max(0.2, deadline_ts - now_ts),
                            )
                        )
                elif submitted_sell_orders:
                    manual_execution_log_stream.append_log(
                        task_id=task_id,
                        tenant_id=tenant_id,
                        user_id=user_id,
                        level="warning",
                        stage="dispatching",
                        status="running",
                        line="卖单等待超时参数<=0，已跳过等待阶段并直接进入买单",
                    )
                    sell_wait_timeout_sec = 0
                    sell_wait_poll_sec = 0.0
                    actual_sell_filled_value = 0.0
                    actual_sell_filled_quantity = 0.0
                    sell_settled_count = 0
                    sell_timed_out = True
                else:
                    manual_execution_log_stream.append_log(
                        task_id=task_id,
                        tenant_id=tenant_id,
                        user_id=user_id,
                        level="warning",
                        stage="dispatching",
                        status="running",
                        line="卖单阶段未产生成功提交的卖单，将直接进入买单阶段",
                    )
                    sell_wait_timeout_sec = _manual_task_sell_wait_timeout_sec()
                    sell_wait_poll_sec = _manual_task_sell_wait_poll_sec()
                    actual_sell_filled_value = 0.0
                    actual_sell_filled_quantity = 0.0
                    sell_settled_count = 0
                    sell_timed_out = False

                initial_cash = _to_float(runtime_plan_summary.get("available_cash"), 0.0)
                buy_budget_after_sell = max(0.0, initial_cash + actual_sell_filled_value)
                recalculated_buy_orders, buy_skipped_items, buy_remaining_cash = _rebuild_buy_orders_with_budget(
                    buy_orders=buy_orders,
                    buy_budget=buy_budget_after_sell,
                )
                if buy_skipped_items:
                    skipped_items.extend(buy_skipped_items)
                    manual_execution_log_stream.append_log(
                        task_id=task_id,
                        tenant_id=tenant_id,
                        user_id=user_id,
                        level="warning",
                        stage="dispatching",
                        status="running",
                        line=f"买单重算后新增跳过 {len(buy_skipped_items)} 笔（资金不足或缺少行情）",
                    )
                buy_orders = recalculated_buy_orders

                runtime_plan_summary.update(
                    {
                        "execution_phase": "sell_wait_buy",
                        "sell_order_count": len(sell_orders),
                        "buy_order_count": len(buy_orders),
                        "skipped_count": len(skipped_items),
                        "sell_wait_timeout_sec": sell_wait_timeout_sec,
                        "sell_wait_poll_sec": sell_wait_poll_sec,
                        "sell_wait_timed_out": bool(sell_timed_out),
                        "sell_submitted_count": len(submitted_sell_orders),
                        "sell_settled_count": sell_settled_count,
                        "actual_sell_filled_quantity": round(
                            actual_sell_filled_quantity, 4
                        ),
                        "actual_sell_filled_value": round(actual_sell_filled_value, 2),
                        "buy_budget_after_sell_wait": round(buy_budget_after_sell, 2),
                        "estimated_remaining_cash": round(buy_remaining_cash, 2),
                    }
                )

                manual_execution_log_stream.append_log(
                    task_id=task_id,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    level="info",
                    stage="dispatching",
                    status="running",
                    line=(
                        "卖后买资金门控完成："
                        f"buy_budget={buy_budget_after_sell:.2f}，"
                        f"buy_orders={len(buy_orders)}"
                    ),
                )
            else:
                runtime_plan_summary.update(
                    {
                        "execution_phase": "buy_only",
                        "sell_order_count": 0,
                        "buy_order_count": len(buy_orders),
                        "skipped_count": len(skipped_items),
                    }
                )
                manual_execution_log_stream.append_log(
                    task_id=task_id,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    level="info",
                    stage="dispatching",
                    status="running",
                    line="本次预案无卖单，直接进入买单阶段",
                )

            # Phase 2: 提交买单（若存在）
            if buy_orders:
                manual_execution_log_stream.append_log(
                    task_id=task_id,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    level="info",
                    stage="dispatching",
                    status="running",
                    line=f"进入买单执行阶段，共 {len(buy_orders)} 笔买单",
                )
                for row in buy_orders:
                    submit_index += 1
                    await _submit_one_order(row, submit_index)
            else:
                manual_execution_log_stream.append_log(
                    task_id=task_id,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    level="warning",
                    stage="dispatching",
                    status="running",
                    line="买单阶段无可执行委托（可能因卖单未成交释放资金）",
                )

            plan_summary = runtime_plan_summary

            result_payload = {
                "success": failed_count == 0,
                "task_id": task_id,
                "task_type": task_type,
                "run_id": run_id,
                "model_id": prepared.model_id,
                "strategy_id": strategy_id,
                "strategy_name": prepared.strategy_name,
                "prediction_trade_date": prepared.prediction_trade_date.isoformat(),
                "trading_mode": trading_mode,
                "signal_count": total,
                "order_count": processed,
                "success_count": success_count,
                "failed_count": failed_count,
                "first_error": first_error or None,
                "preview_summary": plan_summary,
                "stage_label": _stage_label("completed"),
            }
            await manual_execution_persistence.update_task(
                task_id=task_id,
                status="completed",
                stage="completed",
                signal_count=total,
                order_count=processed,
                success_count=success_count,
                failed_count=failed_count,
                result_payload=result_payload,
                progress=100,
            )
            manual_execution_log_stream.append_log(
                task_id=task_id,
                tenant_id=tenant_id,
                user_id=user_id,
                level="info" if failed_count == 0 else "warning",
                stage="completed",
                status="completed",
                progress=100,
                line=(
                    f"{task_label}派单完成: signals={total}, orders={processed}, "
                    f"success={success_count}, failed={failed_count}"
                ),
                summary={
                    **result_payload,
                    "stage_label": _stage_label("completed"),
                },
            )
            manual_execution_log_stream.update_state(
                task_id=task_id,
                tenant_id=tenant_id,
                user_id=user_id,
                stage="completed",
                status="completed",
                progress=100,
                signal_count=total,
                order_count=processed,
                success_count=success_count,
                failed_count=failed_count,
                summary={
                    **result_payload,
                    "stage_label": _stage_label("completed"),
                },
                last_line=(
                    f"{task_label}派单完成: signals={total}, orders={processed}, "
                    f"success={success_count}, failed={failed_count}"
                ),
            )

    async def execute_task_by_id(self, task_id: str) -> None:
        task = await manual_execution_persistence.get_task_any(task_id)
        if task is None:
            return
        await self.process_task(task)


manual_execution_service = ManualExecutionService()
