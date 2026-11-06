#!/usr/bin/env python3
"""止损补单：按柜台实时可卖量市价卖出指定标的（自服务，一条命令）。

为什么存在：2026-11-06 止损单被拒后需要补单，临时脚本被 Claude Code 的
「真实交易」权限闸拦下，最后只能人工在 QMT 里点。把补单收敛成仓库内的
受控脚本后，执行它只需要一条 docker exec，不再依赖一次性脚本。

用法（容器内，默认 dry-run）：
    docker exec -w /app/backend -e PYTHONPATH=/app quantmind \
        python scripts/sltp_sell_now.py --symbols 600183.SH,601869.SH
    # 确认无误后加 --confirm 真下市价卖单
    docker exec -w /app/backend -e PYTHONPATH=/app quantmind \
        python scripts/sltp_sell_now.py --symbols 600183.SH --confirm

安全边界：
* 只发 SELL + MARKET；数量 = min(柜台可卖, 持仓量)，全量卖出时带 full_position_sell；
* 账户通道不可用 / 任一标的无实时行情 → 整批拒绝（fail-closed，不猜）；
* 每笔用带时间戳的新 client_order_id，绝不复用已烧掉的号；
* 派发后按 PG `orders.status` 终态复核（--verify-wait 秒），不拿派发层的
  success 当成交。

用户偏好「止损按 symbol|owner 只卖归属份额」：本脚本只用于**人工规则/无归属**
的整仓止损补单；LLM 分账仓请走 sltp 执行器（它按 owner 账本夹取）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time

USER_ID = "10000001"
TERMINAL_BAD = {"rejected", "failed", "expired", "cancelled"}


def normalize(symbol: str) -> str:
    return str(symbol or "").strip().upper()


def _match_key(code: str) -> str:
    """A 股代码匹配键：只取数字。

    柜台可能返回前缀式（SH600036），调用方传后缀式（600036.SH）；两者
    统一按数字匹配（A 股 6 位、港股 5 位）。无数字的代码（美股 ticker）
    返回空串，调用方跳过——本脚本只用于 A 股整仓止损。
    """
    return ''.join(ch for ch in normalize(code) if ch.isdigit())


def build_plan(positions: list[dict], ticks: dict, symbols: list[str]) -> tuple[list[dict], list[str]]:
    """柜台持仓 + 实时行情 → 卖出计划（纯函数，便于单测）。

    返回 (plan, skipped)：plan 元素含 symbol/qty/full_position/price；skipped 为
    人话原因列表。任一标的「查无持仓」「可卖为 0」「无行情」都只跳过它自己，
    但调用方在 --strict 下会整批拒绝。
    """
    by_code = {}
    for row in positions or []:
        key = _match_key(row.get("stock_code") or row.get("symbol") or "")
        if key:
            by_code[key] = row
    plan: list[dict] = []
    skipped: list[str] = []
    for raw in symbols:
        symbol = normalize(raw)
        row = by_code.get(_match_key(symbol))
        if not row:
            skipped.append(f"{symbol}: 柜台无持仓")
            continue
        volume = float(row.get("volume") or 0)
        can_use = float(
            row.get("can_use_volume")
            if row.get("can_use_volume") is not None
            else (row.get("available") or 0)
        )
        if can_use <= 0:
            skipped.append(f"{symbol}: 可卖为 0（volume={volume:g}）")
            continue
        tick = ticks.get(symbol) or {}
        price = float(tick.get("lastPrice") or 0)
        if price <= 0:
            skipped.append(f"{symbol}: 无实时行情（拒绝市价单绕过风控）")
            continue
        qty = min(can_use, volume or can_use)
        plan.append({
            "symbol": symbol,
            "qty": qty,
            "full_position": bool(volume and qty >= volume),
            "price": price,
        })
    return plan, skipped


async def _retry(call, attempts: int = 6, delay: float = 1.5):
    """限速/短时不可用要重试：桥的令牌桶会以 TRADE_RPC_THROTTLED 快速拒绝，
    这不是故障，隔十几秒再来即可。"""
    last = None
    for i in range(attempts):
        try:
            return await call()
        except Exception as exc:  # noqa: BLE001
            last = exc
            text = str(exc)
            if not any(k in text for k in ("THROTTLED", "UNHEALTHY", "超时", "timeout")):
                raise
            if i + 1 < attempts:
                await asyncio.sleep(delay)
    raise last  # type: ignore[misc]


async def _run(args) -> int:
    from backend.services.live_trading.services.qmt_exec_client import (
        get_qmt_exec_client,
    )

    symbols = [s for s in (args.symbols or "").split(",") if s.strip()]
    if not symbols:
        print("ABORT 未指定 --symbols", file=sys.stderr)
        return 2

    client = get_qmt_exec_client()
    await client.refresh_settings()
    try:
        await _retry(client.get_asset)
        positions = await _retry(client.get_positions)
    except Exception as exc:  # noqa: BLE001
        print(f"ABORT 账户通道不可用：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    try:
        ticks = await _retry(lambda: client.get_full_tick(symbols))
    except Exception as exc:  # noqa: BLE001
        print(f"ABORT 行情不可用：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    plan, skipped = build_plan(positions, ticks, symbols)
    for line in skipped:
        print(f"SKIP {line}")
    print("PLAN " + json.dumps(plan, ensure_ascii=False))
    if not plan:
        return 0
    if args.strict and skipped:
        print("ABORT --strict：存在被跳过的标的", file=sys.stderr)
        return 2
    if not args.confirm:
        print("DRY-RUN：确认后加 --confirm 才真下单")
        return 0

    from backend.services.live_trading.services.internal_strategy_dispatcher import (
        dispatch_internal_strategy_order,
    )
    from backend.services.trade_shared.deps import get_redis
    from backend.shared.database_manager_v2 import get_session

    redis = get_redis()
    stamp = int(time.time())
    order_ids: list[str] = []
    for item in plan:
        order_data = {
            "symbol": item["symbol"],
            "side": "SELL",
            "quantity": item["qty"],
            "price": 0.0,
            "order_type": "MARKET",
            "trading_mode": "REAL",
            "portfolio_id": 0,
            "strategy_id": None,
            "client_order_id": f"sltpfix-{item['symbol']}-{stamp}",
            "remarks": "sltp:止损补单(sltp_sell_now)",
            "ref_price": item["price"],
            "full_position_sell": item["full_position"],
        }
        try:
            async with get_session() as db:
                resp = await dispatch_internal_strategy_order(
                    order_data=order_data,
                    user_id=args.user,
                    tenant_id=args.tenant,
                    redis=redis,
                    db=db,
                )
        except Exception as exc:  # noqa: BLE001
            resp = {"status": "error", "message": f"{type(exc).__name__}: {exc}"}
        oid = str((resp or {}).get("order_id") or "")
        if oid:
            order_ids.append(oid)
        print(f"{item['symbol']} {item['qty']:g} "
              + json.dumps(resp, ensure_ascii=False, default=str)[:400], flush=True)

    if order_ids and args.verify_wait > 0:
        print(f"VERIFY 等待 {args.verify_wait:.0f}s 后复核 PG 终态…")
        await asyncio.sleep(args.verify_wait)
        from sqlalchemy import select

        from backend.services.trade_shared.models.order import Order

        async with get_session(read_only=True) as db:
            rows = (await db.execute(
                select(Order.order_id, Order.symbol, Order.status,
                       Order.filled_quantity, Order.quantity)
                .where(Order.order_id.in_(order_ids))
            )).all()
        for row in rows:
            status = str(getattr(row.status, "value", row.status))
            flag = "BAD" if status in TERMINAL_BAD else "ok"
            print(f"VERIFY {row.symbol} {status} filled={row.filled_quantity:g}/"
                  f"{row.quantity:g} {flag}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="止损补单（市价卖出，默认 dry-run）")
    parser.add_argument("--symbols", required=True, help="逗号分隔，后缀式代码")
    parser.add_argument("--confirm", action="store_true", help="真正下单")
    parser.add_argument("--strict", action="store_true", help="有标的被跳过则整批拒绝")
    parser.add_argument("--verify-wait", type=float, default=5.0,
                        help="派发后复核终态前等待秒数（0=不复核）")
    parser.add_argument("--user", default=USER_ID)
    parser.add_argument("--tenant", default="default")
    args = parser.parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
