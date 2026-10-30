"""TFT 前10调仓执行器（2026-10-30）。

规则（用户拍板）：
  - 卖出：清仓除买入名单外的全部持仓
  - 买入：T+20 默认模型（mdl_cust_train_20261025051806_8b367850_50970198）前 10，
          每只目标市值 10 万元，整百股
用法：
  python qm_tft_rebalance_20261030.py --dry-run
  python qm_tft_rebalance_20261030.py --live
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import time
from dataclasses import dataclass, field
from typing import Any

from backend.services.live_trading.services.qmt_exec_client import (
    QmtExecClient,
    QmtExecError,
)

RUN_TAG = "qm-" + time.strftime("%Y%m%d")
TARGET_VALUE = 100_000.0
BUY_LIST = [
    "SH601869",
    "SZ002384",
    "SZ002281",
    "SZ300475",
    "SZ300750",
    "SZ300308",
    "SZ002851",
    "SH600183",
    "SZ300757",
    "SH603256",
]
ORDER_INTERVAL_S = 0.15
POLL_ROUNDS = 12


@dataclass
class Plan:
    sells: list[dict[str, Any]] = field(default_factory=list)
    buys: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)


def _prefix_to_suffix(code: str) -> str:
    code = str(code or "").strip().upper()
    if len(code) > 2 and code[:2] in ("SH", "SZ", "BJ"):
        return f"{code[2:]}.{code[:2]}"
    return code


def _round2(value: float) -> float:
    return round(float(value) + 1e-9, 2)


def _tick(item: dict[str, Any]) -> tuple[float, float, float]:
    """返回 (last, bid1, ask1)。QMT get_full_tick 字段：lastPrice/bidPrice/askPrice。"""
    last = float(item.get("lastPrice") or 0)
    bids = item.get("bidPrice") or []
    asks = item.get("askPrice") or []
    bid1 = float(bids[0]) if bids and bids[0] else 0.0
    ask1 = float(asks[0]) if asks and asks[0] else 0.0
    return last, bid1, ask1


async def fetch_ticks(client: QmtExecClient, symbols: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for i in range(0, len(symbols), 50):
        chunk = symbols[i : i + 50]
        data = await client.get_full_tick(chunk)
        if isinstance(data, dict):
            for code, item in data.items():
                if isinstance(item, dict):
                    out[str(code).upper()] = item
    return out


def build_plan(positions: list[dict], ticks: dict[str, dict]) -> Plan:
    plan = Plan()
    held = {str(p.get("symbol") or "").upper(): p for p in positions}
    # 卖出：全部非买入名单
    for sym, pos in sorted(
        held.items(), key=lambda kv: -float(kv[1].get("market_value") or 0)
    ):
        if sym in BUY_LIST:
            continue
        qty = int(float(pos.get("can_use_volume") or 0))
        suffix = str(pos.get("stock_code") or _prefix_to_suffix(sym))
        tick = ticks.get(suffix.upper()) or ticks.get(sym) or {}
        last, bid1, _ = _tick(tick)
        if qty <= 0:
            plan.skipped.append(
                {"symbol": sym, "reason": "可用数量为0（T+1锁定/停牌）"}
            )
            continue
        price = bid1 if bid1 > 0 else _round2(last * 0.995)
        if price <= 0:
            plan.skipped.append({"symbol": sym, "reason": "无盘口无最新价"})
            continue
        plan.sells.append(
            {
                "symbol": suffix,
                "prefix": sym,
                "name": pos.get("instrument_name") or "",
                "quantity": qty,
                "price": _round2(price),
                "reference": last,
                "market_value": float(pos.get("market_value") or 0),
            }
        )
    # 买入：目标市值 10 万，扣除已有持仓
    for sym in BUY_LIST:
        suffix = _prefix_to_suffix(sym)
        tick = ticks.get(suffix.upper()) or ticks.get(sym) or {}
        last, _, ask1 = _tick(tick)
        price = ask1 if ask1 > 0 else _round2(last * 1.005)
        if price <= 0:
            plan.skipped.append({"symbol": sym, "reason": "无盘口无最新价"})
            continue
        existing_mv = float((held.get(sym) or {}).get("market_value") or 0)
        budget = max(0.0, TARGET_VALUE - existing_mv)
        shares = int(math.floor(budget / price / 100.0) * 100)
        if shares < 100:
            plan.skipped.append(
                {
                    "symbol": sym,
                    "reason": f"预算不足一手（剩余{_round2(budget)} 价{price}）",
                }
            )
            continue
        plan.buys.append(
            {
                "symbol": suffix,
                "prefix": sym,
                "name": (held.get(sym) or {}).get("instrument_name") or "",
                "quantity": shares,
                "price": _round2(price),
                "reference": last,
                "target_value": _round2(shares * price),
            }
        )
    return plan


def print_plan(plan: Plan) -> None:
    print(f"\n=== 卖出 {len(plan.sells)} 笔（清仓非买入名单）===")
    total_sell = 0.0
    for item in plan.sells:
        total_sell += item["market_value"]
        print(
            f"  SELL {item['symbol']:<10} {item['name']:<8} {item['quantity']:>6} @ {item['price']:>7.2f}"
            f"  (现价 {item['reference']:.2f}, 市值 {item['market_value']:,.0f})"
        )
    print(f"  合计市值约 {total_sell:,.0f}")
    print(f"\n=== 买入 {len(plan.buys)} 笔（T+20 前 10，目标 10 万/只）===")
    total_buy = 0.0
    for item in plan.buys:
        total_buy += item["target_value"]
        print(
            f"  BUY  {item['symbol']:<10} {item['name']:<8} {item['quantity']:>6} @ {item['price']:>7.2f}"
            f"  (现价 {item['reference']:.2f}, 金额 {item['target_value']:,.0f})"
        )
    print(f"  合计约 {total_buy:,.0f}")
    if plan.skipped:
        print(f"\n=== 跳过 {len(plan.skipped)} 笔 ===")
        for item in plan.skipped:
            print(f"  SKIP {item['symbol']}: {item['reason']}")


async def place_sells(client: QmtExecClient, plan: Plan) -> list[dict]:
    results = []
    for item in plan.sells:
        cid = f"{RUN_TAG}-sell-{item['prefix']}"
        rec = {"kind": "sell", **item, "client_order_id": cid}
        try:
            res = await client.submit_order(
                symbol=item["symbol"],
                side="SELL",
                quantity=item["quantity"],
                order_type="LIMIT",
                price=item["price"],
                client_order_id=cid,
            )
            rec["result"] = res
            rec["ok"] = True
            print(
                f"  SELL {item['symbol']} x{item['quantity']} @{item['price']} -> order {res.get('order_id')}"
            )
        except QmtExecError as exc:
            rec["ok"] = False
            rec["error"] = f"{exc.code}: {exc}"
            print(f"  SELL {item['symbol']} 失败: {rec['error']}")
        results.append(rec)
        await asyncio.sleep(ORDER_INTERVAL_S)
    return results


async def place_buys(client: QmtExecClient, plan: Plan) -> list[dict]:
    results = []
    for item in plan.buys:
        cid = f"{RUN_TAG}-buy-{item['prefix']}"
        rec = {"kind": "buy", **item, "client_order_id": cid}
        try:
            res = await client.submit_order(
                symbol=item["symbol"],
                side="BUY",
                quantity=item["quantity"],
                order_type="LIMIT",
                price=item["price"],
                client_order_id=cid,
            )
            rec["result"] = res
            rec["ok"] = True
            print(
                f"  BUY  {item['symbol']} x{item['quantity']} @{item['price']} -> order {res.get('order_id')}"
            )
        except QmtExecError as exc:
            rec["ok"] = False
            rec["error"] = f"{exc.code}: {exc}"
            print(f"  BUY  {item['symbol']} 失败: {rec['error']}")
        results.append(rec)
        await asyncio.sleep(ORDER_INTERVAL_S)
    return results


async def poll_orders(client: QmtExecClient, submitted: list[dict]) -> dict[str, dict]:
    ids = {
        str((r.get("result") or {}).get("order_id") or "")
        for r in submitted
        if r.get("ok")
    }
    latest: dict[str, dict] = {}
    for _ in range(POLL_ROUNDS):
        try:
            orders = await client.query_orders()
        except QmtExecError as exc:
            print(f"  委托查询失败: {exc}")
            await asyncio.sleep(1)
            continue
        for order in orders:
            oid = str(order.get("order_id") or "")
            if oid in ids:
                latest[oid] = order
        pending = {
            i
            for i in ids
            if i
            and str((latest.get(i) or {}).get("status", "")).upper()
            not in ("FILLED", "CANCELED", "CANCELLED", "REJECTED")
        }
        if not pending:
            break
        await asyncio.sleep(1)
    return latest


async def main(live: bool) -> int:
    client = QmtExecClient.from_env()
    await client.refresh_settings()
    cfg = client.effective_config()
    print(
        f"client enabled={cfg['enabled']} account={cfg['account_id']} mode={'LIVE' if live else 'DRY-RUN'}"
    )
    status = await client.bridge_status()
    print("bridge:", json.dumps(status, ensure_ascii=False, default=str))
    if not cfg["enabled"]:
        print("ERROR: QMT 执行端未启用（broker:config:qmt_exec.enabled=false）")
        return 2

    asset = await client.get_asset()
    positions = await client.get_positions()
    print(
        f"账户: 总资产 {asset.get('total_asset', 0):,.2f} 现金 {asset.get('cash', 0):,.2f} "
        f"市值 {asset.get('market_value', 0):,.2f} 持仓 {len(positions)} 只"
    )
    symbols = [str(p.get("symbol") or "") for p in positions if p.get("symbol")]
    # 买入名单也要盘口（不在持仓里的标的不在上面的列表里）
    for sym in BUY_LIST:
        suffix = _prefix_to_suffix(sym)
        if suffix not in symbols:
            symbols.append(suffix)
    ticks = await fetch_ticks(client, symbols)
    print(f"盘口: 取到 {len(ticks)}/{len(symbols)}")

    plan = build_plan(positions, ticks)
    print_plan(plan)
    if not live:
        print("\n[DRY-RUN] 未提交任何委托。")
        return 0

    print("\n=== 开始卖出 ===")
    sell_results = await place_sells(client, plan)
    print("\n=== 开始买入 ===")
    buy_results = await place_buys(client, plan)

    all_results = sell_results + buy_results
    print("\n=== 终态核对（最多 12s）===")
    latest = await poll_orders(client, all_results)
    unresolved = []
    for rec in all_results:
        oid = str((rec.get("result") or {}).get("order_id") or "")
        if not rec.get("ok"):
            print(f"  失败 {rec['symbol']}: {rec.get('error')}")
            continue
        o = latest.get(oid)
        if not o:
            unresolved.append(rec["symbol"])
            print(
                f"  未查到委托 {rec['symbol']} order={oid}（可能已成交，禁止盲目重试）"
            )
            continue
        print(
            f"  {rec['kind'].upper():4} {rec['symbol']:<10} {o.get('status'):<10} "
            f"委托{o.get('order_volume'):.0f} 成交{o.get('traded_volume'):.0f} @{o.get('traded_price')}"
        )

    await asyncio.sleep(2)
    asset_after = await client.get_asset()
    pos_after = await client.get_positions()
    print(
        f"\n账户(后): 总资产 {asset_after.get('total_asset', 0):,.2f} 现金 {asset_after.get('cash', 0):,.2f} "
        f"市值 {asset_after.get('market_value', 0):,.2f} 持仓 {len(pos_after)} 只"
    )
    with open(
        "/tmp/qm_tft_rebalance_20261030_result.json", "w", encoding="utf-8"
    ) as fh:
        json.dump(
            {
                "asset_before": asset,
                "asset_after": asset_after,
                "plan": plan.__dict__,
                "results": all_results,
                "latest_orders": latest,
                "unresolved": unresolved,
                "positions_after": pos_after,
            },
            fh,
            ensure_ascii=False,
            default=str,
            indent=1,
        )
    print("结果已写入 /tmp/qm_tft_rebalance_20261030_result.json")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="真实下单")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.live)))
