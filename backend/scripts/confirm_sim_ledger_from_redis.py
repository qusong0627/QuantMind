#!/usr/bin/env python3
"""模拟盘存量账户确权：Redis → PG 台账（一次性，幂等，默认 dry-run）。

背景（2026-09-18）：模拟盘「PG 台账为主」迁移（T-P1-04）后，历史 Redis-only
账户在 PG 侧无账户行/无批次/无流水 → 对账恒 ledger_empty（C06 无对账报告）、
账户投影无法从台账重建。本脚本按既有合同（projection_service / reconcile_service）
为每个 Redis 存量账户补建三件套：

1. ``simulation_accounts`` 账户行（cash/available 取 Redis，市值按持仓聚合）；
2. ``simulation_cash_ledger`` 期初流水（event_type=opening，balance_after=现金）；
3. ``simulation_position_lots`` 期初批次（成本=Redis 持仓 cost，open_date 取
   确权前 3 天，保证 T+1 语义下与 Redis available_volume 全量一致）。

证据纪律（与 backfill_ledger_account_market.py 同源）：只搬 Redis 有据可依的值；
成本缺失记 0 并点名；``initial_equity`` 取该用户 CN settings 的 initial_cash，
非 CN 或缺失记 0（诚实缺省，不猜测）。

幂等：账户行/批次/流水任一已存在即整账户跳过，重复执行安全。

用法（容器内）:
    python backend/scripts/confirm_sim_ledger_from_redis.py            # DRY-RUN
    python backend/scripts/confirm_sim_ledger_from_redis.py --apply    # 实际写库
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

OPEN_DATE_BACKFILL_DAYS = 3  # 确权批次 open_date 回拨天数（T+1 语义下全量可卖）


def _parse_account_key(key: str) -> tuple[str, str, str] | None:
    parts = key.split(":")
    if len(parts) < 4 or parts[0] != "simulation" or parts[1] != "account":
        return None
    tenant = parts[2].strip() or "default"
    user = parts[3].strip()
    if not user.isdigit():
        return None
    market = parts[4].strip().upper() if len(parts) > 4 else "CN"
    return tenant, user, market


def _split_position_key(key: str) -> tuple[str, str]:
    """Redis 持仓键 → (symbol, position_side)（投影侧的 :short 后缀约定）。"""
    raw = str(key).strip()
    if raw.endswith(":short"):
        return raw[: -len(":short")], "short"
    return raw, "long"


async def _load_redis_accounts() -> dict[str, dict[str, Any]]:
    """扫描 trade Redis 的 simulation:account:*（与 reconcile 同口径）。"""
    from backend.services.trade_shared.redis_client import RedisClient

    redis = RedisClient()
    redis.connect()
    if not redis.client:
        raise RuntimeError("Redis 未连接")
    keys = await asyncio.to_thread(
        lambda: list(redis.client.scan_iter(match="simulation:account:*", count=500))
    )
    accounts: dict[str, dict[str, Any]] = {}
    for raw in keys:
        key = str(raw)
        if _parse_account_key(key) is None:
            continue
        payload = await asyncio.to_thread(redis.client.get, key)
        try:
            data = json.loads(payload) if payload else {}
        except (TypeError, ValueError):
            data = {}
        if isinstance(data, dict):
            accounts[key] = data
    return accounts


def _read_settings_initial(redis, tenant: str, user: str) -> float:
    """CN settings 初始资金（唯一键形；缺失返回 0，诚实缺省）。"""
    try:
        raw = redis.client.get(f"simulation:settings:{tenant}:{user}")
        data = json.loads(raw) if raw else {}
    except Exception:  # noqa: BLE001
        return 0.0
    try:
        return float((data or {}).get("initial_cash") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _position_plan(account: dict[str, Any]) -> tuple[list[dict[str, Any]], float, list[str]]:
    """Redis 持仓 → 期初批次计划；返回 (lots, long_mv, 成本缺失标的)。"""
    lots: list[dict[str, Any]] = []
    long_mv = 0.0
    missing_cost: list[str] = []
    positions = account.get("positions") or {}
    if not isinstance(positions, dict):
        return lots, long_mv, missing_cost
    for key, pos in positions.items():
        if not isinstance(pos, dict):
            continue
        symbol, side = _split_position_key(str(key))
        if not symbol:
            continue
        volume = float(pos.get("volume") or 0.0)
        if volume <= 0:
            continue
        cost = pos.get("cost", pos.get("cost_price", 0.0))
        try:
            cost_f = float(cost or 0.0)
        except (TypeError, ValueError):
            cost_f = 0.0
        if cost_f <= 0:
            missing_cost.append(symbol)
        lots.append(
            {
                "symbol": symbol.upper(),
                "position_side": side,
                "quantity": volume,
                "cost_price": cost_f,
            }
        )
        if side == "long":
            try:
                long_mv += float(pos.get("market_value") or 0.0)
            except (TypeError, ValueError):
                pass
    return lots, round(long_mv, 4), missing_cost


async def _account_has_rows(session, account_id: str) -> bool:
    from sqlalchemy import text as sa_text

    for table in ("simulation_position_lots", "simulation_cash_ledger"):
        row = (
            await session.execute(
                sa_text(f"SELECT 1 FROM {table} WHERE account_id = :aid LIMIT 1"),
                {"aid": account_id},
            )
        ).first()
        if row:
            return True
    return False


async def run(*, apply: bool) -> int:
    from sqlalchemy import select

    from backend.services.simulation.models.account import SimulationAccount
    from backend.services.simulation.models.cash_ledger import SimulationCashLedger
    from backend.services.simulation.models.position_lot import SimulationPositionLot
    from backend.services.trade_shared.redis_client import RedisClient
    from backend.shared.database_manager_v2 import get_session
    from backend.shared.simulation_account_keys import ledger_account_id

    accounts = await _load_redis_accounts()
    if not accounts:
        print("[确权] 未发现 simulation:account:* 账户，退出")
        return 0

    redis = RedisClient()
    redis.connect()
    now = datetime.utcnow()
    open_date = now - timedelta(days=OPEN_DATE_BACKFILL_DAYS)

    created = 0
    skipped = 0
    for key in sorted(accounts):
        parsed = _parse_account_key(key)
        if parsed is None:
            continue
        tenant, user, market = parsed
        account = accounts[key]
        account_id = ledger_account_id(tenant, user, market)
        cash = float(account.get("cash") or 0.0)
        lots, long_mv, missing_cost = _position_plan(account)
        initial = _read_settings_initial(redis, tenant, user) if market == "CN" else 0.0
        total_asset = round(cash + long_mv, 4)
        print(
            f"[确权] {key} → {account_id} cash={cash:.2f} lots={len(lots)} "
            f"long_mv={long_mv:.2f} initial={initial:.2f}"
            + (f" 成本缺失={missing_cost}" if missing_cost else "")
        )
        async with get_session() as session:
            existing = await session.get(SimulationAccount, account_id)
            if existing is not None or await _account_has_rows(session, account_id):
                print(f"        已存在台账（账户行或批次/流水），跳过")
                skipped += 1
                continue
            if not apply:
                print("        DRY-RUN：将建账户行 + 期初流水 + 期初批次")
                created += 1
                continue
            session.add(
                SimulationAccount(
                    account_id=account_id,
                    market=market,
                    tenant_id=tenant,
                    user_id=user,
                    initial_equity=initial,
                    cash=cash,
                    available_cash=cash,
                    frozen_cash=0.0,
                    long_market_value=long_mv,
                    short_market_value=0.0,
                    total_asset=total_asset,
                    equity=total_asset,
                    liabilities=float(account.get("liabilities") or 0.0),
                    maintenance_margin_ratio=float(
                        account.get("maintenance_margin_ratio") or 0.0
                    ),
                    last_projected_at=now,
                )
            )
            session.add(
                SimulationCashLedger(
                    account_id=account_id,
                    tenant_id=tenant,
                    user_id=user,
                    market=market,
                    event_type="opening",
                    ref_type="opening",
                    ref_id=None,
                    amount=cash,
                    balance_after=cash,
                    trade_date=now,
                    occurred_at=now,
                    note="确权期初（Redis 存量账户迁移，2026-09-18）",
                )
            )
            for lot in lots:
                qty = lot["quantity"]
                session.add(
                    SimulationPositionLot(
                        account_id=account_id,
                        tenant_id=tenant,
                        user_id=user,
                        market=market,
                        symbol=lot["symbol"],
                        position_side=lot["position_side"],
                        open_date=open_date,
                        quantity_open=qty,
                        quantity_remaining=qty,
                        cost_price=lot["cost_price"],
                        cost_amount=round(qty * lot["cost_price"], 4),
                        status="open",
                    )
                )
            await session.commit()
            created += 1
            print("        ✔ 已确权")

    mode = "APPLY" if apply else "DRY-RUN"
    print(f"[确权] {mode} 完成：将处理/已处理 {created} 个账户，跳过 {skipped} 个存量台账")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="模拟盘存量账户 Redis→PG 确权")
    parser.add_argument("--apply", action="store_true", help="实际写库（默认 dry-run）")
    args = parser.parse_args()
    return asyncio.run(run(apply=args.apply))


if __name__ == "__main__":
    raise SystemExit(main())
