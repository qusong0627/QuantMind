"""ST/*ST 持仓分批清仓（2026-10-30）。

前置：伴生桥已重启并加载 `risk.apply_to_sells=false` 的版本（卖出不再被
ST 名称黑名单/单笔 20 万/±5% 偏离/30 笔每分拦截）。未重启时只会得到
`risk: blacklisted name`，脚本遇此立刻停止，不会空转。

策略：
  - 每只按「单笔 ≤13 万且 ≤2 万股」切批（002883 盘口薄，必须拆）；
  - 轮次执行：每轮每只卖一批，轮间隔 20s（分批，不砸盘）；
  - 价格：买一价（无买一则最新价）；下单被风控以偏离为由拒绝时才退市价；
  - 任何超时不重试，只查询核对。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import time
import urllib.request
from typing import Any

from backend.services.live_trading.services.qmt_exec_client import (
    QmtExecClient,
    QmtExecError,
)

BRIDGE_URL = os.getenv("TDX_BRIDGE_URL", "http://192.168.31.13:8550").rstrip("/")
BRIDGE_TOKEN = os.getenv("TDX_BRIDGE_TOKEN", "")
TAG = "stbatch-" + time.strftime("%Y%m%d-%H%M%S")
MAX_ORDER_VALUE = 130_000.0
MAX_ORDER_SHARES = 20_000
ROUND_GAP_S = 20.0
ORDER_GAP_S = 2.4
TERMINAL = {"FILLED", "CANCELLED", "REJECTED", "PARTIALLY_FILLED"}


def _http(path: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        f"{BRIDGE_URL}{path}",
        data=data,
        headers={
            "Authorization": f"Bearer {BRIDGE_TOKEN}",
            "Content-Type": "application/json",
        },
        method="POST" if data is not None else "GET",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
        return json.loads(resp.read().decode())


def _quote(symbol: str) -> dict[str, Any]:
    payload = _http(f"/api/v1/market/quote?code={symbol}&source=tdx")
    return (payload.get("data") or {}).get("data") or {}


def _st_positions() -> list[dict]:
    acct = _http("/api/v1/account/query", {"source": "qmt"})
    out = []
    for p in acct.get("positions") or []:
        name = str(p.get("stock_name") or "")
        vol = int(p.get("total_volume") or 0)
        avail = int(p.get("available_volume") or 0)
        if vol > 0 and ("ST" in name or "退" in name):
            out.append(
                {
                    "symbol": str(p.get("stock_code") or "").upper(),
                    "name": name,
                    "volume": vol,
                    "available": avail,
                }
            )
    return sorted(out, key=lambda x: -x["volume"])


def _tranches(volume: int, price: float) -> list[int]:
    if volume <= 0:
        return []
    cap_by_value = MAX_ORDER_VALUE / price if price > 0 else MAX_ORDER_SHARES
    cap = max(100, min(MAX_ORDER_SHARES, int(cap_by_value // 100) * 100))
    if volume <= cap:
        return [volume]
    chunks = []
    left = volume
    while left > 0:
        take = min(left, cap)
        if left - take < 100:
            take = left
        chunks.append(take)
        left -= take
    return chunks


def build_plan() -> list[dict]:
    plan = []
    for pos in _st_positions():
        q = _quote(pos["symbol"])
        last = float(q.get("last") or 0)
        bid1 = float((q.get("bids") or [{}])[0].get("price") or 0)
        price = bid1 if bid1 > 0 else last
        if price <= 0:
            print(f"  跳过 {pos['symbol']}：无盘口")
            continue
        vol = min(pos["volume"], pos["available"] or pos["volume"])
        for qty in _tranches(vol, price):
            plan.append({**pos, "quantity": qty, "price": round(price, 2)})
    return plan


def rounds_of(plan: list[dict]) -> list[list[dict]]:
    """按标的分组后轮转：每轮每只一笔，实现分批。"""
    by_symbol: dict[str, list[dict]] = {}
    for item in plan:
        by_symbol.setdefault(item["symbol"], []).append(item)
    rounds: list[list[dict]] = []
    while any(by_symbol.values()):
        layer = []
        for items in by_symbol.values():
            if items:
                layer.append(items.pop(0))
        if layer:
            rounds.append(layer)
    return rounds


async def _wait_final(
    client: QmtExecClient, order_id: str, timeout_s: float = 15.0
) -> str:
    start = time.monotonic()
    last = ""
    while time.monotonic() - start < timeout_s:
        try:
            for o in await client.query_orders():
                if str(o.get("order_id")) == str(order_id):
                    last = str(o.get("status") or "")
                    if last in TERMINAL:
                        return last
        except QmtExecError:
            pass
        await asyncio.sleep(0.7)
    return last or "UNKNOWN"


async def submit(client: QmtExecClient, item: dict) -> dict:
    cid = f"{TAG}-{item['symbol']}-{item['quantity']}"
    rec = {**item, "client_order_id": cid}
    for attempt_type in ("LIMIT", "MARKET"):
        price = item["price"] if attempt_type == "LIMIT" else 0.0
        t0 = time.monotonic()
        try:
            res = await client.submit_order(
                symbol=item["symbol"],
                side="SELL",
                quantity=item["quantity"],
                order_type=attempt_type,
                price=price,
                client_order_id=cid + ("" if attempt_type == "LIMIT" else "-mkt"),
            )
            rec.update(
                order_id=res.get("order_id"),
                order_type=attempt_type,
                accept_ms=round((time.monotonic() - t0) * 1000, 1),
            )
            print(
                f"  {item['symbol']} {item['name']} {item['quantity']} {attempt_type}"
                f"@{price:.2f} -> {rec['order_id']} ({rec['accept_ms']}ms)"
            )
            return rec
        except QmtExecError as exc:
            msg = f"{exc.code}: {exc}"
            print(
                f"  {item['symbol']} {item['quantity']} {attempt_type} 失败: {msg[:120]}"
            )
            rec.update(error=msg[:300], order_type=attempt_type)
            if "blacklisted" in msg:
                rec["abort"] = True
                return rec
            if "deviation" not in msg or attempt_type == "MARKET":
                return rec
    return rec


async def main(live: bool) -> int:
    client = QmtExecClient.from_env()
    await client.refresh_settings()
    print("mode:", "LIVE" if live else "DRY-RUN")
    plan = build_plan()
    rounds = rounds_of(plan)
    total = sum(i["quantity"] * i["price"] for i in plan)
    print(f"\nST 持仓计划：{len(plan)} 笔 / {len(rounds)} 轮，合计约 ¥{total:,.0f}")
    for r, layer in enumerate(rounds, 1):
        print(
            f"  第 {r} 轮: "
            + ", ".join(f"{i['symbol']} {i['quantity']}@{i['price']}" for i in layer)
        )
    if not live:
        return 0

    results = []
    for r, layer in enumerate(rounds, 1):
        print(f"\n=== 第 {r}/{len(rounds)} 轮 ===")
        for item in layer:
            rec = await submit(client, item)
            results.append(rec)
            if rec.get("abort"):
                print("\n桥侧仍拦 ST 卖出（未重启加载新代码），停止。")
                _dump(results)
                return 3
            if rec.get("order_id"):
                status = await _wait_final(client, str(rec["order_id"]))
                rec["final_status"] = status
                print(f"    终态 {status}")
            await asyncio.sleep(ORDER_GAP_S)
        if r < len(rounds):
            print(f"    等待 {ROUND_GAP_S:.0f}s 进入下一轮…")
            await asyncio.sleep(ROUND_GAP_S)

    _dump(results)
    acct = _http("/api/v1/account/query", {"source": "qmt"})
    a = acct.get("asset") or {}
    print(f"\n账户: 现金 {a.get('cash', 0):,.2f} 市值 {a.get('market_value', 0):,.2f}")
    left = [
        p for p in acct.get("positions") or [] if "ST" in str(p.get("stock_name") or "")
    ]
    print(
        "剩余 ST:",
        [(p["stock_code"], p["stock_name"], p["total_volume"]) for p in left],
    )
    return 0


def _dump(results: list[dict]) -> None:
    with open("/tmp/qm_st_sell_batched_20261030.json", "w", encoding="utf-8") as fh:
        json.dump(results, fh, ensure_ascii=False, indent=1, default=str)
    print("结果 -> /tmp/qm_st_sell_batched_20261030.json")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true")
    args = ap.parse_args()
    raise SystemExit(asyncio.run(main(args.live)))
