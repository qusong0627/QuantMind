#!/usr/bin/env python3
"""把「TDX 跨日过期但实际成交」的委托补记进分账（P0，2026-11-18）。

背景：TDX 柜台只提供**当日**委托；过期（expired）不等于没成交。审计发现 5 笔
过期委托里至少 3 笔仍留着账户/分账差：

* 601658.SH 3,300 股（glm 买单，过期但账户有、分账无）
* 002958.SZ 2,400 股（flash 买单，同上）
* 601825.SH 1,400 股（pro 卖单，过期但账户已减、分账未扣）

判据（全部满足才补；任何一条不满足只报告）：
1. ``orders.status='expired'``、``agent`` 非空、``filled_quantity>0``；
2. 该标的**当前**账户/分账差 ≥ 该委托数量（说明成交后果仍在）；
3. 买单用账户成本价（单笔建仓时即真实成交价），卖单用委托限价（历史成交价不可考，
   标 ``approx``）；价格过 ``apply_fill`` 的 ±40% 坏价闸。

写库走 ``apply_fill``（与真单同一实现），幂等键 ``expired-recover:<order_id>:<agent>``。
默认 DRY-RUN；``--apply`` 才写。

用法（容器内）：
    python backend/scripts/reconcile_expired_agent_orders.py          # DRY-RUN
    python backend/scripts/reconcile_expired_agent_orders.py --apply
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.shared.agent_ledger_fill import market_of  # noqa: E402
from backend.shared.agent_ledger_store import apply_fill  # noqa: E402
from backend.shared.database_manager_v2 import get_session  # noqa: E402
from backend.shared.decision.agent_ledger import RECOVER_FILL_PREFIX  # noqa: E402
from backend.shared.decision.manual_sell_auto_absorb import (  # noqa: E402
    compute_mismatch,
    load_tdx_account_volumes,
)

BEIJING = ZoneInfo("Asia/Shanghai")
TENANT = "default"
USER = "10000001"
LOOKBACK_DAYS = 60
EPS = 1e-6
FILL_KEY_PREFIX = RECOVER_FILL_PREFIX


@dataclass(frozen=True)
class Recovery:
    order_id: str
    agent: str
    code: str
    side: str
    volume: float
    price: float
    trade_date: date
    filled_at: datetime
    note: str


async def load_expired_orders(session, since: datetime) -> list[dict]:
    from sqlalchemy import text

    rows = (
        await session.execute(
            text(
                "SELECT order_id::text AS order_id, agent, symbol, side::text AS side, "
                "       quantity, filled_quantity, average_price, price, created_at "
                "FROM orders "
                "WHERE tenant_id=:t AND user_id=:u AND status::text='expired' "
                "  AND COALESCE(agent,'') <> '' AND quantity > 0 "
                "  AND created_at >= :since "
                "ORDER BY created_at"
            ),
            {"t": TENANT, "u": USER, "since": since},
        )
    ).fetchall()
    return [dict(r._mapping) for r in rows]


async def load_ledger(session) -> list[dict]:
    from sqlalchemy import text

    rows = (
        await session.execute(
            text(
                "SELECT agent, code, volume, cost_price, buy_ts "
                "FROM qm_agent_ledger_position "
                "WHERE tenant_id=:t AND user_id=:u"
            ),
            {"t": TENANT, "u": USER},
        )
    ).fetchall()
    return [dict(r._mapping) for r in rows]


def to_suffix(code: str) -> str:
    from backend.shared.decision.manual_sell_absorption import to_suffix_code

    return to_suffix_code(code)


def build_recovery(order: dict, ledger_excess: dict, account_excess: dict, account_cost: dict) -> Recovery | None:
    code = to_suffix(order["symbol"])
    side = str(order["side"]).lower()
    # 过期单的 filled_quantity 恒为 0（系统从未收到成交回报）；数量按**委托量**，
    # 与账户/分账差逐一比对后才补记（差不足即拒，不猜部分成交）。
    qty = float(order.get("filled_quantity") or 0) or float(order.get("quantity") or 0)
    if qty <= 0:
        return None
    agent = str(order["agent"])
    created = order["created_at"]
    if created.tzinfo is None:
        created = created.replace(tzinfo=BEIJING)
    trade_day = created.astimezone(BEIJING).date()

    if side == "buy":
        gap = account_excess.get(code, {}).get("account", 0) - account_excess.get(code, {}).get("ledger", 0)
        if gap + EPS < qty:
            return None
        price = account_cost.get(code)
        if not price:
            return None
        return Recovery(
            order_id=str(order["order_id"]),
            agent=agent,
            code=code,
            side="buy",
            volume=qty,
            price=float(price),
            trade_date=trade_day,
            filled_at=created.astimezone(timezone.utc),
            note="expired 委托补记：账户持仓差与委托一致，价格取账户成本价",
        )

    gap = ledger_excess.get(code, {}).get("ledger", 0) - ledger_excess.get(code, {}).get("account", 0)
    if gap + EPS < qty:
        return None
    price = float(order.get("average_price") or order.get("price") or 0)
    if price <= 0:
        return None
    return Recovery(
        order_id=str(order["order_id"]),
        agent=agent,
        code=code,
        side="sell",
        volume=qty,
        price=price,
        trade_date=trade_day,
        filled_at=created.astimezone(timezone.utc),
        note="expired 委托补记：账本多出量与委托一致，价格取委托限价（approx）",
    )


async def run(apply: bool) -> int:
    # orders.created_at 是 naive 列（北京墙钟）：asyncpg 不接受 aware 参数
    since = datetime.now(BEIJING).replace(tzinfo=None) - timedelta(days=LOOKBACK_DAYS)
    async with get_session(read_only=True) as db:
        orders = await load_expired_orders(db, since)
        ledger = await load_ledger(db)

    real_volumes, note = await load_tdx_account_volumes(TENANT, USER)
    if real_volumes is None:
        print(f"[reconcile] 账户不可读：{note}，拒绝补记")
        return 2
    ledger_excess, account_excess = compute_mismatch(ledger, real_volumes)
    account_cost = await _account_cost()

    print(
        f"[reconcile] 过期委托 {len(orders)} 笔；账户 {len(real_volumes)} 只（{note}）；"
        f"账本多 {len(ledger_excess)} 只 / 账户多 {len(account_excess)} 只"
    )
    plans = [p for o in orders if (p := build_recovery(o, ledger_excess, account_excess, account_cost))]
    if not plans:
        print("[reconcile] 没有可补记的委托（差异已消或证据不足）")
        return 0

    for p in plans:
        print(
            f"  {p.agent} {p.code} {p.side} {p.volume:g} @{p.price:.3f} "
            f"← 单 {p.order_id}（{p.trade_date}）"
        )
    if not apply:
        print("[reconcile] DRY-RUN（未写库）。确认后加 --apply")
        return 1

    outcomes = []
    async with get_session() as db:
        for p in plans:
            out = await apply_fill(
                db,
                tenant_id=TENANT,
                user_id=USER,
                agent=p.agent,
                code=p.code,
                side=p.side,
                volume=p.volume,
                price=p.price,
                fill_key=f"{FILL_KEY_PREFIX}:{p.order_id}:{p.agent}",
                trade_date=p.trade_date,
                order_id=p.order_id,
                filled_at=p.filled_at,
                market=market_of(p.code),
                exit_reason="expired_recovery",
            )
            if out.duplicate:
                print(f"  ⚠️ 幂等命中（已补过）：{p.order_id} {p.agent}")
                continue
            if abs(out.applied - p.volume) > EPS:
                raise RuntimeError(f"{p.agent} {p.code} 只记 {out.applied}/{p.volume}：{out.note}")
            outcomes.append((p, out))
        await db.commit()

    # 写后复验：重算差异
    async with get_session(read_only=True) as db:
        ledger_after = await load_ledger(db)
    real_after, _ = await load_tdx_account_volumes(TENANT, USER)
    le, ae = compute_mismatch(ledger_after, real_after or {})
    print(
        f"[reconcile] 补记 {len(outcomes)} 笔；写后差异：账本多 {le or '无'}，账户多 {ae or '无'}"
    )
    return 0


async def _account_cost() -> dict[str, float]:
    """当前 TDX 账户成本价（买单补记用；单笔建仓时即成交价）。"""
    from sqlalchemy import text

    from backend.shared.decision.manual_sell_auto_absorb import _decode_payload

    sql = (
        "SELECT payload_json FROM real_account_snapshots "
        "WHERE tenant_id=:t AND user_id=:u AND source='tdx_bridge' "
        "ORDER BY snapshot_at DESC LIMIT 1"
    )
    async with get_session(read_only=True) as db:
        row = (await db.execute(text(sql), {"t": TENANT, "u": USER})).fetchone()
    if not row:
        return {}
    payload = _decode_payload(row[0])
    out: dict[str, float] = {}
    for item in payload.get("positions") or []:
        if not isinstance(item, dict):
            continue
        code = to_suffix(item.get("symbol"))
        cost = float(item.get("cost_price") or 0)
        if code and cost > 0:
            out[code] = cost
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="补记 TDX 过期委托的实际成交（默认 DRY-RUN）")
    parser.add_argument("--apply", action="store_true", help="实际写库")
    args = parser.parse_args(argv)
    return asyncio.run(run(args.apply))


if __name__ == "__main__":
    raise SystemExit(main())
