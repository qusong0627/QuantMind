#!/usr/bin/env python3
"""真账户日度台账权益一致性修复：把「总资产 < 现金+市值」的存量行归一到两分量之和。

背景：写侧归一（``real_account_ledger_service.normalize_equity``，2026-09-23 引入）只
作用于**新写入**的行；本脚本处理历史存量行，返修走**同一实现**，禁另写口径。
实况（2026-09-03/04，tdx 桥）：total 字段被读错掉 161,058（−17.5%）而同行 cash/mv
未动——该值进了日度台账，除污染账户图与风控档位回撤输入外，还沿 ``day_open_equity``
（写侧=「上一交易日快照 total 的逐字拷贝」）传下去，派生列出现假的 −17.72% / +12.85%
/ +7.32% 三天。

规则（幂等；修复后的行 = 「当初写入时 total 就是对的」会得到的行）：
1. 直接命中：重建值（现金+市值）高出上报总资产的差额 > max(100 元, 重建值×1%)，
   且两分量都 > 0；反方向（total ≥ cash+mv，冻结/在途）不动 ⇒ total_asset 换重建值；
2. 传播链：某行 ``day_open_equity`` 逐分等于同账户（tenant/user/account 自然键三元组，
   同一 account_id 可存在于多个 user_id 空间）①中某行的**坏值** ⇒ 重锚到其重建值
   （跨账户/跨空间同数字不误命中）；
3. 派生列（monthly_pnl_raw / daily_return_pct / total_return_pct）一律用
   ``derive_equity_returns`` 按换锚后的值重算；``DERIVED_PNL_SOURCES``（本方派生盈亏的
   source）的 today/total_pnl_raw 同口径重算，其余 source 的上报值禁动；
4. 留痕：payload_json 记录 ``equity_normalized`` / ``equity_anchor_repaired``
   （规则/原值/重建值/时间）。

用法（容器内）:
    python backend/scripts/repair_ledger_equity.py                    # DRY-RUN（默认）
    python backend/scripts/repair_ledger_equity.py --since 2026-09-01
    python backend/scripts/repair_ledger_equity.py --apply            # 实际写库

退出码：DRY-RUN 有待修行 → 1（可当体检项）；无待修行或 --apply 成功 → 0。
--apply 时读主库（DRY-RUN 走从库，仅列清单）。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.services.trade.services.real_account_ledger_service import (  # noqa: E402
    DERIVED_PNL_SOURCES,
    derive_equity_returns,
    normalize_equity,
)


def _f(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def plan_repairs(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """存量行 → 待修清单（纯函数）。

    两遍扫描，判据全部来自写侧同一 ``normalize_equity`` / ``derive_equity_returns``：
    ① 直接命中（total 被读错）；② day_open 逐分等于 ① 的坏值（写侧逐字拷贝的传播链）。
    每个计划带 ``updates``（要写的列）与 ``guard``（compare-and-set 期望原值）。
    """
    now_iso = datetime.now(timezone.utc).isoformat()

    # ① 直接命中 + 每账户「坏值 → 重建值」表（键=自然键三元组：同一 account_id
    # 可能同时存在于多个 user_id/tenant 空间，只按 account_id 分组会跨空间互相重锚）
    caught: dict[Any, float] = {}
    bad_anchor: dict[tuple[str, str, str], dict[float, float]] = {}

    def _account_key(row: dict[str, Any]) -> tuple[str, str, str]:
        return (
            str(row.get("tenant_id") or ""),
            str(row.get("user_id") or ""),
            str(row.get("account_id") or ""),
        )

    for row in rows:
        equity, evidence = normalize_equity(
            row.get("total_asset"), row.get("cash"), row.get("market_value")
        )
        if evidence is None:
            continue
        caught[row.get("id")] = equity
        bad_anchor.setdefault(_account_key(row), {})[
            round(evidence["raw_total_asset"], 2)
        ] = equity

    plans: list[dict[str, Any]] = []
    for row in rows:
        rid = row.get("id")
        account_id = str(row.get("account_id") or "")
        total_new = caught.get(rid)
        current_day_open = _f(row.get("day_open_equity"))
        day_open_new = None
        if current_day_open:
            day_open_new = bad_anchor.get(_account_key(row), {}).get(
                round(current_day_open, 2)
            )
        if total_new is None and day_open_new is None:
            continue

        final_total = total_new if total_new is not None else _f(row.get("total_asset"))
        final_day_open = day_open_new if day_open_new is not None else current_day_open
        initial_equity = _f(row.get("initial_equity"))
        derived = derive_equity_returns(
            total_asset=final_total,
            day_open_equity=final_day_open,
            month_open_equity=_f(row.get("month_open_equity")),
            initial_equity=initial_equity,
            today_pnl=_f(row.get("today_pnl_raw")),
            total_pnl=_f(row.get("total_pnl_raw")),
        )

        updates: dict[str, Any] = {
            "daily_return_pct": derived["daily_return_pct"],
            "total_return_pct": derived["total_return_pct"],
            "monthly_pnl_raw": derived["monthly_pnl_raw"],
        }
        guard: list[tuple[str, Any]] = []
        mark: dict[str, Any] = {}
        if total_new is not None:
            updates["total_asset"] = total_new
            guard.append(("total_asset", _f(row.get("total_asset"))))
            mark["equity_normalized"] = {
                "rule": "equity_underreport",
                "raw_total_asset": _f(row.get("total_asset")),
                "rebuilt": total_new,
                "gap": round(total_new - _f(row.get("total_asset")), 2),
                "normalized_at": now_iso,
                "repaired_by": "repair_ledger_equity.py",
            }
        if day_open_new is not None:
            updates["day_open_equity"] = day_open_new
            guard.append(("day_open_equity", current_day_open))
            mark["equity_anchor_repaired"] = {
                "field": "day_open_equity",
                "rule": "day_open_copies_bad_total",
                "raw": current_day_open,
                "rebuilt": day_open_new,
                "repaired_at": now_iso,
                "repaired_by": "repair_ledger_equity.py",
            }
        if str(row.get("source") or "") in DERIVED_PNL_SOURCES:
            updates["today_pnl_raw"] = final_total - (final_day_open or final_total)
            updates["total_pnl_raw"] = final_total - (initial_equity or final_total)

        payload = dict(row.get("payload_json") or {})
        payload.update(mark)
        plans.append(
            {
                "id": rid,
                "tenant_id": str(row.get("tenant_id") or ""),
                "user_id": str(row.get("user_id") or ""),
                "account_id": account_id,
                "snapshot_date": row.get("snapshot_date"),
                "total_raw": _f(row.get("total_asset")),
                "total_new": total_new,
                "day_open_raw": current_day_open,
                "day_open_new": day_open_new,
                "daily_return_old": _f(row.get("daily_return_pct")),
                "daily_return_new": derived["daily_return_pct"],
                "updates": updates,
                "guard": guard,
                "payload_json": payload,
            }
        )
    return plans


def build_update_statement(plan: dict[str, Any], ledger_model: Any):
    """计划 → UPDATE 语句（含 compare-and-set 守卫：只改「原值仍是扫描所见」的行）。

    独立成函数以便测试真语句（guard 被摘 = 静默覆盖他人写入，必须被断言抓住）。
    """
    from sqlalchemy import update

    conditions = [ledger_model.id == plan["id"]]
    for column, expected in plan["guard"]:
        conditions.append(getattr(ledger_model, column) == expected)
    return (
        update(ledger_model)
        .where(*conditions)
        .values(**plan["updates"], payload_json=plan["payload_json"])
    )


async def run(*, apply: bool, since: date | None, until: date | None) -> int:
    from sqlalchemy import select

    from backend.services.trade_shared.models.real_account_ledger import (
        RealAccountLedgerDailySnapshot as Ledger,
    )
    from backend.shared.database_manager_v2 import close_database, get_session

    try:
        async with get_session(read_only=not apply) as session:
            stmt = select(
                Ledger.id,
                Ledger.tenant_id,
                Ledger.user_id,
                Ledger.account_id,
                Ledger.snapshot_date,
                Ledger.total_asset,
                Ledger.cash,
                Ledger.market_value,
                Ledger.day_open_equity,
                Ledger.month_open_equity,
                Ledger.initial_equity,
                Ledger.today_pnl_raw,
                Ledger.total_pnl_raw,
                Ledger.daily_return_pct,
                Ledger.source,
                Ledger.payload_json,
            ).order_by(Ledger.snapshot_date, Ledger.account_id)
            if since is not None:
                stmt = stmt.where(Ledger.snapshot_date >= since)
            if until is not None:
                stmt = stmt.where(Ledger.snapshot_date <= until)
            rows = [dict(r._mapping) for r in (await session.execute(stmt)).all()]
            plans = plan_repairs(rows)

            print(f"[repair-ledger] 扫描 {len(rows)} 行；待修 {len(plans)} 行")
            for plan in plans[:50]:
                bits = []
                if plan["total_new"] is not None:
                    bits.append(
                        f"total {plan['total_raw']:,.2f} -> {plan['total_new']:,.2f}"
                    )
                if plan["day_open_new"] is not None:
                    bits.append(
                        f"day_open {plan['day_open_raw']:,.2f} -> {plan['day_open_new']:,.2f}"
                    )
                bits.append(
                    f"日收益 {plan['daily_return_old']:.2f}% -> {plan['daily_return_new']:.2f}%"
                )
                print(
                    f"  {plan['snapshot_date']} user={plan['user_id']} "
                    f"{plan['account_id']}: " + "；".join(bits)
                )
            if len(plans) > 50:
                print(f"  ...（其余 {len(plans) - 50} 行略）")

            if not apply:
                print(
                    "[repair-ledger] DRY-RUN（未写库）。确认清单后加 --apply 执行；"
                    f"退出码：有待修行=1，干净=0（本次 {'1' if plans else '0'}）。"
                )
                return 1 if plans else 0

            updated = 0
            missed = 0
            for plan in plans:
                # compare-and-set：行在扫描与本条更新之间被改写（竞态）则跳过并点名，
                # 不覆盖他人的新值（幂等：下一轮扫描会再核对一次）。
                result = await session.execute(build_update_statement(plan, Ledger))
                if getattr(result, "rowcount", 0):
                    updated += 1
                else:
                    missed += 1
                    print(
                        f"  [WARN] 行已被改写，跳过 id={plan['id']} "
                        f"{plan['snapshot_date']} {plan['account_id']}"
                    )
            print(f"[repair-ledger] 已修 {updated} 行（原值留痕 payload_json）")
            if missed:
                print(f"[repair-ledger] 跳过 {missed} 行（并发改写），请重跑核对")
            return 0
    finally:
        await close_database()


def _parse_day(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="真账户日度台账权益一致性修复（默认 DRY-RUN）"
    )
    parser.add_argument("--apply", action="store_true", help="实际写库（默认只列清单）")
    parser.add_argument(
        "--since", type=_parse_day, default=None, help="只扫 >= 该日（YYYY-MM-DD）"
    )
    parser.add_argument(
        "--until", type=_parse_day, default=None, help="只扫 <= 该日（YYYY-MM-DD）"
    )
    args = parser.parse_args()
    return asyncio.run(run(apply=args.apply, since=args.since, until=args.until))


if __name__ == "__main__":
    sys.exit(main())
