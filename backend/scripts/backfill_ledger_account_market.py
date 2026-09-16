#!/usr/bin/env python3
"""台账账户市场化回填（T-P1-04 收口配套）：非 CN 行补账户 id 市场段 + 补建账户行。

用法（容器内）:
    python backend/scripts/backfill_ledger_account_market.py            # DRY-RUN（默认）
    python backend/scripts/backfill_ledger_account_market.py --apply    # 实际写库

规则（幂等，保守；CN 无后缀是存量约定，一律不动）：
1. 批次/流水表中 ``COALESCE(market,'CN') <> 'CN'`` 且 account_id 未带该市场后缀的行：
   account_id 追加 ``:{MARKET}``；
2. 存在非 CN 子表行但对应账户行缺失时补建：cash = 该 (tenant,user,market)
   **最后一笔 cash_ledger 的 balance_after**（有据可依）；initial_equity = 0（诚实缺省，
   不猜测）；无流水依据则跳过并点名，交人工；
3. ``simulation_orders`` 旧格式 account_id（缺 ``sim:`` 前缀）：按 symbol 推断市场，
   归一为台账 id（该字段仅存储语义，无读取方依赖）。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

_CHILD_TABLES = ("simulation_position_lots", "simulation_cash_ledger")


async def _scan_suffix_plan(session) -> list[dict[str, Any]]:
    """规则 1 清单：非 CN 且未带市场后缀的 account_id。"""
    from sqlalchemy import text as sa_text

    plan: list[dict[str, Any]] = []
    for table in _CHILD_TABLES:
        rows = (
            await session.execute(
                sa_text(
                    f"SELECT DISTINCT tenant_id, user_id, market, account_id FROM {table} "
                    "WHERE COALESCE(market,'CN') <> 'CN' "
                    "AND account_id NOT LIKE '%:' || UPPER(market)"
                )
            )
        ).fetchall()
        for r in rows:
            plan.append(
                {
                    "table": table,
                    "tenant_id": str(r[0]),
                    "user_id": str(r[1]),
                    "market": str(r[2]).upper(),
                    "old": str(r[3]),
                    "new": f"{r[3]}:{str(r[2]).upper()}",
                }
            )
    return plan


async def _scan_account_gap_plan(session) -> list[dict[str, Any]]:
    """规则 2 清单：非 CN 子表行存在但账户行缺失（cash 取该市场末笔 balance_after）。"""
    from sqlalchemy import text as sa_text

    from backend.shared.simulation_account_keys import ledger_account_id

    group_rows = (
        await session.execute(
            sa_text(
                "SELECT tenant_id, user_id, UPPER(market) AS mkt FROM simulation_cash_ledger "
                "WHERE COALESCE(market,'CN') <> 'CN' "
                "GROUP BY tenant_id, user_id, UPPER(market)"
            )
        )
    ).fetchall()
    plan: list[dict[str, Any]] = []
    for tenant_id, user_id, market in group_rows:
        account_id = ledger_account_id(str(tenant_id), str(user_id), str(market))
        exists = (
            await session.execute(
                sa_text("SELECT 1 FROM simulation_accounts WHERE account_id=:a LIMIT 1"),
                {"a": account_id},
            )
        ).fetchone()
        if exists is not None:
            continue
        last_balance = (
            await session.execute(
                sa_text(
                    "SELECT balance_after FROM simulation_cash_ledger "
                    "WHERE tenant_id=:t AND CAST(user_id AS varchar)=:u "
                    "AND COALESCE(market,'CN')=:m "
                    "ORDER BY occurred_at DESC, id DESC LIMIT 1"
                ),
                {"t": str(tenant_id), "u": str(user_id), "m": str(market)},
            )
        ).fetchone()
        plan.append(
            {
                "tenant_id": str(tenant_id),
                "user_id": str(user_id),
                "market": str(market),
                "account_id": account_id,
                "currency": {"HK": "HKD", "US": "USD"}.get(str(market).upper(), "CNY"),
                "cash": float(last_balance[0]) if last_balance and last_balance[0] is not None else None,
            }
        )
        if plan[-1]["cash"] is None:
            print(
                f"  [需人工] {account_id}：无 balance_after 依据，跳过补建（不猜测现金）"
            )
    return [p for p in plan if p["cash"] is not None]


async def _scan_orders_format_plan(session) -> list[dict[str, Any]]:
    """规则 3 清单：simulation_orders 旧格式（缺 sim: 前缀）account_id。"""
    from sqlalchemy import text as sa_text

    from backend.services.simulation.services.market_rules import infer_market
    from backend.shared.simulation_account_keys import ledger_account_id

    rows = (
        await session.execute(
            sa_text(
                "SELECT DISTINCT tenant_id, user_id, symbol, account_id FROM simulation_orders "
                "WHERE account_id NOT LIKE 'sim:%' LIMIT 500"
            )
        )
    ).fetchall()
    return [
        {
            "tenant_id": str(r[0]),
            "user_id": str(r[1]),
            "old": str(r[3]),
            "new": ledger_account_id(str(r[0]), str(r[1]), infer_market(str(r[2]))),
        }
        for r in rows
    ]


async def run(*, apply: bool) -> int:
    from backend.shared.database_manager_v2 import close_database, get_session

    async with get_session(read_only=not apply) as session:
        suffix_plan = await _scan_suffix_plan(session)
        gap_plan = await _scan_account_gap_plan(session)
        orders_plan = await _scan_orders_format_plan(session)

    print(
        f"[backfill] 账户 id 补后缀 {len(suffix_plan)} 组 / 补建账户行 {len(gap_plan)} 个 / "
        f"orders 归一 {len(orders_plan)} 组"
    )
    for item in suffix_plan[:10]:
        print(f"  {item['table']}: {item['old']} -> {item['new']}")
    for item in gap_plan[:10]:
        print(
            f"  补建账户: {item['account_id']} cash={item['cash']:.2f}（末笔 balance_after）"
        )
    for item in orders_plan[:10]:
        print(f"  orders: {item['old']} -> {item['new']}")

    if not apply:
        print("[backfill] DRY-RUN（未写库）。确认清单后加 --apply 执行。")
        await close_database()
        return 0

    from sqlalchemy import text as sa_text

    async with get_session(read_only=False) as session:
        for item in suffix_plan:
            await session.execute(
                sa_text(
                    f"UPDATE {item['table']} SET account_id = :new "
                    "WHERE tenant_id=:t AND CAST(user_id AS varchar)=:u "
                    "AND COALESCE(market,'CN')=:m AND account_id = :old"
                ),
                {
                    "new": item["new"],
                    "old": item["old"],
                    "t": item["tenant_id"],
                    "u": item["user_id"],
                    "m": item["market"],
                },
            )
        for item in gap_plan:
            await session.execute(
                sa_text(
                    "INSERT INTO simulation_accounts "
                    "(account_id, tenant_id, user_id, market, initial_equity, cash, "
                    " available_cash, total_asset, equity, base_currency, account_type, status) "
                    "VALUES (:a, :t, :u, :m, 0, :cash, :cash, :cash, :cash, :cur, 'cash', 'active') "
                    "ON CONFLICT (account_id) DO NOTHING"
                ),
                {
                    "a": item["account_id"],
                    "t": item["tenant_id"],
                    "u": item["user_id"],
                    "m": item["market"],
                    "cur": item.get("currency", "CNY"),
                    "cash": item["cash"],
                },
            )
        for item in orders_plan:
            await session.execute(
                sa_text(
                    "UPDATE simulation_orders SET account_id = :new "
                    "WHERE tenant_id=:t AND CAST(user_id AS varchar)=:u AND account_id = :old"
                ),
                {
                    "new": item["new"],
                    "old": item["old"],
                    "t": item["tenant_id"],
                    "u": item["user_id"],
                },
            )
        await session.commit()
    print("[backfill] 已执行；重跑应为 0 组（幂等）")
    await close_database()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="台账账户市场化回填（T-P1-04 收口配套）")
    parser.add_argument("--apply", action="store_true", help="实际写库（默认 DRY-RUN）")
    args = parser.parse_args()
    return asyncio.run(run(apply=args.apply))


if __name__ == "__main__":
    raise SystemExit(main())
