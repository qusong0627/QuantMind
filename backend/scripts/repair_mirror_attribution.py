#!/usr/bin/env python3
"""把「桥回报超时 → agent=NULL 桥委托行成交」修回真正下单的模型（一次性修账）。

背景（2026-11-27 五芳斋/福恩实录）：模型镜像单提交后桥 ACK 超时（``[AWAITING_BRIDGE_ACK]``），
订单停在 ``submitted``；桥自己的委托行以 ``agent=NULL`` 落库并成交，自动吸收把成交记成了
``manual-buy``。于是：真实持仓与盈亏挂在人工单桶，模型看不到自己的仓，同标的后续买单还被
``l3.inflight_dup`` 误拦（glm 11:00 的福恩买单）。

本脚本按「唯一镜像单」配对，做四件事（**一个事务**，写前 backup、写后复验）：

1. 人工单桶冲回：按原价/原量记一笔 ``manual-absorb:repair...`` 卖出，持仓归零、现金复原；
2. 真单成交记回模型：``apply_fill`` 用 ``trades.exchange_trade_id`` 作幂等键（与 C14 同一
   配对键），数量/价格/成交时刻照抄 ``trades``；
3. 桥委托行 ``agent`` 置为模型（C14 的 trades⨝orders 归属随之正确），备注打修复标记；
4. 镜像单置 ``cancelled`` + ``[SUPERSEDED_BY_BRIDGE:...]``：退出在途集合，不再误拦同标的
   后续买单；委托号留在桥行（镜像行不写，避免唯一键冲突）。

用法（容器内；``data/`` 是挂载卷）::

    python backend/scripts/repair_mirror_attribution.py --day 2026-11-27
    python backend/scripts/repair_mirror_attribution.py --day 2026-11-27 --apply \
        --record /data/logs/repair_mirror_attribution_20261127.json

退出码：0 成功/干跑；2 参数/校验/写库/复验失败（事务已回滚）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

CST = timezone(timedelta(hours=8))
#: 镜像单与桥委托行的最大落库时差（桥排队可能很久：实测 18 分钟）
PAIR_WINDOW_S = 1800
#: 冲回流水前缀：C14 把 ``manual-absorb:`` 视为不配对流水；自动吸收也据此跳过该 order
REPAIR_PREFIX = "manual-absorb:repair"


@dataclass(frozen=True)
class Pair:
    """一对「镜像单 ↔ 桥委托行」+ 成交事实（全部来自库，不猜）。"""

    agent: str
    bridge_order: str
    mirror_order: str
    code: str
    volume: float
    price: float
    trade_key: str
    filled_at: datetime
    trade_date: date
    manual_fill_key: str
    manual_volume: float
    bridge_exchange_id: str
    mirror_quantity: float
    bridge_quantity: float


def _jsonable(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def _day_bounds(day: date) -> tuple[datetime, datetime]:
    """``orders.created_at`` 是 naive 北京墙钟列（不是 timestamptz），窗口也只能给 naive。"""
    start = datetime(day.year, day.month, day.day)
    return start, start + timedelta(days=1)


async def find_pairs(session: Any, day: date) -> tuple[list[Pair], list[str]]:
    """找出「无 agent 的桥成交 + 唯一镜像单 + manual-buy 流水」配对。"""
    from sqlalchemy import text

    start, end = _day_bounds(day)
    rows = (
        (
            await session.execute(
                text(
                    """
                    SELECT o.order_id::text AS bridge_order, o.symbol AS code,
                           o.side::text AS side, o.quantity AS bridge_qty,
                           o.filled_quantity AS filled_qty,
                           o.average_price AS avg_price,
                           o.exchange_order_id AS exchange_id,
                           t.exchange_trade_id AS trade_key,
                           t.quantity AS trade_qty, t.price AS trade_price,
                           t.executed_at AS trade_at
                    FROM orders o
                    JOIN trades t ON t.order_id = o.order_id
                                 AND t.tenant_id = o.tenant_id
                                 AND t.user_id = o.user_id
                    WHERE o.tenant_id = 'default' AND o.user_id = :uid
                      AND COALESCE(o.agent, '') = ''
                      AND o.status::text = 'filled'
                      AND o.filled_quantity > 0
                      AND o.created_at >= :start AND o.created_at < :end
                    ORDER BY o.created_at
                    """
                ),
                {"uid": _account_user(), "start": start, "end": end},
            )
        )
        .mappings()
        .all()
    )
    pairs: list[Pair] = []
    skipped: list[str] = []
    seen_bridge: set[str] = set()
    for r in rows:
        bridge = str(r["bridge_order"])
        if bridge in seen_bridge:
            # 同一委托多笔成交：第一笔已配对（本脚本按委托整笔修，部分成交另行处理）
            skipped.append(f"{bridge} {r['code']}：同一委托多笔成交，仅取第一笔")
            continue
        seen_bridge.add(bridge)
        raw_trade_at = r["trade_at"]
        trade_at = raw_trade_at if isinstance(raw_trade_at, datetime) else None
        if trade_at is None:
            skipped.append(f"{bridge} {r['code']}：成交时刻缺失")
            continue
        trade_at_aware = _aware_cst(trade_at)
        candidates = (
            (
                await session.execute(
                    text(
                        """
                        SELECT m.order_id::text AS mirror_order, m.agent AS agent,
                               m.quantity AS quantity, m.created_at AS created_at
                        FROM orders m
                        WHERE m.tenant_id = 'default' AND m.user_id = :uid
                          AND COALESCE(m.agent, '') <> ''
                          AND m.side::text = :side
                          AND m.symbol = :code
                          AND ABS(m.quantity - :qty) < 0.5
                          AND m.client_order_id LIKE 'mir-%'
                          AND ABS(EXTRACT(EPOCH FROM (m.created_at - :created_at)))
                              <= :window
                        """
                    ),
                    {
                        "uid": _account_user(),
                        "side": r["side"],
                        "code": r["code"],
                        "qty": r["bridge_qty"],
                        "created_at": trade_at,
                        "window": PAIR_WINDOW_S,
                    },
                )
            )
            .mappings()
            .all()
        )
        if len(candidates) != 1:
            skipped.append(
                f"{bridge} {r['code']}：镜像候选 {len(candidates)} 个（要求恰好 1 个）"
            )
            continue
        mirror = candidates[0]
        manual = (
            (
                await session.execute(
                    text(
                        """
                        SELECT fill_key, volume FROM qm_agent_ledger_fill
                        WHERE tenant_id = 'default' AND user_id = :uid
                          AND agent = 'manual' AND order_id = :oid
                          AND fill_key LIKE 'manual-buy:%'
                        ORDER BY id LIMIT 1
                        """
                    ),
                    {"uid": _account_user(), "oid": bridge},
                )
            )
            .mappings()
            .first()
        )
        if manual is None:
            skipped.append(f"{bridge} {r['code']}：没有 manual-buy 流水（无需冲回）")
            continue
        volume = float(r["trade_qty"] or 0)
        price = float(r["trade_price"] or 0)
        if volume <= 0 or price <= 0:
            skipped.append(f"{bridge} {r['code']}：成交数量/价格不完整")
            continue
        pairs.append(
            Pair(
                agent=str(mirror["agent"]),
                bridge_order=bridge,
                mirror_order=str(mirror["mirror_order"]),
                code=str(r["code"]),
                volume=volume,
                price=price,
                trade_key=str(r["trade_key"]),
                filled_at=trade_at_aware,
                trade_date=day,
                manual_fill_key=str(manual["fill_key"]),
                manual_volume=float(manual["volume"] or 0),
                bridge_exchange_id=str(r["exchange_id"] or ""),
                mirror_quantity=float(mirror["quantity"] or 0),
                bridge_quantity=float(r["bridge_qty"] or 0),
            )
        )
    return pairs, skipped


def _account_user() -> str:
    from backend.shared.simulation_account_keys import resolve_db_account_user
    from backend.services.trade.services.decision_round_core import ENV_ACCOUNT_USER

    return resolve_db_account_user(ENV_ACCOUNT_USER)


def _aware_cst(value: Any) -> datetime:
    """``trades.executed_at`` 是 naive 北京墙钟（容器实测）→ aware CST。"""
    if not isinstance(value, datetime):
        raise ValueError(f"成交时刻不是 datetime：{value!r}")
    return value.replace(tzinfo=CST) if value.tzinfo is None else value.astimezone(CST)


async def _backup(session: Any, pairs: list[Pair]) -> dict[str, Any]:
    from sqlalchemy import bindparam, text

    order_ids = [p.bridge_order for p in pairs] + [p.mirror_order for p in pairs]
    orders = (
        (
            await session.execute(
                text("SELECT * FROM orders WHERE order_id::text IN :ids").bindparams(
                    bindparam("ids", expanding=True)
                ),
                {"ids": order_ids},
            )
        )
        .mappings()
        .all()
    )
    trades = (
        (
            await session.execute(
                text(
                    "SELECT * FROM trades WHERE order_id::text IN :ids"
                ).bindparams(bindparam("ids", expanding=True)),
                {"ids": [p.bridge_order for p in pairs]},
            )
        )
        .mappings()
        .all()
    )
    ledger = (
        (
            await session.execute(
                text(
                    "SELECT * FROM qm_agent_ledger_fill WHERE order_id IN :ids"
                ).bindparams(bindparam("ids", expanding=True)),
                {"ids": [p.bridge_order for p in pairs]},
            )
        )
        .mappings()
        .all()
    )
    accounts = (
        (
            await session.execute(
                text(
                    "SELECT * FROM qm_agent_ledger_account WHERE tenant_id='default'"
                )
            )
        )
        .mappings()
        .all()
    )
    positions = (
        (
            await session.execute(
                text(
                    "SELECT * FROM qm_agent_ledger_position WHERE tenant_id='default'"
                )
            )
        )
        .mappings()
        .all()
    )
    return {
        "orders": [_jsonable(dict(r)) for r in orders],
        "trades": [_jsonable(dict(r)) for r in trades],
        "ledger_fill": [_jsonable(dict(r)) for r in ledger],
        "ledger_account": [_jsonable(dict(r)) for r in accounts],
        "ledger_position": [_jsonable(dict(r)) for r in positions],
    }


async def apply_plan(session: Any, pairs: list[Pair]) -> None:
    """一个事务：manual 冲回 → 模型入账 → 订单归属/终态。"""
    from sqlalchemy import text

    from backend.shared.agent_ledger_store import apply_fill

    user = _account_user()
    for p in pairs:
        # ① manual 冲回：同价同量卖出；键走 manual-absorb 前缀（C14 不配对/吸收不再重复处理）
        await apply_fill(
            session,
            tenant_id="default",
            user_id=user,
            agent="manual",
            code=p.code,
            side="sell",
            volume=p.volume,
            price=p.price,
            fill_key=f"{REPAIR_PREFIX}:{p.manual_fill_key}:manual",
            trade_date=p.trade_date,
            order_id=p.bridge_order,
            filled_at=datetime.now(CST),
            exit_reason="mirror_attr_repair",
        )
        # ② 模型入账：幂等键 = 成交表的 exchange_trade_id（C14 同键配对）
        await apply_fill(
            session,
            tenant_id="default",
            user_id=user,
            agent=p.agent,
            code=p.code,
            side="buy",
            volume=p.volume,
            price=p.price,
            fill_key=p.trade_key,
            trade_date=p.trade_date,
            order_id=p.bridge_order,
            filled_at=p.filled_at,
        )
        # ③ 桥委托行归属模型（trades⨝orders 的 agent 由此正确）
        await session.execute(
            text(
                """
                UPDATE orders SET agent = :agent,
                    remarks = COALESCE(NULLIF(remarks, ''), '通达信桥委托')
                              || ' [ATTRIB_REPAIRED: mirror ' || :mirror || ']'
                WHERE order_id::text = :oid
                """
            ),
            {"agent": p.agent, "mirror": p.mirror_order, "oid": p.bridge_order},
        )
        # ④ 镜像单退出在途：置 cancelled + 指向桥委托号（保留客户单号供审计）
        await session.execute(
            text(
                """
                UPDATE orders SET status = 'cancelled',
                    remarks = COALESCE(NULLIF(remarks, ''), '')
                              || ' [SUPERSEDED_BY_BRIDGE:'
                              || COALESCE(:exchange, '') || ']'
                WHERE order_id::text = :oid
                """
            ),
            {"exchange": p.bridge_exchange_id, "oid": p.mirror_order},
        )


async def verify(session: Any, pairs: list[Pair]) -> list[str]:
    """写后复验：账本持仓/现金、归属、在途、C14 同键配对。返回失败项（空=通过）。"""
    from sqlalchemy import text

    user = _account_user()
    problems: list[str] = []
    for p in pairs:
        row = (
            (
                await session.execute(
                    text(
                        """
                        SELECT agent, fill_key, volume FROM qm_agent_ledger_fill
                        WHERE tenant_id='default' AND user_id=:uid
                          AND order_id=:oid AND fill_key=:key
                        """
                    ),
                    {"uid": user, "oid": p.bridge_order, "key": p.trade_key},
                )
            )
            .mappings()
            .first()
        )
        if row is None or str(row["agent"]) != p.agent:
            problems.append(f"{p.code}: 模型买入流水缺失/归属不对（{dict(row) if row else None}）")
        manual_pos = (
            (
                await session.execute(
                    text(
                        "SELECT volume FROM qm_agent_ledger_position "
                        "WHERE tenant_id='default' AND user_id=:uid AND agent='manual' "
                        "AND code=:code"
                    ),
                    {"uid": user, "code": p.code},
                )
            )
            .mappings()
            .first()
        )
        if manual_pos is not None and float(manual_pos["volume"] or 0) > 0:
            problems.append(f"{p.code}: manual 仍有持仓 {manual_pos['volume']}")
        model_pos = (
            (
                await session.execute(
                    text(
                        "SELECT volume, cost_price FROM qm_agent_ledger_position "
                        "WHERE tenant_id='default' AND user_id=:uid AND agent=:agent "
                        "AND code=:code"
                    ),
                    {"uid": user, "agent": p.agent, "code": p.code},
                )
            )
            .mappings()
            .first()
        )
        if model_pos is None or float(model_pos["volume"] or 0) < p.volume:
            problems.append(f"{p.code}: {p.agent} 持仓不足（{dict(model_pos) if model_pos else None}）")
        bridge_agent = (
            (
                await session.execute(
                    text("SELECT agent FROM orders WHERE order_id::text=:oid"),
                    {"oid": p.bridge_order},
                )
            )
            .scalar()
        )
        if str(bridge_agent or "") != p.agent:
            problems.append(f"{p.code}: 桥委托行 agent={bridge_agent!r}，应为 {p.agent}")
        in_flight = (
            (
                await session.execute(
                    text(
                        "SELECT status::text FROM orders WHERE order_id::text=:oid"
                    ),
                    {"oid": p.mirror_order},
                )
            )
            .scalar()
        )
        if str(in_flight or "").lower() in {"pending", "submitted", "partially_filled"}:
            problems.append(f"{p.code}: 镜像单仍在途（{in_flight}）")
        # C14 同键：成交行的 exchange_trade_id ↔ 账本 fill_key，且 order 归属一致
        pair_ok = (
            (
                await session.execute(
                    text(
                        """
                        SELECT 1 FROM trades t JOIN orders o ON o.order_id=t.order_id
                        JOIN qm_agent_ledger_fill f
                          ON f.tenant_id=t.tenant_id AND f.user_id=t.user_id
                         AND f.order_id=t.order_id::text
                         AND f.fill_key=t.exchange_trade_id
                        WHERE t.order_id::text=:oid AND f.agent=o.agent
                        """
                    ),
                    {"oid": p.bridge_order},
                )
            )
            .first()
        )
        if pair_ok is None:
            problems.append(f"{p.code}: C14 同键配对失败")
    return problems


async def _main(args: argparse.Namespace) -> int:
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import get_session

    day = datetime.strptime(args.day, "%Y-%m-%d").date()
    async with get_session() as session:
        pairs, skipped = await find_pairs(session, day)
        print(f"[repair] 配对 {len(pairs)} 笔；跳过 {len(skipped)} 条")
        for s in skipped:
            print("  - 跳过：", s)
        for p in pairs:
            print(
                f"  * {p.code} {p.volume:g}@{p.price:g} → {p.agent}"
                f"（桥 {p.bridge_order[:8]} / 镜像 {p.mirror_order[:8]} / 键 {p.trade_key}）"
            )
        if not pairs:
            return 0
        backup = await _backup(session, pairs)
        record: dict[str, Any] = {
            "day": args.day,
            "apply": bool(args.apply),
            "pairs": [_jsonable(asdict(p)) for p in pairs],
            "skipped": skipped,
            "before": backup,
        }
        if not args.apply:
            print("[repair] 干跑：未写库（加 --apply 执行）")
            if args.record:
                Path(args.record).write_text(
                    json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            return 0
        await apply_plan(session, pairs)
        problems = await verify(session, pairs)
        if problems:
            await session.rollback()
            print("[repair] 复验失败，已回滚：")
            for p in problems:
                print("  ✗", p)
            return 2
        await session.commit()
        print("[repair] 已提交并复验通过")
        record["problems"] = []
        if args.record:
            Path(args.record).write_text(
                json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
            )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="镜像单归因修复（manual → 真正下单的模型）")
    parser.add_argument("--day", required=True, help="交易日 YYYY-MM-DD")
    parser.add_argument("--apply", action="store_true", help="真正写库（默认干跑）")
    parser.add_argument("--record", default="", help="修复记录 JSON 落盘路径")
    args = parser.parse_args()
    try:
        return asyncio.run(_main(args))
    except Exception as exc:  # noqa: BLE001
        print(f"[repair] 失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
