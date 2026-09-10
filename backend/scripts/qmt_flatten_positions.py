#!/usr/bin/env python3
"""按清单平仓大 QMT 真账户持仓（默认干跑；加 ``--live`` 才真下单）。

用途：把压测/试单买进的持仓按限价了结，或按清单减仓。

规则（每条都对应实盘踩过的坑）：
* 只卖「清单里 + 柜台可用量 > 0」的部分；**当日买入的 T+1 锁定部分自动跳过**
  （柜台口径 can_use_volume=0，硬报会吃「可用不足」拒单）。
* 限价 = QuantDB 最近收盘价 × (1 - ``--limit-pct``)（卖出取低价更易成交），
  参考价取不到则跳过该标的——fail-closed，宁可不卖也不拍脑袋报价。
* 数量默认取柜台可用量的整数股；ST/北交所同样按 0.01 元报价口径。
* 默认走内部策略真单链路（落本地 orders 表，可被 qmt_exec_poller 回收对账）；
  ``--via direct`` 直连 QMT 客户端，仅作应急（不经本地落库）。

用法（容器内执行）::

    # 干跑：只打印将要下的单，不碰柜台
    docker exec -w /app -e PYTHONPATH=/app quantmind \
        python backend/scripts/qmt_flatten_positions.py

    # 实际执行（先 --limit 1 试一笔，确认链路再全量）
    docker exec -w /app -e PYTHONPATH=/app quantmind \
        python backend/scripts/qmt_flatten_positions.py --live --limit 1

清单来源：``--from-file``（默认 /tmp/qmt_flatten_list.json，形如
``[{"symbol": "600289.SH", "volume": 100000}]``）或 ``--symbols 600289.SH,002883.SZ``
（只给代码时数量取柜台可用量）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime

DEFAULT_LIST_FILE = "/tmp/qmt_flatten_list.json"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="按清单平仓 QMT 真账户持仓")
    parser.add_argument("--symbols", default="", help="逗号分隔的标的（后缀式 600289.SH）")
    parser.add_argument(
        "--from-file",
        default=DEFAULT_LIST_FILE,
        help=f"清单 JSON 路径（默认 {DEFAULT_LIST_FILE}）",
    )
    parser.add_argument(
        "--live", action="store_true", help="真下单；缺省为干跑（只打印）"
    )
    parser.add_argument(
        "--limit-pct",
        type=float,
        default=0.02,
        help="限价折让（限价 = 参考价 × (1 - 折让)，默认 0.02）",
    )
    parser.add_argument(
        "--via",
        choices=["dispatcher", "direct"],
        default="dispatcher",
        help="下单通道：dispatcher=内部真单链路（默认，落库可对账）/ direct=直连客户端",
    )
    parser.add_argument("--user-id", default="1", help="下单用户（DB 补零口径前的原值）")
    parser.add_argument("--limit", type=int, default=0, help="最多处理 N 只（0=全部）")
    parser.add_argument("--sleep", type=float, default=0.3, help="每笔间隔秒数")
    return parser


def _load_symbols(args: argparse.Namespace) -> dict[str, float]:
    """返回 {symbol: 指定数量(0=用柜台可用量)}。"""
    from backend.shared.stock_utils import StockCodeUtil

    wanted: dict[str, float] = {}
    if args.symbols.strip():
        for raw in args.symbols.split(","):
            if raw.strip():
                wanted[StockCodeUtil.to_suffix(raw.strip())] = 0.0
        return wanted
    try:
        with open(args.from_file, encoding="utf-8") as fh:
            items = json.load(fh)
    except FileNotFoundError:
        print(f"!! 清单文件不存在：{args.from_file}（用 --symbols 或先落清单）")
        return {}
    for item in items or []:
        symbol = StockCodeUtil.to_suffix(str(item.get("symbol") or ""))
        if symbol:
            wanted[symbol] = float(item.get("volume") or 0)
    return wanted


async def _positions(client) -> dict[str, dict]:
    from backend.shared.stock_utils import StockCodeUtil

    rows = await client.get_positions()
    out: dict[str, dict] = {}
    for item in rows:
        symbol = StockCodeUtil.to_suffix(
            str(item.get("symbol") or item.get("stock_code") or "")
        )
        if symbol:
            out[symbol] = {
                "volume": float(item.get("volume") or 0),
                "can_use": float(item.get("can_use_volume") or 0),
                "name": str(item.get("instrument_name") or ""),
            }
    return out


async def _reference_prices(symbols: list[str]) -> dict[str, float]:
    """批量取参考价（QuantDB 最近收盘）；失败返回空 dict 由调用方跳过。"""
    from backend.services.live_trading.services.qmt_account_sync_task import (
        batch_quantdb_last_close,
    )

    if not symbols:
        return {}
    try:
        closes = await asyncio.to_thread(batch_quantdb_last_close, symbols)
        return {k: float(v or 0) for k, v in (closes or {}).items()}
    except Exception as exc:  # noqa: BLE001
        print(f"   !! 参考价批量取数失败：{exc}")
        return {}


async def _submit_via_dispatcher(
    *, symbol: str, quantity: float, price: float, cid: str, user_id: str
) -> dict:
    from backend.services.live_trading.services.internal_strategy_dispatcher import (
        dispatch_internal_strategy_order,
    )
    from backend.services.trade_shared.deps import get_redis
    from backend.shared.database_manager_v2 import get_session

    async with get_session() as db:
        return await dispatch_internal_strategy_order(
            order_data={
                "symbol": symbol,
                "side": "SELL",
                "quantity": quantity,
                "price": price,
                "order_type": "LIMIT",
                "trading_mode": "REAL",
                "portfolio_id": 0,
                "strategy_id": None,
                "client_order_id": cid,
                "remarks": "flatten:manual",
            },
            user_id=user_id,
            tenant_id="default",
            redis=get_redis(),
            db=db,
        )


async def _submit_via_direct(client, *, symbol, quantity, price, cid) -> dict:
    return await client.submit_order(
        symbol=symbol,
        side="SELL",
        quantity=quantity,
        price=price,
        order_type="LIMIT",
        client_order_id=cid,
    )


async def _run(args: argparse.Namespace) -> int:
    from backend.services.live_trading.services.qmt_exec_client import (
        get_qmt_exec_client,
    )

    client = get_qmt_exec_client()
    try:
        effective = await client.refresh_settings()
    except Exception as exc:  # noqa: BLE001
        print(f"!! 刷新 QMT 页面配置失败：{exc}")
        return 1
    if not effective.get("enabled"):
        print(f"!! QMT 执行端未启用：{effective}")
        return 1

    wanted = _load_symbols(args)
    if not wanted:
        print("!! 清单为空，无事可做")
        return 1
    positions = await _positions(client)

    sellable = [
        symbol
        for symbol, want in wanted.items()
        if (positions.get(symbol) or {}).get("can_use", 0) > 0
    ]
    prices = await _reference_prices(sellable)

    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    plan: list[dict] = []
    for symbol, want in wanted.items():
        pos = positions.get(symbol)
        if not pos or pos["volume"] <= 0:
            plan.append({"symbol": symbol, "skip": "无持仓"})
            continue
        can_use = pos["can_use"]
        if can_use <= 0:
            plan.append(
                {
                    "symbol": symbol,
                    "skip": f"可用 0（T+1 锁定 {pos['volume']:.0f} 股）",
                }
            )
            continue
        quantity = min(want, can_use) if want > 0 else can_use
        if quantity <= 0:
            plan.append({"symbol": symbol, "skip": "清单数量为 0"})
            continue
        reference = float(prices.get(symbol) or 0)
        if reference <= 0:
            plan.append({"symbol": symbol, "skip": "参考价缺失（fail-closed）"})
            continue
        plan.append(
            {
                "symbol": symbol,
                "name": pos["name"],
                "quantity": quantity,
                "can_use": can_use,
                "reference": reference,
                "limit": round(reference * (1 - args.limit_pct), 2),
            }
        )

    ready = [p for p in plan if "skip" not in p]
    if args.limit > 0:
        ready = ready[: args.limit]
    print(f"=== 平仓计划（{'实盘' if args.live else '干跑'}，via={args.via}）===")
    for row in plan:
        if "skip" in row:
            print(f"  {row['symbol']:<12} 跳过：{row['skip']}")
        else:
            flag = "" if row in ready else "（本次不处理：--limit 截断）"
            print(
                f"  {row['symbol']:<12} {row['name']:<8} 卖 {row['quantity']:>8.0f} 股"
                f" @ {row['limit']:.2f}（参考 {row['reference']:.2f}，可用 {row['can_use']:.0f}）{flag}"
            )

    if not args.live:
        print(f"\n干跑结束：{len(ready)} 只可卖。加 --live 执行。")
        return 0

    print(f"\n=== 执行（共 {len(ready)} 只）===")
    for index, row in enumerate(ready):
        cid = f"flat-{run_id}-{index:03d}"
        try:
            if args.via == "dispatcher":
                result = await _submit_via_dispatcher(
                    symbol=row["symbol"],
                    quantity=row["quantity"],
                    price=row["limit"],
                    cid=cid,
                    user_id=args.user_id,
                )
            else:
                result = await _submit_via_direct(
                    client,
                    symbol=row["symbol"],
                    quantity=row["quantity"],
                    price=row["limit"],
                    cid=cid,
                )
            status = str(result.get("status") or result.get("success"))
            detail = result.get("result") or result.get("detail") or ""
            print(f"  [{index:02d}] {row['symbol']} 卖 {row['quantity']:.0f} → {status} {detail}")
        except Exception as exc:  # noqa: BLE001
            print(f"  [{index:02d}] {row['symbol']} 卖 {row['quantity']:.0f} → 异常：{exc}")
        await asyncio.sleep(max(0.0, args.sleep))
    return 0


def main() -> int:
    return asyncio.run(_run(_build_parser().parse_args()))


if __name__ == "__main__":
    sys.exit(main())
