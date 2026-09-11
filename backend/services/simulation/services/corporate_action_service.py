"""
Apply simulation corporate actions to lots, cash ledger, and account projection.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
import asyncio
import json
import logging
import os

from sqlalchemy import Select, or_, select
from sqlalchemy import text

from backend.services.simulation.models.account import SimulationAccount
from backend.services.simulation.models.cash_ledger import SimulationCashLedger
from backend.services.simulation.models.corporate_action import (
    SimulationCorporateAction,
)
from backend.services.simulation.models.position_lot import SimulationPositionLot
from backend.services.simulation.services.projection_service import (
    SimulationProjectionService,
)
from backend.shared.stock_utils import StockCodeUtil
from backend.shared.simulation_account_keys import account_key
from backend.shared.database_manager_v2 import get_session
from backend.shared.trade_account_cache import write_trade_account_cache
from backend.services.trade_shared.redis_client import redis_client

logger = logging.getLogger(__name__)


class SimulationCorporateActionService:
    @staticmethod
    def _merge_action_note(action: SimulationCorporateAction, summary: str) -> None:
        summary_text = str(summary or "").strip()
        if not summary_text:
            return
        existing = str(action.note or "").strip()
        action.note = f"{existing}; {summary_text}" if existing else summary_text

    @staticmethod
    def compute_dividend_cash(quantity: float, per_share: float) -> float:
        return round(max(0.0, float(quantity or 0.0)) * float(per_share or 0.0), 4)

    @staticmethod
    def compute_share_multiplier(action_type: str, share_ratio: float) -> float:
        normalized = str(action_type or "").strip().lower()
        ratio = float(share_ratio or 0.0)
        if normalized in {"bonus_share", "rights_issue"}:
            return max(0.0, 1.0 + ratio)
        if normalized in {"split", "reverse_split"}:
            return max(0.0, ratio if ratio > 0 else 1.0)
        return 1.0

    @classmethod
    async def apply_due_actions(cls, *, now: datetime | None = None) -> int:
        cutoff = now or datetime.utcnow()
        applied = 0
        async with get_session(read_only=False) as session:
            stmt: Select[tuple[SimulationCorporateAction]] = (
                select(SimulationCorporateAction)
                .where(
                    SimulationCorporateAction.status == "pending",
                    or_(
                        (
                            SimulationCorporateAction.effective_date.is_not(None)
                            & (SimulationCorporateAction.effective_date <= cutoff)
                        ),
                        (
                            SimulationCorporateAction.effective_date.is_(None)
                            & SimulationCorporateAction.ex_date.is_not(None)
                            & (SimulationCorporateAction.ex_date <= cutoff)
                        ),
                    ),
                )
                .order_by(
                    SimulationCorporateAction.effective_date.asc().nullsfirst(),
                    SimulationCorporateAction.ex_date.asc().nullsfirst(),
                    SimulationCorporateAction.id.asc(),
                )
            )
            actions = list((await session.execute(stmt)).scalars().all())
            for action in actions:
                # P0-3：原子认领（pending->processing），双worker/重跑只能一个得手；
                # 认领单独提交，apply失败回滚后状态回到pending可重跑，不留半截账。
                action_id = action.id
                claim = await session.execute(
                    text(
                        "UPDATE simulation_corporate_actions "
                        "SET status='processing' WHERE id=:id AND status='pending'"
                    ),
                    {"id": action_id},
                )
                await session.commit()
                if (getattr(claim, "rowcount", 0) or 0) == 0:
                    continue
                try:
                    fresh = await session.get(SimulationCorporateAction, action_id)
                    if fresh is None:
                        continue
                    await cls._apply_action(
                        session=session, action=fresh, applied_at=cutoff
                    )
                    await session.commit()
                    applied += 1
                except Exception as exc:
                    try:
                        await session.rollback()
                    except Exception:
                        pass
                    logger.error(
                        "Corporate action apply failed id=%s, rolled back to pending: %s",
                        action_id,
                        exc,
                        exc_info=True,
                    )
        return applied

    @staticmethod
    async def _ledger_exists(
        session, *, account_id: str, event_type: str, ref_id: str
    ) -> bool:
        """同一action对同一账户是否已记过该事件账（P0-3重跑幂等）。"""
        try:
            row = await session.execute(
                select(SimulationCashLedger.id)
                .where(
                    SimulationCashLedger.account_id == account_id,
                    SimulationCashLedger.event_type == event_type,
                    SimulationCashLedger.ref_type == "corporate_action",
                    SimulationCashLedger.ref_id == str(ref_id),
                )
                .limit(1)
            )
            return row.scalar_one_or_none() is not None
        except Exception:
            return False

    @classmethod
    async def _apply_action(
        cls,
        *,
        session,
        action: SimulationCorporateAction,
        applied_at: datetime,
    ) -> None:
        normalized_type = str(action.action_type or "").strip().lower()
        normalized_symbol = StockCodeUtil.to_prefix(action.symbol)
        lots = list(
            (
                await session.execute(
                    select(SimulationPositionLot).where(
                        SimulationPositionLot.symbol == normalized_symbol,
                        SimulationPositionLot.position_side == "long",
                        SimulationPositionLot.status == "open",
                        SimulationPositionLot.quantity_remaining > 0,
                    )
                )
            )
            .scalars()
            .all()
        )

        if normalized_type == "dividend":
            by_account: dict[str, list[SimulationPositionLot]] = defaultdict(list)
            for lot in lots:
                by_account[str(lot.account_id)].append(lot)
            applied_accounts = 0
            per_share = float(action.cash_dividend_per_share or 0.0)
            for account_id, account_lots in by_account.items():
                qty = sum(float(lot.quantity_remaining or 0.0) for lot in account_lots)
                cash = cls.compute_dividend_cash(qty, per_share)
                if cash <= 0:
                    continue
                account = await session.get(SimulationAccount, account_id)
                if account is None:
                    continue
                # P0-3：该账户已记过此次分红账则跳过（重跑幂等，不双发）
                if await cls._ledger_exists(
                    session,
                    account_id=account.account_id,
                    event_type="DIVIDEND_CASH",
                    ref_id=str(action.id),
                ):
                    continue
                account.cash = float(account.cash or 0.0) + cash
                account.available_cash = float(account.available_cash or 0.0) + cash
                account.total_asset = float(account.total_asset or 0.0) + cash
                account.equity = (
                    float(account.equity or account.total_asset or 0.0) + cash
                )
                account.last_projected_at = applied_at
                # 除息下调成本：名义价自然贴权，成本不降则此后浮盈系统性偏低。
                # cost_amount 同步重算；下限 0（高分红不倒贴）。
                if per_share > 0:
                    for lot in account_lots:
                        try:
                            new_cost = max(
                                0.0, float(lot.cost_price or 0.0) - per_share
                            )
                            lot.cost_price = round(new_cost, 6)
                            lot.cost_amount = round(
                                new_cost * float(lot.quantity_open or 0.0), 6
                            )
                        except Exception:
                            continue
                session.add(
                    SimulationCashLedger(
                        account_id=account.account_id,
                        tenant_id=account.tenant_id,
                        user_id=account.user_id,
                        event_type="DIVIDEND_CASH",
                        ref_type="corporate_action",
                        ref_id=str(action.id),
                        amount=cash,
                        balance_after=float(account.cash or 0.0),
                        trade_date=applied_at,
                        occurred_at=applied_at,
                        note=f"{normalized_symbol} dividend",
                    )
                )
                await cls._refresh_account_projection(
                    session=session,
                    account_id=account.account_id,
                    applied_at=applied_at,
                )
                applied_accounts += 1
            cls._merge_action_note(
                action,
                f"dividend_applied_accounts={applied_accounts},cost_adjusted_per_share={per_share}",
            )
        elif normalized_type in {"bonus_share", "split", "reverse_split"}:
            # 注意：当前 QuantDB 同步与 CSV 导入都不产生 split/reverse_split，
            # 该分支仅对手工入库的记录生效；若出现会在 note 中标出来源。
            if normalized_type in {"split", "reverse_split"}:
                logger.warning(
                    "公司行为出现拆股类型 %s symbol=%s（上游暂不产出，请核对手工录入）",
                    normalized_type,
                    normalized_symbol,
                )
            multiplier = cls.compute_share_multiplier(
                normalized_type, float(action.share_ratio or 0.0)
            )
            if multiplier <= 0:
                multiplier = 1.0
            touched_accounts: set[str] = set()
            old_qty_by_account: dict[str, float] = defaultdict(float)
            for lot in lots:
                old_open = float(lot.quantity_open or 0.0)
                old_remaining = float(lot.quantity_remaining or 0.0)
                if old_open <= 0 or old_remaining <= 0:
                    continue
                old_qty_by_account[str(lot.account_id)] += old_remaining
                lot.quantity_open = round(old_open * multiplier, 6)
                lot.quantity_remaining = round(old_remaining * multiplier, 6)
                if lot.quantity_open > 0:
                    lot.cost_price = round(
                        float(lot.cost_amount or 0.0) / float(lot.quantity_open),
                        6,
                    )
                touched_accounts.add(str(lot.account_id))
            latest_price = await cls._load_latest_price(session, normalized_symbol)
            for account_id in touched_accounts:
                await cls._refresh_account_projection(
                    session=session,
                    account_id=account_id,
                    applied_at=applied_at,
                )
                if latest_price > 0 and multiplier > 1.0:
                    account = await session.get(SimulationAccount, account_id)
                    if account is None:
                        continue
                    delta_qty = old_qty_by_account.get(account_id, 0.0) * (
                        multiplier - 1.0
                    )
                    value_delta = round(delta_qty * latest_price, 4)
                    if value_delta > 0:
                        session.add(
                            SimulationCashLedger(
                                account_id=account.account_id,
                                tenant_id=account.tenant_id,
                                user_id=account.user_id,
                                event_type="BONUS_SHARE_VALUE",
                                ref_type="corporate_action",
                                ref_id=str(action.id),
                                amount=value_delta,
                                balance_after=float(account.cash or 0.0),
                                trade_date=applied_at,
                                occurred_at=applied_at,
                                note=f"{normalized_symbol} {normalized_type} value delta",
                            )
                        )
            cls._merge_action_note(
                action,
                f"{normalized_type}_applied_accounts={len(touched_accounts)}",
            )
        elif normalized_type == "rights_issue":
            by_account: dict[str, list[SimulationPositionLot]] = defaultdict(list)
            for lot in lots:
                by_account[str(lot.account_id)].append(lot)
            applied_accounts = 0
            skipped_accounts = 0
            for account_id, account_lots in by_account.items():
                account = await session.get(SimulationAccount, account_id)
                if account is None:
                    continue
                subscribed_qty = sum(
                    max(0.0, float(lot.quantity_remaining or 0.0))
                    * max(0.0, float(action.share_ratio or 0.0))
                    for lot in account_lots
                )
                subscribed_qty = round(subscribed_qty, 6)
                if subscribed_qty <= 0:
                    continue
                total_cost = round(
                    subscribed_qty * float(action.rights_price or 0.0), 4
                )
                if total_cost <= 0:
                    continue
                # 配股认购开关：SIM_RIGHTS_AUTO_SUBSCRIBE=false 时只记录跳过，不动资金
                # （默认 true 保持现状：现金足够即全额认购）。
                if os.getenv("SIM_RIGHTS_AUTO_SUBSCRIBE", "true").strip().lower() in {
                    "0",
                    "false",
                    "no",
                    "off",
                }:
                    skipped_accounts += 1
                    session.add(
                        SimulationCashLedger(
                            account_id=account.account_id,
                            tenant_id=account.tenant_id,
                            user_id=account.user_id,
                            event_type="RIGHTS_SUBSCRIPTION_SKIPPED",
                            ref_type="corporate_action",
                            ref_id=str(action.id),
                            amount=0.0,
                            balance_after=float(account.cash or 0.0),
                            trade_date=applied_at,
                            occurred_at=applied_at,
                            note=(
                                f"{normalized_symbol} rights issue skipped: "
                                f"auto-subscribe disabled (SIM_RIGHTS_AUTO_SUBSCRIBE=false)"
                            ),
                        )
                    )
                    continue
                available_cash = float(account.available_cash or 0.0)
                if available_cash + 1e-6 < total_cost:
                    skipped_accounts += 1
                    session.add(
                        SimulationCashLedger(
                            account_id=account.account_id,
                            tenant_id=account.tenant_id,
                            user_id=account.user_id,
                            event_type="RIGHTS_SUBSCRIPTION_SKIPPED",
                            ref_type="corporate_action",
                            ref_id=str(action.id),
                            amount=0.0,
                            balance_after=float(account.cash or 0.0),
                            trade_date=applied_at,
                            occurred_at=applied_at,
                            note=(
                                f"{normalized_symbol} rights issue skipped: "
                                f"insufficient_cash available={available_cash:.4f} required={total_cost:.4f}"
                            ),
                        )
                    )
                    continue
                account.cash = float(account.cash or 0.0) - total_cost
                account.available_cash = available_cash - total_cost
                account.long_market_value = (
                    float(account.long_market_value or 0.0) + total_cost
                )
                account.last_projected_at = applied_at
                session.add(
                    SimulationCashLedger(
                        account_id=account.account_id,
                        tenant_id=account.tenant_id,
                        user_id=account.user_id,
                        event_type="RIGHTS_SUBSCRIPTION",
                        ref_type="corporate_action",
                        ref_id=str(action.id),
                        amount=-total_cost,
                        balance_after=float(account.cash or 0.0),
                        trade_date=applied_at,
                        occurred_at=applied_at,
                        note=f"{normalized_symbol} rights issue",
                    )
                )
                session.add(
                    SimulationPositionLot(
                        account_id=account.account_id,
                        tenant_id=account.tenant_id,
                        user_id=account.user_id,
                        symbol=normalized_symbol,
                        position_side="long",
                        open_fill_id=f"corporate_action:{action.id}",
                        open_date=applied_at,
                        quantity_open=subscribed_qty,
                        quantity_remaining=subscribed_qty,
                        cost_price=float(action.rights_price or 0.0),
                        cost_amount=total_cost,
                        status="open",
                    )
                )
                await cls._refresh_account_projection(
                    session=session,
                    account_id=account.account_id,
                    applied_at=applied_at,
                )
                applied_accounts += 1
            cls._merge_action_note(
                action,
                "rights_issue_applied_accounts="
                f"{applied_accounts},skipped_accounts={skipped_accounts}",
            )

        action.status = "applied"
        action.applied_at = applied_at

    @classmethod
    async def _refresh_account_projection(
        cls,
        *,
        session,
        account_id: str,
        applied_at: datetime,
    ) -> None:
        account = await session.get(SimulationAccount, account_id)
        if account is None:
            return
        projection = await SimulationProjectionService(session).load_projection(
            tenant_id=account.tenant_id,
            user_id=account.user_id,
            latest_price_loader=lambda symbol: cls._load_latest_price(session, symbol),
        )
        positions = projection.positions or {}
        long_market_value = 0.0
        short_market_value = 0.0
        for pos in positions.values():
            if not isinstance(pos, dict):
                continue
            market_value = float(pos.get("market_value") or 0.0)
            side = str(pos.get("side") or "long").strip().lower()
            if side == "short":
                short_market_value += market_value
            else:
                long_market_value += market_value
        cash = float(account.cash or 0.0)
        liabilities = float(account.liabilities or 0.0)
        # P0-6：计入Redis侧short_proceeds，与盘中equity口径对齐
        try:
            from backend.shared.simulation_account_keys import account_key
            from backend.shared.trade_account_cache import read_json_cache
            from backend.services.trade_shared.redis_client import (
                redis_client as _redis_client,
            )

            _cached = read_json_cache(
                _redis_client,
                account_key(account.tenant_id, account.user_id, "CN"),
            )
            _proceeds = float((_cached or {}).get("short_proceeds") or 0.0)
        except Exception:
            _proceeds = 0.0
        total_asset = round(
            cash + _proceeds + long_market_value - short_market_value, 4
        )
        account.long_market_value = round(long_market_value, 4)
        account.short_market_value = round(short_market_value, 4)
        account.total_asset = total_asset
        account.equity = total_asset
        account.last_projected_at = applied_at
        cls._persist_projection_cache(
            account=account,
            positions=positions,
            tenant_id=account.tenant_id,
            user_id=account.user_id,
        )

    @staticmethod
    def _persist_projection_cache(
        *,
        account: SimulationAccount,
        positions: dict,
        tenant_id: str,
        user_id: str,
    ) -> None:
        if not redis_client.client:
            return
        sim_key = account_key(tenant_id, user_id)
        # 空投影保护（与 EOD _rebuild_redis 同理）：ledger 为空时不覆盖 Redis 实盘持仓
        if not positions:
            try:
                from backend.shared.trade_account_cache import read_json_cache

                current = read_json_cache(redis_client, sim_key) or {}
                live = current.get("positions") or {}
                if isinstance(live, str):
                    try:
                        live = json.loads(live)
                    except Exception:
                        live = {}
                live_count = (
                    sum(
                        1
                        for pos in live.values()
                        if isinstance(pos, dict) and float(pos.get("volume") or 0) > 0
                    )
                    if isinstance(live, dict)
                    else 0
                )
                if live_count > 0:
                    logger.error(
                        "Corporate-action rebuild skipped for %s: ledger projection empty "
                        "but Redis holds live positions",
                        sim_key,
                    )
                    return
            except Exception:
                pass
        payload = SimulationProjectionService.build_cache_payload(
            account=account,
            positions=positions,
            source="corporate_action_apply",
        )
        redis_client.client.set(sim_key, json.dumps(payload, ensure_ascii=False))
        write_trade_account_cache(redis_client, tenant_id, user_id, payload)

    @staticmethod
    async def _load_latest_price(session, symbol: str) -> float:
        prefix_symbol = StockCodeUtil.to_prefix(symbol)
        suffix_symbol = StockCodeUtil.to_suffix(prefix_symbol)
        query = text(
            """
            SELECT close, adj_factor
            FROM stock_daily_latest
            WHERE symbol = :symbol
            ORDER BY trade_date DESC
            LIMIT 1
            """
        )
        for candidate in (prefix_symbol, suffix_symbol):
            result = await session.execute(query, {"symbol": candidate})
            row = result.fetchone()
            if not row:
                continue
            close_price = float(row[0] or 0.0)
            if close_price <= 0:
                continue
            return close_price
        return 0.0


async def run_simulation_corporate_action_worker(interval_seconds: int = 3600) -> None:
    while True:
        try:
            await SimulationCorporateActionService.apply_due_actions()
        except Exception as exc:
            logger.error(
                "Simulation corporate action worker failed: %s", exc, exc_info=True
            )
        await asyncio.sleep(max(60, int(interval_seconds or 3600)))
