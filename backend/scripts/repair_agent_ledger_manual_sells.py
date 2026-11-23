#!/usr/bin/env python3
"""把**未归属**的真单卖出成交吸收进分账账本（人工/风控单造成的幻影仓修复）。

背景：``agent_ledger_fill`` 纪律 1——``orders.agent`` 为空的成交（人工在桥里点的
单、风控单）**不写分账**。人工清仓后账本留下幻影仓：读侧（复盘/对话提示词）已有
真实账户闸门兜底，但决策轮「已用额度」、实况页 agent 净值、arena 台账仍按账本算。
本脚本把「真实账户已清空、且有未归属卖出成交」的持仓按**真实成交价**吸收掉，走
``apply_fill`` 同一实现——现金回补与回合行（``qm_agent_ledger_roundtrip``）口径与
真单完全一致，已实现盈亏归到名下模型。

吸收判据（**全部满足**才写；任何一条不满足只报告不写）：

1. 该标的已不在真实账户（``real_account_snapshots``，与决策轮同一读法
   :func:`backend.shared.real_positions.load_real_positions`，多源并集/停更剔除）；
2. 窗口内 ``orders.agent`` 为空、``side=sell``、``status=filled`` 的成交总量足以
   覆盖账本该标的合计持仓（不足 = 账本本来就没记那么多，**不猜**）；
3. 账本每一行的 ``buy_ts`` 早于分配到的卖出成交（卖出之后才买的仓不由这笔解释；
   ``buy_ts`` 缺失同样不动——无法证明它当时存在）。

分配：按 ``buy_ts`` FIFO 逐行吃卖出成交（一笔单可跨多家 agent，一家也可跨多笔单），
每一段一条流水。幂等键 ``manual-absorb:<order_id>:<agent>``——账本 fill 唯一键是
``(tenant, user, trade_date, fill_key)``，**不含 agent**，故键里必须带 agent：两家
同持一只票时第二家的流水才不会被幂等守卫吞掉。``trade_date`` 取**成交日**（不是
运行日），``filled_at`` 取成交瞬时（桥报的北京墙钟）。

用法（容器内；后端代码是 bind mount，脚本立即可用）：

    python backend/scripts/repair_agent_ledger_manual_sells.py            # DRY-RUN（默认）
    python backend/scripts/repair_agent_ledger_manual_sells.py --since 2026-10-14
    python backend/scripts/repair_agent_ledger_manual_sells.py --apply    # 实际写库

退出码：DRY-RUN 有吸收计划 = 1（可当体检项），干净 = 0；``--apply`` 成功且回读
复验通过 = 0，写库或复验失败 = 2。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from collections.abc import Mapping, Sequence
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.shared.decision.manual_sell_absorption import (  # noqa: E402
    EPS,
    EXIT_REASON,
    REASON_TEXT,
    AbsorbFill,
    AbsorbPlan,
    expected_cash,
    fill_fields,
    plan_absorption,
    to_float,
    to_suffix_code,
)

#: 身份：与决策轮/复盘同一口径（管理员族规范名 10000001）。
LEDGER_TENANT = "default"
DEFAULT_LEDGER_USER = "10000001"

#: 默认回看天数：人工清仓往往在成交后一两天才被发现。
DEFAULT_LOOKBACK_DAYS = 7

#: 默认最大快照年龄（秒）：桥每 30s 同步一次账户，超过这个窗口说明桥掉线，
#: 真实持仓不是「现在」的，拒绝对账。
DEFAULT_MAX_SNAPSHOT_AGE_S = 1800

BEIJING = ZoneInfo("Asia/Shanghai")


# ── DB 读（容器内走 asyncpg；口径与决策轮/交易服务一致）────────────────


async def load_ledger_rows(
    session: Any, tenant: str, user: str
) -> list[dict[str, Any]]:
    from sqlalchemy import text

    rows = (
        await session.execute(
            text(
                "SELECT agent, code, volume, cost_price, buy_ts "
                "FROM qm_agent_ledger_position "
                "WHERE tenant_id = :tid AND user_id = :uid "
                "ORDER BY code, buy_ts NULLS FIRST, agent"
            ),
            {"tid": tenant, "uid": user},
        )
    ).fetchall()
    return [dict(r._mapping) for r in rows]


async def load_accounts(session: Any, tenant: str, user: str) -> dict[str, float]:
    from sqlalchemy import text

    rows = (
        await session.execute(
            text(
                "SELECT agent, virtual_cash FROM qm_agent_ledger_account "
                "WHERE tenant_id = :tid AND user_id = :uid"
            ),
            {"tid": tenant, "uid": user},
        )
    ).fetchall()
    return {str(r[0]): to_float(r[1]) for r in rows}


async def load_unattributed_sells(
    session: Any, tenant: str, user: str, since: datetime
) -> list[dict[str, Any]]:
    """窗口内 ``orders.agent`` 为空的已成交卖出 → 成交面聚合。

    数量/价格**以 ``trades`` 为准**（成交是事实），没有成交明细才回落到委托的
    ``filled_quantity``/``average_price``；``orders.price`` 是限价，绝不当成交价。
    """
    from sqlalchemy import text

    rows = (
        await session.execute(
            text(
                "SELECT o.order_id::text AS order_id, o.symbol AS code, "
                "       o.filled_quantity, o.average_price, o.created_at, "
                "       COALESCE(SUM(t.quantity), 0) AS trade_volume, "
                "       CASE WHEN COALESCE(SUM(t.quantity), 0) > 0 "
                "            THEN SUM(t.quantity * t.price) / SUM(t.quantity) "
                "            ELSE NULL END AS trade_price, "
                "       MIN(t.executed_at) AS first_trade_at "
                "FROM orders o "
                "LEFT JOIN trades t "
                "  ON t.order_id = o.order_id "
                " AND t.tenant_id = o.tenant_id AND t.user_id = o.user_id "
                "WHERE o.tenant_id = :tid AND o.user_id = :uid "
                "  AND COALESCE(o.agent, '') = '' "
                "  AND o.side::text = 'sell' "
                "  AND o.status::text = 'filled' "
                "  AND o.filled_quantity > 0 "
                "  AND o.created_at >= :since "
                "GROUP BY o.order_id, o.symbol, o.filled_quantity, "
                "         o.average_price, o.created_at "
                "ORDER BY o.created_at"
            ),
            {"tid": tenant, "uid": user, "since": since},
        )
    ).fetchall()

    out: list[dict[str, Any]] = []
    for row in rows:
        rec = dict(row._mapping)
        volume = to_float(rec.get("trade_volume")) or to_float(
            rec.get("filled_quantity")
        )
        price = to_float(rec.get("trade_price")) or to_float(rec.get("average_price"))
        wall = rec.get("first_trade_at") or rec.get("created_at")
        if volume <= 0 or price <= 0 or not isinstance(wall, datetime):
            print(
                f"⚠️ 跳过未归属卖单 {rec.get('order_id')} {rec.get('code')}："
                f"成交量/价/时间不完整（volume={volume} price={price} at={wall}）"
            )
            continue
        trade_volume = to_float(rec.get("trade_volume"))
        filled_quantity = to_float(rec.get("filled_quantity"))
        if trade_volume > 0 and abs(trade_volume - filled_quantity) > EPS:
            print(
                f"⚠️ 单 {rec.get('order_id')} {rec.get('code')} 成交明细 "
                f"{trade_volume:g} 股与委托累计 {filled_quantity:g} 股不一致：以成交明细为准"
            )
        out.append(
            {
                "order_id": str(rec.get("order_id") or ""),
                "code": to_suffix_code(rec.get("code")),
                "volume": volume,
                "price": price,
                "executed_at": _wall_to_utc(wall),
                "trade_date": wall.date(),
                "agent": "",
            }
        )
    return out


async def load_unattributed_buys(
    session: Any, tenant: str, user: str, since: datetime
) -> list[dict[str, Any]]:
    """窗口内 ``orders.agent`` 为空的已成交买入 → 成交面聚合（人工买入归属）。

    与 :func:`load_unattributed_sells` 完全同口径（数量/价格以 ``trades`` 为准，
    回落委托累计），只是方向为 ``buy``。
    """
    from sqlalchemy import text

    rows = (
        await session.execute(
            text(
                "SELECT o.order_id::text AS order_id, o.symbol AS code, "
                "       o.filled_quantity, o.average_price, o.created_at, "
                "       COALESCE(SUM(t.quantity), 0) AS trade_volume, "
                "       CASE WHEN COALESCE(SUM(t.quantity), 0) > 0 "
                "            THEN SUM(t.quantity * t.price) / SUM(t.quantity) "
                "            ELSE NULL END AS trade_price, "
                "       MIN(t.executed_at) AS first_trade_at "
                "FROM orders o "
                "LEFT JOIN trades t "
                "  ON t.order_id = o.order_id "
                " AND t.tenant_id = o.tenant_id AND t.user_id = o.user_id "
                "WHERE o.tenant_id = :tid AND o.user_id = :uid "
                "  AND COALESCE(o.agent, '') = '' "
                "  AND o.side::text = 'buy' "
                "  AND o.status::text = 'filled' "
                "  AND o.filled_quantity > 0 "
                "  AND o.created_at >= :since "
                "GROUP BY o.order_id, o.symbol, o.filled_quantity, "
                "         o.average_price, o.created_at "
                "ORDER BY o.created_at"
            ),
            {"tid": tenant, "uid": user, "since": since},
        )
    ).fetchall()

    out: list[dict[str, Any]] = []
    for row in rows:
        rec = dict(row._mapping)
        volume = to_float(rec.get("trade_volume")) or to_float(
            rec.get("filled_quantity")
        )
        price = to_float(rec.get("trade_price")) or to_float(rec.get("average_price"))
        wall = rec.get("first_trade_at") or rec.get("created_at")
        if volume <= 0 or price <= 0 or not isinstance(wall, datetime):
            print(
                f"⚠️ 跳过未归属买单 {rec.get('order_id')} {rec.get('code')}："
                f"成交量/价/时间不完整（volume={volume} price={price} at={wall}）"
            )
            continue
        out.append(
            {
                "order_id": str(rec.get("order_id") or ""),
                "code": to_suffix_code(rec.get("code")),
                "volume": volume,
                "price": price,
                "executed_at": _wall_to_utc(wall),
                "trade_date": wall.date(),
                "agent": "",
            }
        )
    return out


def _wall_to_utc(value: datetime) -> datetime:
    """北京墙钟（naive）→ aware UTC；本来就是 aware 的原样归一。"""
    if value.tzinfo is None:
        return value.replace(tzinfo=BEIJING).astimezone(timezone.utc)
    return value.astimezone(timezone.utc)


def _snapshot_age_s(meta: Mapping[str, Any], *, now: datetime) -> float | None:
    raw = meta.get("snapshot_at")
    if isinstance(raw, str):
        try:
            raw = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(raw, datetime):
        return None
    if raw.tzinfo is None:
        raw = raw.replace(tzinfo=timezone.utc)
    return (now - raw.astimezone(timezone.utc)).total_seconds()


async def load_real_volumes(
    tenant: str, user: str, *, max_age_s: int
) -> tuple[dict[str, float] | None, str]:
    """真实持仓量（suffix 键）+ 结论说明；读不到/太旧返回 ``(None, 原因)``。"""
    from backend.shared.real_positions import load_real_positions

    try:
        positions, meta = await load_real_positions(tenant, user)
    except Exception as exc:  # noqa: BLE001 读不到就整批不动，由调用方打印
        return None, f"读取失败：{exc}"

    age_s = _snapshot_age_s(meta, now=datetime.now(timezone.utc))
    if age_s is None:
        return None, "快照无时间戳"
    if age_s > max_age_s:
        return None, f"快照 {age_s:.0f}s 前（阈值 {max_age_s}s），桥可能掉线"

    out: dict[str, float] = {}
    for symbol, pos in positions.items():
        volume = to_float(pos.get("volume"))
        if volume > 0:
            out[to_suffix_code(symbol)] = volume
    return out, f"快照 {age_s:.0f}s 前，{len(out)} 只持仓"


# ── 写库（apply_fill 同一实现）─────────────────────────────────────────


def _write_backup(payload: Mapping[str, Any]) -> Path:
    """写前留痕（持仓/账户/计划原样落 JSON），修复后可逐笔对账。"""
    directory = PROJECT_ROOT / "logs"
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(BEIJING).strftime("%Y%m%d_%H%M%S")
    path = directory / f"repair_agent_ledger_manual_sells_{stamp}.json"
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    return path


async def apply_plan(
    plan: AbsorbPlan, *, tenant: str, user: str, quota: float | None = None
) -> list[dict[str, Any]]:
    """在一个事务里逐段 ``apply_fill``；任何一段异常/重复/未记满 → 整批回滚。

    ``quota`` 只在**首次开户**生效（人工买入的 ``manual`` 账户要按真实账户规模开户，
    否则虚拟现金会被一笔大额人工买入打成大额负数）。
    """
    from backend.shared.agent_ledger_fill import market_of
    from backend.shared.agent_ledger_store import apply_fill
    from backend.shared.database_manager_v2 import get_session

    extra: dict[str, Any] = {"quota": float(quota)} if quota and quota > 0 else {}
    outcomes: list[dict[str, Any]] = []
    async with get_session() as session:
        for fill in plan.fills:
            outcome = await apply_fill(
                session,
                tenant_id=tenant,
                user_id=user,
                agent=fill.agent,
                code=fill.code,
                side=fill.side if fill.side in ("buy", "sell") else "sell",
                volume=fill.volume,
                price=fill.price,
                fill_key=fill.fill_key,
                trade_date=fill.trade_date,
                order_id=fill.order_id,
                filled_at=fill.filled_at,
                market=market_of(fill.code),
                exit_reason=EXIT_REASON,
                **extra,
            )
            if outcome.duplicate:
                raise RuntimeError(
                    f"fill_key 已存在（{fill.fill_key}）：账本已被另一轮修复写过？"
                )
            if abs(outcome.applied - fill.volume) > EPS:
                raise RuntimeError(
                    f"{fill.agent} {fill.code} 只记上 {outcome.applied:g}/{fill.volume:g} 股："
                    f"{outcome.note or '账本持仓与计划不一致'}"
                )
            if outcome.note:
                raise RuntimeError(
                    f"{fill.agent} {fill.code} 记账留痕非空：{outcome.note}"
                )
            outcomes.append(
                {
                    "fill_key": fill.fill_key,
                    "agent": fill.agent,
                    "code": fill.code,
                    "side": fill.side,
                    "volume": fill.volume,
                    "price": fill.price,
                    "virtual_cash": outcome.virtual_cash,
                }
            )
    return outcomes


async def verify_plan(
    plan: AbsorbPlan,
    outcomes: Sequence[Mapping[str, Any]],
    *,
    tenant: str,
    user: str,
    accounts_before: Mapping[str, float],
    ledger_before: Sequence[Mapping[str, Any]] | None = None,
    quota: float | None = None,
) -> list[str]:
    """写后回读复验：持仓等于计划期末、现金等于逐步预期、流水行 applied/note 正确。

    ``ledger_before`` 给了就按**期望期末持仓**校验（部分吸收后 agent 仍有仓是正常的）；
    缺省保持旧行为（只允许清仓）。人工买入会新建 ``manual`` 账户，现金校验用
    ``quota`` 作为开户初始值。
    """
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import get_session

    problems: list[str] = []
    by_agent: dict[str, list[AbsorbFill]] = defaultdict(list)
    for fill in plan.fills:
        by_agent[fill.agent].append(fill)

    expected_end: dict[tuple[str, str], float] = {}
    if ledger_before is not None:
        for rec in ledger_before:
            key = (str(rec.get("agent") or ""), str(rec.get("code") or ""))
            expected_end[key] = expected_end.get(key, 0.0) + to_float(rec.get("volume"))
        for fill in plan.fills:
            key = (fill.agent, fill.code)
            sign = 1.0 if fill.side == "buy" else -1.0
            expected_end[key] = expected_end.get(key, 0.0) + sign * fill.volume

    async with get_session() as session:
        for agent, agent_fills in by_agent.items():
            for code in {f.code for f in agent_fills}:
                row = (
                    await session.execute(
                        text(
                            "SELECT volume FROM qm_agent_ledger_position "
                            "WHERE tenant_id = :tid AND user_id = :uid "
                            "AND agent = :agent AND code = :code"
                        ),
                        {"tid": tenant, "uid": user, "agent": agent, "code": code},
                    )
                ).first()
                got = to_float(row[0]) if row is not None else 0.0
                if ledger_before is None:
                    if row is not None:
                        problems.append(
                            f"{agent} {code} 仍有 {got:g} 股（应已清仓）"
                        )
                    continue
                want = expected_end.get((agent, code), 0.0)
                if abs(got - want) > EPS:
                    problems.append(
                        f"{agent} {code} 持仓 {got:g} 股 ≠ 计划期末 {want:g} 股"
                    )

        for fill in plan.fills:
            row = (
                await session.execute(
                    text(
                        "SELECT applied_volume, note FROM qm_agent_ledger_fill "
                        "WHERE tenant_id = :tid AND user_id = :uid "
                        "AND trade_date = :day AND fill_key = :key"
                    ),
                    {
                        "tid": tenant,
                        "uid": user,
                        "day": fill.trade_date,
                        "key": fill.fill_key,
                    },
                )
            ).first()
            if row is None:
                problems.append(f"流水缺失：{fill.fill_key}")
                continue
            applied, note = to_float(row[0]), str(row[1] or "")
            if abs(applied - fill.volume) > EPS or note:
                problems.append(
                    f"流水异常：{fill.fill_key} applied={applied:g} note={note!r}"
                )

        cash_rows = (
            await session.execute(
                text(
                    "SELECT agent, virtual_cash FROM qm_agent_ledger_account "
                    "WHERE tenant_id = :tid AND user_id = :uid"
                ),
                {"tid": tenant, "uid": user},
            )
        ).fetchall()
        cash_now = {str(r[0]): to_float(r[1]) for r in cash_rows}
        for agent, agent_fills in by_agent.items():
            base = accounts_before.get(agent)
            if base is None:
                base = float(quota or 0.0)
            want = expected_cash(base, agent_fills)
            got = cash_now.get(agent)
            if got is None or abs(got - want) > 0.005:
                problems.append(
                    f"{agent} 现金 {got} ≠ 预期 {want}（写前 {base}）"
                )

    # 事务内返回值也要对得上（两层证据：apply_fill 的 outcome + 提交后回读）。
    # outcomes 按计划顺序追加，故按同一顺序累加就是每笔记账后的期望现金。
    planned_by_key = {f.fill_key: f for f in plan.fills}
    applied: dict[str, list[AbsorbFill]] = defaultdict(list)
    for outcome in outcomes:
        fill = planned_by_key.get(str(outcome.get("fill_key") or ""))
        if fill is None:
            problems.append(f"返回值里有计划外的流水：{outcome.get('fill_key')}")
            continue
        applied[fill.agent].append(fill)
        got = outcome.get("virtual_cash")
        if got is None:
            problems.append(f"{fill.fill_key} 未返回记账后现金")
            continue
        base = accounts_before.get(fill.agent)
        if base is None:
            base = float(quota or 0.0)
        want = expected_cash(base, applied[fill.agent])
        if abs(to_float(got) - want) > 0.005:
            problems.append(
                f"{fill.agent} 事务内现金 {got} ≠ 逐步预期 {want}（{fill.fill_key}）"
            )
    return problems


# ── 报告 ──────────────────────────────────────────────────────────────


def print_plan(plan: AbsorbPlan, accounts: Mapping[str, float]) -> None:
    if plan.skipped:
        for skip in plan.skipped:
            detail = f"（{skip.detail}）" if skip.detail else ""
            print(
                f"  跳过 {skip.code}：{REASON_TEXT.get(skip.reason, skip.reason)}{detail}"
            )
    if not plan.fills:
        print("[manual-absorb] 没有可吸收的持仓")
        return

    print(f"[manual-absorb] 吸收 {len(plan.fills)} 段：")
    for fill in plan.fills:
        closing = "清仓" if fill.closes else "减仓"
        print(
            f"  {fill.agent} {fill.code} {fill.volume:g} 股 @{fill.price:.4f} "
            f"← 单 {fill.order_id}；回款 {fill.proceeds:,.2f}"
            f"（成本 {fill.cost_price:.4f}，盈亏 {fill.realized_pnl:+,.2f}，{closing}）"
        )
    print("  合计：")
    for agent, proceeds in sorted(plan.proceeds_by_agent.items()):
        before = accounts.get(agent, 0.0)
        after = expected_cash(before, [f for f in plan.fills if f.agent == agent])
        print(
            f"    {agent}: 现金 {before:,.2f} → {after:,.2f}"
            f"（+{proceeds:,.2f}，已实现盈亏 {plan.pnl_by_agent.get(agent, 0.0):+,.2f}）"
        )


def plan_payload(
    plan: AbsorbPlan,
    *,
    tenant: str,
    user: str,
    ledger_rows: Sequence[Mapping[str, Any]],
    accounts: Mapping[str, float],
    sells: Sequence[Mapping[str, Any]],
    real_note: str,
) -> dict[str, Any]:
    return {
        "tenant": tenant,
        "user": user,
        "created_at": datetime.now(BEIJING).isoformat(),
        "real_positions": real_note,
        "ledger_positions": [dict(r) for r in ledger_rows],
        "accounts_before": dict(accounts),
        "unattributed_sells": [dict(s) for s in sells],
        "fills": [fill_fields(f) for f in plan.fills],
        "skipped": [asdict(s) for s in plan.skipped],
    }


# ── 入口 ──────────────────────────────────────────────────────────────


def _identity() -> tuple[str, str]:
    from backend.shared.simulation_account_keys import normalize_runtime_user

    raw = os.getenv("QM_DECISION_ACCOUNT_USER_ID", "").strip()
    return LEDGER_TENANT, normalize_runtime_user(raw or DEFAULT_LEDGER_USER)


async def run(args: argparse.Namespace) -> int:
    from backend.shared.database_manager_v2 import close_database, get_session

    tenant, user = _identity()
    now_beijing = datetime.now(BEIJING)
    since = now_beijing.replace(tzinfo=None) - timedelta(days=args.days)

    async with get_session() as session:
        ledger_rows = await load_ledger_rows(session, tenant, user)
        accounts = await load_accounts(session, tenant, user)
        sells = await load_unattributed_sells(session, tenant, user, since)

    real_volumes, real_note = await load_real_volumes(
        tenant, user, max_age_s=args.max_snapshot_age
    )

    print(
        f"[manual-absorb] tenant={tenant} user={user}；"
        f"账本 {len(ledger_rows)} 行 / 未归属卖出 {len(sells)} 笔 / 窗口 {since:%Y-%m-%d} 起；"
        f"真实持仓：{real_note}"
    )
    if real_volumes is None and not args.allow_stale_snapshot:
        print(
            "拒绝执行：真实持仓不可用于对账（--allow-stale-snapshot 可强制，但会失去兜底判据）"
        )
        await close_database()
        return 2
    if real_volumes is None:
        print("⚠️ --allow-stale-snapshot：真实持仓不可读，本次不吸收任何仓位")

    plan = plan_absorption(ledger_rows, sells, real_volumes)
    print_plan(plan, accounts)
    if plan.skipped and not plan.fills:
        print("（全部跳过：没有可吸收的持仓）")

    if not plan.fills:
        await close_database()
        return 0

    if not args.apply:
        print(
            "[manual-absorb] DRY-RUN（未写库）。确认清单后加 --apply 执行；"
            "退出码：有吸收计划=1，干净=0（本次 1）。"
        )
        await close_database()
        return 1

    payload = plan_payload(
        plan,
        tenant=tenant,
        user=user,
        ledger_rows=ledger_rows,
        accounts=accounts,
        sells=sells,
        real_note=real_note,
    )
    backup = _write_backup(payload)
    print(f"[manual-absorb] 写前留痕：{backup}")

    try:
        outcomes = await apply_plan(plan, tenant=tenant, user=user)
    except Exception as exc:
        print(f"[manual-absorb] 写库失败（事务已回滚）：{exc}")
        await close_database()
        return 2

    problems = await verify_plan(
        plan,
        outcomes,
        tenant=tenant,
        user=user,
        accounts_before=accounts,
    )
    await close_database()
    if problems:
        print("[manual-absorb] ❌ 写后复验失败：")
        for problem in problems:
            print(f"  - {problem}")
        print("（事务已提交，留痕 JSON 可据此回滚/对账）")
        return 2

    print("[manual-absorb] ✅ 写后复验通过：持仓已清、现金与流水一致")
    for agent, proceeds in sorted(plan.proceeds_by_agent.items()):
        print(
            f"  {agent}: 现金 {expected_cash(accounts.get(agent, 0.0), [f for f in plan.fills if f.agent == agent]):,.2f}"
            f"（+{proceeds:,.2f}）"
        )
    return 0


def _parse_since(value: str) -> int:
    """``--since YYYY-MM-DD`` → 回看天数（只支持近 N 天，避免误扫全历史）。"""
    try:
        day = date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"--since 需要 YYYY-MM-DD：{value}") from exc
    days = (datetime.now(BEIJING).date() - day).days
    if days < 0:
        raise argparse.ArgumentTypeError("--since 不能是未来日期")
    return days


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="把未归属真单卖出吸收进分账账本（默认 DRY-RUN）"
    )
    parser.add_argument(
        "--apply", action="store_true", help="实际写库（缺省只打印计划）"
    )
    parser.add_argument(
        "--since",
        dest="days",
        type=_parse_since,
        default=DEFAULT_LOOKBACK_DAYS,
        metavar="YYYY-MM-DD",
        help=f"回看起点（缺省近 {DEFAULT_LOOKBACK_DAYS} 天）",
    )
    parser.add_argument(
        "--max-snapshot-age",
        type=int,
        default=DEFAULT_MAX_SNAPSHOT_AGE_S,
        help=f"真实持仓快照最大年龄秒数（缺省 {DEFAULT_MAX_SNAPSHOT_AGE_S}）",
    )
    parser.add_argument(
        "--allow-stale-snapshot",
        action="store_true",
        help="真实持仓不可读/过旧时仍继续（此时不吸收任何仓位）",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
