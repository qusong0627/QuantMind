#!/usr/bin/env python3
"""按**账户快照证据**补记桥回报缺失的真单成交（601838 案例，2026-10-30）。

为什么需要它：桥的 order 回报停在 ``submitted``/``expired``、``trades`` 里没有
卖出成交，但 ``real_account_snapshots`` 的持仓与现金在两次 30 秒快照之间**只**
601838 发生了变化：

* 10-29 10:05:58 400 股 / cash 850,314.26 → 10:06:29 200 股 / 854,227.09
  （+3,912.83 = 200 × 19.56415 净价）
* 10-30 14:01:51 200 股 / cash 854,227.09 → 14:02:21 0 股 / 858,211.89
  （+3,984.80 = 200 × 19.92400 净价）

同一快照里 601825 等其它持仓数量/现金均未变，故这两次现金增量只能来自
601838 卖出。价格是**净价**（券商费用已含在现金里），与券商的剩余成本
（18.806 → 18.047，按「总成本 − 卖出净额」回推）逐分吻合。

本脚本做四件事（一个事务）：
1. ``apply_fill`` 把两笔卖出记进 PG 账本（历史 ``trade_date``/``filled_at``，
   幂等键 = ``cum-<日>-<委托号>-<累计量>``，与真单同形）；
2. 补 ``trades`` 行（``exchange_trade_id`` = 同一个键，价格=净价、费用 0 并在
   remarks 标明「净价，费用已含」——不臆造毛利/费率拆分）；
3. 把两张 ``orders`` 从 expired/submitted 改成 filled（补 filled_quantity/
   average_price/filled_at/备注），让委托台账与账本一致；
4. 写前 backup + 写后复验，全部落 ``--record`` JSON。

用法（容器内；``data/`` 是挂载卷）::

    python backend/scripts/repair_agent_ledger_recovered_fills.py \
        --path /data/logs/recovered_fills_601838_20261030.json
    python backend/scripts/repair_agent_ledger_recovered_fills.py \
        --path /data/logs/recovered_fills_601838_20261030.json \
        --apply --record /data/logs/recovered_fills_601838_20261030_record.json

退出码：0 成功/干跑；2 参数/校验/写库/复验失败（事务已回滚）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

CST = timezone(timedelta(hours=8))


@dataclass(frozen=True, slots=True)
class RecoveredFill:
    code: str
    side: str
    volume: int
    price: float
    trade_date: date
    filled_at: datetime
    order_id: str
    internal_order_id: str
    fill_key: str
    exit_reason: str
    note: str


@dataclass(frozen=True, slots=True)
class RecoveredPlan:
    tenant_id: str
    user_id: str
    agent: str
    quota: float
    code: str
    evidence: str
    fills: tuple[RecoveredFill, ...]


def _parse_ts(value: Any, field: str) -> datetime:
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value or "").strip()
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{field} 不是 ISO 时间：{text!r}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=CST)
    return dt.astimezone(timezone.utc)


def _parse_date(value: Any, field: str) -> date:
    text = str(value or "").strip().replace("-", "")
    if len(text) != 8 or not text.isdigit():
        raise ValueError(f"{field} 不是 YYYYMMDD/YYYY-MM-DD：{value!r}")
    return date(int(text[:4]), int(text[4:6]), int(text[6:8]))


def _finite(value: Any, field: str, *, positive: bool = False) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} 不是数字：{value!r}") from exc
    if not math.isfinite(out) or (positive and out <= 0):
        raise ValueError(f"{field} 非有限或非正：{value!r}")
    return out


def load_plan(path: Path) -> RecoveredPlan:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, Mapping):
        raise ValueError("顶层必须是对象")
    agent = str(data.get("agent") or "").strip()
    if not agent:
        raise ValueError("agent 为空")
    tenant_id = str(data.get("tenant_id") or "default").strip() or "default"
    user_id = str(data.get("user_id") or "").strip()
    if not user_id:
        raise ValueError("user_id 为空")
    quota = _finite(data.get("quota"), "quota", positive=True)
    code = str(data.get("code") or "").strip()
    raw_fills = data.get("fills")
    if not isinstance(raw_fills, Sequence) or not raw_fills:
        raise ValueError("fills 必须是非空数组")
    fills: list[RecoveredFill] = []
    keys: set[str] = set()
    for index, raw in enumerate(raw_fills, start=1):
        if not isinstance(raw, Mapping):
            raise ValueError(f"fills[{index}] 不是对象")
        fill_code = str(raw.get("code") or code or "").strip()
        if not fill_code:
            raise ValueError(f"fills[{index}].code 为空")
        side = str(raw.get("side") or "sell").strip().lower()
        if side not in ("buy", "sell"):
            raise ValueError(f"fills[{index}].side 只认 buy/sell：{side!r}")
        volume = int(_finite(raw.get("volume"), f"fills[{index}].volume", positive=True))
        price = _finite(raw.get("price"), f"fills[{index}].price", positive=True)
        key = str(raw.get("fill_key") or "").strip()
        if not key or key in keys:
            raise ValueError(f"fills[{index}].fill_key 为空或重复：{key!r}")
        keys.add(key)
        internal = str(raw.get("internal_order_id") or "").strip()
        UUID(internal)  # 形态非法当场 ValueError
        fills.append(
            RecoveredFill(
                code=fill_code,
                side=side,
                volume=volume,
                price=price,
                trade_date=_parse_date(
                    raw.get("trade_date"), f"fills[{index}].trade_date"
                ),
                filled_at=_parse_ts(raw.get("filled_at"), f"fills[{index}].filled_at"),
                order_id=str(raw.get("order_id") or "").strip(),
                internal_order_id=internal,
                fill_key=key,
                exit_reason=str(raw.get("exit_reason") or "fill_recovered")[:32],
                note=str(raw.get("note") or "")[:500],
            )
        )
    return RecoveredPlan(
        tenant_id=tenant_id,
        user_id=user_id,
        agent=agent,
        quota=quota,
        code=code,
        evidence=str(data.get("evidence") or "")[:2000],
        fills=tuple(fills),
    )


def _jsonable(row: Any) -> dict[str, Any]:
    return {str(k): (v.isoformat() if isinstance(v, datetime) else v) for k, v in dict(row).items()}


async def _backup(session: Any, plan: RecoveredPlan) -> dict[str, Any]:
    from sqlalchemy import text

    params = {
        "t": plan.tenant_id,
        "u": plan.user_id,
        "a": plan.agent,
        "c": plan.code,
        "ids": [f.internal_order_id for f in plan.fills],
    }
    out: dict[str, Any] = {}
    for name, sql in (
        (
            "account",
            "SELECT * FROM qm_agent_ledger_account "
            "WHERE tenant_id=:t AND user_id=:u AND agent=:a",
        ),
        (
            "positions",
            "SELECT * FROM qm_agent_ledger_position "
            "WHERE tenant_id=:t AND user_id=:u AND agent=:a ORDER BY code",
        ),
        (
            "fills",
            "SELECT * FROM qm_agent_ledger_fill "
            "WHERE tenant_id=:t AND user_id=:u AND agent=:a AND code=:c "
            "ORDER BY trade_date, id",
        ),
        (
            "roundtrips",
            "SELECT * FROM qm_agent_ledger_roundtrip "
            "WHERE tenant_id=:t AND user_id=:u AND agent=:a AND code=:c "
            "ORDER BY sell_ts, id",
        ),
        (
            "orders",
            "SELECT * FROM orders WHERE order_id::text = ANY(:ids)",
        ),
        (
            "trades",
            "SELECT * FROM trades WHERE order_id::text = ANY(:ids)",
        ),
    ):
        res = await session.execute(text(sql), params)
        out[name] = [_jsonable(r) for r in res.mappings().all()]
    return out


async def apply_plan(plan: RecoveredPlan) -> dict[str, Any]:
    from sqlalchemy import select, text

    from backend.services.trade_shared.models.enums import (
        OrderSide,
        OrderStatus,
        PositionSide,
        TradeAction,
        TradingMode,
    )
    from backend.services.trade_shared.models.order import Order
    from backend.services.trade_shared.models.trade import Trade
    from backend.shared.agent_ledger_store import apply_fill
    from backend.shared.agent_ledger_fill import market_of
    from backend.shared.database_manager_v2 import get_session

    async with get_session() as session:
        before = await _backup(session, plan)
        outcomes: list[dict[str, Any]] = []
        trades_inserted: list[str] = []
        orders_updated: list[str] = []
        for fill in plan.fills:
            outcome = await apply_fill(
                session,
                tenant_id=plan.tenant_id,
                user_id=plan.user_id,
                agent=plan.agent,
                code=fill.code,
                side=fill.side,
                volume=fill.volume,
                price=fill.price,
                fill_key=fill.fill_key,
                trade_date=fill.trade_date,
                order_id=fill.order_id,
                filled_at=fill.filled_at,
                quota=plan.quota,
                market=market_of(fill.code),
                exit_reason=fill.exit_reason,
            )
            if outcome.duplicate:
                raise RuntimeError(f"fill_key 已存在，拒绝重复补账：{fill.fill_key}")
            if abs(outcome.applied - fill.volume) > 1e-6:
                raise RuntimeError(
                    f"{fill.fill_key} 只记上 {outcome.applied:g}/{fill.volume:g} 股："
                    f"{outcome.note or '账本与计划不一致'}"
                )
            if outcome.note:
                raise RuntimeError(f"{fill.fill_key} 记账留痕非空：{outcome.note}")
            outcomes.append(
                {
                    "fill_key": fill.fill_key,
                    "applied": outcome.applied,
                    "price": fill.price,
                    "trade_date": fill.trade_date.isoformat(),
                    "filled_at": fill.filled_at.isoformat(),
                }
            )

            exists = await session.execute(
                select(Trade.trade_id).where(
                    Trade.tenant_id == plan.tenant_id,
                    Trade.user_id == plan.user_id,
                    Trade.exchange_trade_id == fill.fill_key,
                )
            )
            if exists.first() is None:
                session.add(
                    Trade(
                        trade_id=uuid4(),
                        order_id=UUID(fill.internal_order_id),
                        tenant_id=plan.tenant_id,
                        user_id=plan.user_id,
                        portfolio_id=0,
                        symbol=fill.code,
                        symbol_name="成都银行",
                        side=OrderSide.SELL if fill.side == "sell" else OrderSide.BUY,
                        trade_action=TradeAction.SELL_TO_CLOSE
                        if fill.side == "sell"
                        else TradeAction.BUY_TO_OPEN,
                        position_side=PositionSide.LONG,
                        is_margin_trade=False,
                        trading_mode=TradingMode.REAL,
                        quantity=float(fill.volume),
                        price=float(fill.price),
                        trade_value=round(fill.volume * fill.price, 2),
                        commission=0.0,
                        stamp_duty=0.0,
                        transfer_fee=0.0,
                        total_fee=0.0,
                        executed_at=fill.filled_at.astimezone(CST).replace(tzinfo=None),
                        exchange_trade_id=fill.fill_key,
                        exchange_name="tdx_bridge",
                        remarks=(
                            "fill_recovered: snapshot cash delta; price=NET "
                            f"({fill.price:.6f}), fees embedded"
                        )[:500],
                    )
                )
                trades_inserted.append(fill.fill_key)

            order = (
                await session.execute(
                    select(Order).where(Order.order_id == UUID(fill.internal_order_id))
                )
            ).scalar_one_or_none()
            if order is None:
                raise RuntimeError(f"orders 缺行：{fill.internal_order_id}")
            order.status = OrderStatus.FILLED
            order.filled_quantity = float(fill.volume)
            order.filled_value = round(fill.volume * fill.price, 2)
            order.average_price = float(fill.price)
            order.filled_at = fill.filled_at.astimezone(CST).replace(tzinfo=None)
            order.remarks = (
                f"{(order.remarks or '').strip()} "
                f"[FILL_RECOVERED {fill.fill_key} net={fill.price:.6f}]"
            ).strip()[:500]
            orders_updated.append(fill.internal_order_id)

        # 写后复验（同一事务内可见已写行）
        pos = await session.execute(
            text(
                "SELECT count(*) FROM qm_agent_ledger_position "
                "WHERE tenant_id=:t AND user_id=:u AND agent=:a AND code=:c"
            ),
            {"t": plan.tenant_id, "u": plan.user_id, "a": plan.agent, "c": plan.code},
        )
        cash = await session.execute(
            text(
                "SELECT virtual_cash FROM qm_agent_ledger_account "
                "WHERE tenant_id=:t AND user_id=:u AND agent=:a"
            ),
            {"t": plan.tenant_id, "u": plan.user_id, "a": plan.agent},
        )
        rts = await session.execute(
            text(
                "SELECT count(*), COALESCE(sum(realized_pnl),0) "
                "FROM qm_agent_ledger_roundtrip "
                "WHERE tenant_id=:t AND user_id=:u AND agent=:a AND code=:c"
            ),
            {"t": plan.tenant_id, "u": plan.user_id, "a": plan.agent, "c": plan.code},
        )
        orders = await session.execute(
            text(
                "SELECT order_id::text, status::text, filled_quantity, average_price "
                "FROM orders WHERE order_id::text = ANY(:ids) ORDER BY order_id"
            ),
            {"ids": [f.internal_order_id for f in plan.fills]},
        )
        trades = await session.execute(
            text(
                "SELECT exchange_trade_id, quantity, price FROM trades "
                "WHERE exchange_trade_id = ANY(:keys) ORDER BY exchange_trade_id"
            ),
            {"keys": [f.fill_key for f in plan.fills]},
        )
        rt_row = rts.first()
        after = {
            "position_rows": int(pos.scalar() or 0),
            "virtual_cash": float(cash.scalar() or 0),
            "roundtrip_rows": int((rt_row[0] if rt_row else 0) or 0),
            "roundtrip_realized_pnl": float((rt_row[1] if rt_row else 0) or 0),
            "orders": [_jsonable(r) for r in orders.mappings().all()],
            "trades": [_jsonable(r) for r in trades.mappings().all()],
        }
        return {
            "before": before,
            "outcomes": outcomes,
            "trades_inserted": trades_inserted,
            "orders_updated": orders_updated,
            "after": after,
        }


def main() -> int:
    parser = argparse.ArgumentParser(description="按账户快照补记缺失的真单成交")
    parser.add_argument("--path", required=True, help="恢复计划 JSON")
    parser.add_argument("--apply", action="store_true", help="实际写库（默认 dry-run）")
    parser.add_argument("--record", default="", help="写库留痕 JSON（--apply 必填）")
    args = parser.parse_args()

    path = Path(args.path)
    if not path.is_file():
        print(f"❌ 文件不存在：{path}", file=sys.stderr)
        return 2
    if args.apply and not args.record:
        print("❌ --apply 必须带 --record", file=sys.stderr)
        return 2
    try:
        plan = load_plan(path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"❌ 计划无效：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    print(
        f"[补账] {plan.agent} {plan.code}：{len(plan.fills)} 笔；"
        f"合计 {sum(f.volume for f in plan.fills)} 股"
    )
    for fill in plan.fills:
        print(
            f"  {fill.trade_date} {fill.side} {fill.volume}股 @ {fill.price:.6f} "
            f"order={fill.order_id} key={fill.fill_key}"
        )
    print(f"  证据：{plan.evidence[:200]}")
    if not args.apply:
        print("[补账] DRY-RUN（未写库）；确认后加 --apply --record <path>")
        return 0

    try:
        result = asyncio.run(apply_plan(plan))
    except Exception as exc:  # noqa: BLE001 写库失败整批回滚
        print(f"❌ 补账失败（事务已回滚）：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    record = {
        "ts": datetime.now(CST).isoformat(),
        "source": str(path),
        "plan": {
            "tenant_id": plan.tenant_id,
            "user_id": plan.user_id,
            "agent": plan.agent,
            "quota": plan.quota,
            "code": plan.code,
            "evidence": plan.evidence,
            "fills": [
                {
                    "fill_key": f.fill_key,
                    "code": f.code,
                    "side": f.side,
                    "volume": f.volume,
                    "price": f.price,
                    "trade_date": f.trade_date.isoformat(),
                    "filled_at": f.filled_at.isoformat(),
                    "order_id": f.order_id,
                    "internal_order_id": f.internal_order_id,
                    "note": f.note,
                }
                for f in plan.fills
            ],
        },
        **result,
    }
    record_path = Path(args.record)
    record_path.parent.mkdir(parents=True, exist_ok=True)
    record_path.write_text(
        json.dumps(record, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    after = result["after"]
    print(
        f"[补账] 完成：持仓行 {after['position_rows']}（应 0）｜"
        f"现金 ¥{after['virtual_cash']:,.2f}｜"
        f"回合 {after['roundtrip_rows']} 条 / 已实现 ¥{after['roundtrip_realized_pnl']:+,.2f}"
    )
    print(f"  留痕：{record_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
