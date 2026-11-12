#!/usr/bin/env python3
"""修复被「EOD 取 T-1 收盘价」污染的模拟盘日终快照。

背景
----
`eod_service` 曾在 D 当天 15:00 就触发 D 的日终结算，而 D 的日线要等次日
凌晨同步才落盘，取价退化成 D-1 收盘价，于是 D 的日终快照被写成 T-1 估值。
表现为：每日收益率整体错后一天，前端「今日盈亏」混入前一日的涨跌。

本脚本按「目标交易日收盘价」重算指定日期区间的：
  1. simulation_position_daily（close_price / market_value / unrealized_pnl）
  2. simulation_account_daily（市值 / 权益 / 当日盈亏 / 累计盈亏）
  3. simulation_fund_snapshots（total_asset / market_value / today_pnl / total_pnl）

现金不动（价格重估不影响现金），持仓数量/成本不动，只重估市值并重算盈亏。
按日期升序处理，当日盈亏用「上一日重算后的总资产」作日初基线。

用法
----
    python backend/scripts/repair_sim_eod_snapshot_prices.py \
        --start 2026-11-09 --end 2026-11-11 --tenant default --user 10000001
    # 确认无误后加 --apply 落库（默认 dry-run，只打印）

幂等：同一区间重复执行结果一致；`--apply` 前会先把受影响行备份到
`qm_backup_<ts>_*` 表。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import text  # noqa: E402

from backend.services.simulation.services.local_market_data import (  # noqa: E402
    get_local_market_data,
)
from backend.shared.database_manager_v2 import get_session  # noqa: E402


def _parse_date(raw: str) -> date:
    return datetime.strptime(raw, "%Y-%m-%d").date()


async def _load_prev_total(
    session, tenant_id: str, user_id: str, before: date
) -> float | None:
    row = (
        await session.execute(
            text(
                "SELECT total_asset FROM simulation_fund_snapshots "
                "WHERE tenant_id = :t AND user_id = :u AND snapshot_date < :d "
                "ORDER BY snapshot_date DESC LIMIT 1"
            ),
            {"t": tenant_id, "u": user_id, "d": before},
        )
    ).fetchone()
    return float(row[0]) if row and row[0] is not None else None


async def _load_dates(
    session, tenant_id: str, user_id: str, start: date, end: date
) -> list[date]:
    rows = (
        await session.execute(
            text(
                "SELECT DISTINCT snapshot_date FROM simulation_fund_snapshots "
                "WHERE tenant_id = :t AND user_id = :u "
                "AND snapshot_date BETWEEN :s AND :e "
                "ORDER BY snapshot_date ASC"
            ),
            {"t": tenant_id, "u": user_id, "s": start, "e": end},
        )
    ).fetchall()
    return [row[0] for row in rows]


async def _plan_date(
    session, *, tenant_id: str, user_id: str, snap_date: date, prev_total: float | None
) -> dict | None:
    fund = (
        await session.execute(
            text(
                "SELECT total_asset, available_balance, frozen_balance, "
                "market_value, initial_capital, today_pnl, total_pnl, source "
                "FROM simulation_fund_snapshots "
                "WHERE tenant_id = :t AND user_id = :u AND snapshot_date = :d"
            ),
            {"t": tenant_id, "u": user_id, "d": snap_date},
        )
    ).mappings().first()
    if fund is None:
        return None

    positions = (
        await session.execute(
            text(
                "SELECT symbol, position_side, quantity, cost_price, close_price, "
                "market_value, unrealized_pnl FROM simulation_position_daily "
                "WHERE tenant_id = :t AND user_id = :u AND snapshot_date = :d"
                " ORDER BY symbol"
            ),
            {"t": tenant_id, "u": user_id, "d": snap_date},
        )
    ).mappings().all()

    lmd = get_local_market_data("CN")
    rows: list[dict] = []
    missing: list[str] = []
    long_mv = 0.0
    short_mv = 0.0
    if not positions:
        # 非交易日（周末/节假日）快照没有持仓明细，市值沿用上一交易日，
        # 不属于本 BUG 的污染范围，原样保留且不推进基线。
        return {"date": snap_date, "skip": True, "reason": "no_positions", "rows": []}
    for pos in positions:
        bar = lmd.get_bar(pos["symbol"], snap_date)
        if bar is None or not float(bar.close or 0.0):
            missing.append(str(pos["symbol"]))
            continue
        close = float(bar.close)
        qty = float(pos["quantity"] or 0.0)
        mv = round(close * qty, 2)
        upnl = round(mv - float(pos["cost_price"] or 0.0) * qty, 2)
        if str(pos["position_side"] or "long").strip().lower() == "short":
            short_mv += mv
        else:
            long_mv += mv
        rows.append(
            {
                "symbol": pos["symbol"],
                "side": pos["position_side"],
                "close_price": close,
                "market_value": mv,
                "unrealized_pnl": upnl,
                "old_close": float(pos["close_price"] or 0.0),
                "old_mv": float(pos["market_value"] or 0.0),
            }
        )

    if missing:
        return {"date": snap_date, "missing": missing, "rows": [], "skip": True}

    cash = float(fund["available_balance"] or 0.0) + float(
        fund["frozen_balance"] or 0.0
    )
    new_total = round(cash + long_mv - short_mv, 4)
    initial = float(fund["initial_capital"] or 0.0) or new_total
    day_open = prev_total if prev_total is not None else new_total
    return {
        "date": snap_date,
        "rows": rows,
        "missing": [],
        "skip": False,
        "cash": cash,
        "long_mv": round(long_mv, 4),
        "short_mv": round(short_mv, 4),
        "new_total": new_total,
        "old_total": float(fund["total_asset"] or 0.0),
        "initial": initial,
        "day_open": day_open,
        "today_pnl": round(new_total - day_open, 4),
        "total_pnl": round(new_total - initial, 4),
        "old_today_pnl": float(fund["today_pnl"] or 0.0),
        "old_total_pnl": float(fund["total_pnl"] or 0.0),
        "old_market_value": float(fund["market_value"] or 0.0),
        "source": str(fund["source"] or ""),
    }


async def _apply_date(session, *, tenant_id: str, user_id: str, plan: dict) -> None:
    snap_date = plan["date"]
    for row in plan["rows"]:
        await session.execute(
            text(
                "UPDATE simulation_position_daily SET close_price = :cp, "
                "market_value = :mv, unrealized_pnl = :upnl, updated_at = now() "
                "WHERE tenant_id = :t AND user_id = :u AND snapshot_date = :d "
                "AND symbol = :sym AND position_side = :side"
            ),
            {
                "cp": row["close_price"],
                "mv": row["market_value"],
                "upnl": row["unrealized_pnl"],
                "t": tenant_id,
                "u": user_id,
                "d": snap_date,
                "sym": row["symbol"],
                "side": row["side"],
            },
        )

    await session.execute(
        text(
            "UPDATE simulation_account_daily SET long_market_value = :lmv, "
            "short_market_value = :smv, total_asset = :total, equity = :total, "
            "daily_pnl = :dpnl, total_pnl = :tpnl "
            "WHERE tenant_id = :t AND user_id = :u AND snapshot_date = :d"
        ),
        {
            "lmv": plan["long_mv"],
            "smv": plan["short_mv"],
            "total": plan["new_total"],
            "dpnl": plan["today_pnl"],
            "tpnl": plan["total_pnl"],
            "t": tenant_id,
            "u": user_id,
            "d": snap_date,
        },
    )

    await session.execute(
        text(
            "UPDATE simulation_fund_snapshots SET total_asset = :total, "
            "market_value = :mv, today_pnl = :dpnl, total_pnl = :tpnl, "
            "initial_capital = :init, updated_at = now() "
            "WHERE tenant_id = :t AND user_id = :u AND snapshot_date = :d"
        ),
        {
            "total": plan["new_total"],
            "mv": round(plan["long_mv"] - plan["short_mv"], 4),
            "dpnl": plan["today_pnl"],
            "tpnl": plan["total_pnl"],
            "init": plan["initial"],
            "t": tenant_id,
            "u": user_id,
            "d": snap_date,
        },
    )


async def _backup(session, *, tenant_id: str, user_id: str, start: date, end: date) -> str:
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    pairs = (
        ("simulation_position_daily", "pos_daily"),
        ("simulation_account_daily", "acct_daily"),
        ("simulation_fund_snapshots", "fund_snap"),
    )
    names = []
    for table, suffix in pairs:
        bak = f"qm_backup_{stamp}_{suffix}"
        await session.execute(
            text(
                f'CREATE TABLE "{bak}" AS SELECT * FROM {table} '
                "WHERE tenant_id = :t AND user_id = :u "
                "AND snapshot_date BETWEEN :s AND :e"
            ),
            {"t": tenant_id, "u": user_id, "s": start, "e": end},
        )
        names.append(bak)
    return ", ".join(names)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--tenant", default="default")
    parser.add_argument("--user", default="10000001")
    parser.add_argument(
        "--apply", action="store_true", help="实际落库（默认 dry-run 只打印）"
    )
    args = parser.parse_args()

    start = _parse_date(args.start)
    end = _parse_date(args.end)
    if end < start:
        print("end 必须 >= start")
        return 2

    tenant_id, user_id = args.tenant, str(args.user)
    lmd = get_local_market_data("CN")
    print(f"本地日线最新交易日: {lmd.latest_trade_date()}")
    print(f"修复区间: {start} ~ {end}  账户: {tenant_id}/{user_id}")
    print(f"模式: {'APPLY' if args.apply else 'DRY-RUN'}")

    async with get_session(read_only=not args.apply) as session:
        dates = await _load_dates(session, tenant_id, user_id, start, end)
        if not dates:
            print("区间内没有资金快照，无需修复")
            return 0

        prev_total = await _load_prev_total(session, tenant_id, user_id, start)
        print(f"区间前一日基线 total_asset: {prev_total}")

        plans = []
        for snap_date in dates:
            plan = await _plan_date(
                session,
                tenant_id=tenant_id,
                user_id=user_id,
                snap_date=snap_date,
                prev_total=prev_total,
            )
            if plan is None:
                continue
            if plan.get("skip"):
                reason = plan.get("reason") or "missing_bar"
                if reason == "no_positions":
                    print(f"[跳过] {snap_date} 无持仓明细（非交易日），保留原值")
                else:
                    print(
                        f"[跳过] {snap_date} 缺少日线: {','.join(plan['missing'][:5])}"
                        f"{' ...' if len(plan['missing']) > 5 else ''}"
                    )
                continue
            plans.append(plan)
            prev_total = plan["new_total"]

        print()
        header = (
            f"{'date':<12}{'old_total':>14}{'new_total':>14}{'delta':>12}"
            f"{'old_today':>12}{'new_today':>12}{'chg_px':>8}"
        )
        print(header)
        for plan in plans:
            changed = sum(
                1
                for row in plan["rows"]
                if abs(row["close_price"] - row["old_close"]) > 1e-9
            )
            print(
                f"{str(plan['date']):<12}{plan['old_total']:>14,.2f}"
                f"{plan['new_total']:>14,.2f}"
                f"{plan['new_total'] - plan['old_total']:>12,.2f}"
                f"{plan['old_today_pnl']:>12,.2f}{plan['today_pnl']:>12,.2f}"
                f"{changed:>8}"
            )

        if not args.apply:
            print("\nDRY-RUN：未写入。确认无误后加 --apply 执行。")
            return 0

        bak = await _backup(
            session, tenant_id=tenant_id, user_id=user_id, start=start, end=end
        )
        print(f"\n已备份: {bak}")
        for plan in plans:
            await _apply_date(session, tenant_id=tenant_id, user_id=user_id, plan=plan)
        await session.commit()
        print(f"已修复 {len(plans)} 个交易日")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
