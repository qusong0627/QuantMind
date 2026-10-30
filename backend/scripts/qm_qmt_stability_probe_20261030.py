"""QMT 下单/撤单稳定性探针（2026-10-30）。

四组真机用例（价格都落在桥侧 ±5% 偏离闸内）：
  1. SELL-CANCEL：002851 卖 100 @ 昨收+4%（挂不上）→ 撤单 → CANCELLED
  2. BUY-CANCEL ：601398 买 100 @ 昨收-2%（挂不上）→ 撤单 → CANCELLED
  3. BUY-FILL   ：002851 买 100 @ 昨收-4.99%（可成交）→ FILLED
  4. SELL-FILL  ：002851 卖 100 @ MARKET（昨日可用）→ FILLED，净持仓不变

每次记录受理延迟、终态延迟、order_id、终态。超时一律**不重试**，只查询核对。
只读数据（账户/盘口）走 8550 桥的缓存，不占 QMT 单线程管道。
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

TAG = "stab-" + time.strftime("%Y%m%d-%H%M%S")
TERMINAL = {"FILLED", "CANCELLED", "REJECTED"}
BRIDGE_URL = os.getenv("TDX_BRIDGE_URL", "http://192.168.31.13:8550").rstrip("/")
BRIDGE_TOKEN = os.getenv("TDX_BRIDGE_TOKEN", "")


def _http_json(path: str, payload: dict | None = None) -> dict:
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


def _http_quote(symbol: str) -> dict[str, Any]:
    payload = _http_json(f"/api/v1/market/quote?code={symbol}&source=tdx")
    return (payload.get("data") or {}).get("data") or {}


def _http_account() -> dict[str, Any]:
    return _http_json("/api/v1/account/query", {"source": "qmt"})


def _pos_of(acct: dict, symbol: str) -> tuple[int, int]:
    for p in acct.get("positions") or []:
        if str(p.get("stock_code") or "").upper() == symbol:
            return int(p.get("total_volume") or 0), int(p.get("available_volume") or 0)
    return 0, 0


async def _wait_status(
    client: QmtExecClient, order_id: str, wanted: set[str], timeout_s: float = 20.0
) -> tuple[str, float]:
    start = time.monotonic()
    last = ""
    while time.monotonic() - start < timeout_s:
        try:
            orders = await client.query_orders()
        except QmtExecError as exc:
            print(f"    查询失败（继续等）: {str(exc)[:100]}")
            await asyncio.sleep(1.0)
            continue
        for order in orders:
            if str(order.get("order_id")) == str(order_id):
                last = str(order.get("status") or "")
                if last in wanted:
                    return last, time.monotonic() - start
        await asyncio.sleep(0.5)
    return last or "UNKNOWN", time.monotonic() - start


async def _cancel(
    client: QmtExecClient, order_id: str, symbol: str
) -> tuple[bool, str, float]:
    start = time.monotonic()
    try:
        await client.cancel_order(order_id=order_id, symbol=symbol)
        return True, "", time.monotonic() - start
    except QmtExecError as exc:
        return False, f"{exc.code}: {exc}", time.monotonic() - start


async def _case(client: QmtExecClient, spec: dict) -> dict:
    name = spec["name"]
    print(
        f"\n[{name}] {spec['side']} {spec['symbol']} {spec['qty']} @ {spec['price']} ({spec['order_type']})"
    )
    rec: dict[str, Any] = dict(spec)
    t0 = time.monotonic()
    try:
        res = await client.submit_order(
            symbol=spec["symbol"],
            side=spec["side"],
            quantity=spec["qty"],
            order_type=spec["order_type"],
            price=spec["price"],
            client_order_id=f"{TAG}-{name}",
        )
    except QmtExecError as exc:
        rec["error"] = f"{exc.code}: {exc}"
        print(f"    受理失败: {rec['error'][:140]}")
        return rec
    rec["accept_ms"] = round((time.monotonic() - t0) * 1000, 1)
    rec["order_id"] = res.get("order_id")
    print(f"    受理 {rec['accept_ms']}ms order_id={rec['order_id']}")

    if spec["want"] == "SUBMITTED":
        status, wait = await _wait_status(client, rec["order_id"], {"SUBMITTED"})
        print(f"    状态 {status}（{wait:.1f}s）")
        ok, err, cancel_ms = await _cancel(client, rec["order_id"], spec["symbol"])
        rec.update(
            cancel_ok=ok, cancel_error=err[:200], cancel_ms=round(cancel_ms * 1000, 1)
        )
        print(f"    撤单 {'成功' if ok else '失败'} {rec['cancel_ms']}ms {err[:80]}")
        status2, wait2 = await _wait_status(client, rec["order_id"], TERMINAL)
        rec.update(final_status=status2, final_wait_s=round(wait2, 1))
        print(f"    终态 {status2}（{wait2:.1f}s）")
    else:
        status, wait = await _wait_status(client, rec["order_id"], {spec["want"]})
        rec.update(final_status=status, final_wait_s=round(wait, 1))
        print(f"    终态 {status}（{wait:.1f}s）")
    return rec


def _build_plan() -> list[dict]:
    q1 = _http_quote("002851.SZ")
    q2 = _http_quote("601398.SH")
    pre1 = float(q1.get("prev_close") or 0)
    pre2 = float(q2.get("prev_close") or 0)
    if not (pre1 and pre2):
        raise SystemExit("盘口不完整")
    # 买入价必须 <= 昨收 × 0.95（±5% 闸），按分向下取整避开浮点边界
    buy_cap = math.ceil(pre1 * 0.95 * 100) / 100
    ask1 = float((q1.get("asks") or [{}])[0].get("price") or 0)
    if ask1 and ask1 <= buy_cap:
        buy_fill_price, buy_fill_type = buy_cap, "LIMIT"
    else:
        # 现价已跌破 -5%：限价进不去，用市价（风控只在 price>0 时算偏离）
        buy_fill_price, buy_fill_type = 0.0, "MARKET"
    print(
        f"002851 pre={pre1} ask1={ask1} 买入上限={buy_cap} → "
        f"{buy_fill_type} {buy_fill_price}"
    )
    print(f"601398 pre={pre2} 挂单价={round(pre2 * 0.98, 2)}")
    return [
        {
            "name": "SELL-CANCEL",
            "symbol": "002851.SZ",
            "side": "SELL",
            "price": round(pre1 * 1.04, 2),
            "qty": 100,
            "order_type": "LIMIT",
            "want": "SUBMITTED",
        },
        {
            "name": "BUY-CANCEL",
            "symbol": "601398.SH",
            "side": "BUY",
            "price": round(pre2 * 0.98, 2),
            "qty": 100,
            "order_type": "LIMIT",
            "want": "SUBMITTED",
        },
        {
            "name": "BUY-FILL",
            "symbol": "002851.SZ",
            "side": "BUY",
            "price": buy_fill_price,
            "qty": 100,
            "order_type": buy_fill_type,
            "want": "FILLED",
        },
        {
            "name": "SELL-FILL",
            "symbol": "002851.SZ",
            "side": "SELL",
            "price": 0.0,
            "qty": 100,
            "order_type": "MARKET",
            "want": "FILLED",
        },
    ]


async def main(live: bool, only: str = "") -> int:
    client = QmtExecClient.from_env()
    await client.refresh_settings()
    print("mode:", "LIVE" if live else "DRY-RUN")
    acct = _http_account()
    asset = acct.get("asset") or {}
    tv, av = _pos_of(acct, "002851.SZ")
    print(
        f"账户 现金 {asset.get('cash', 0):,.2f} 总资产 {asset.get('balance', 0):,.2f}; "
        f"002851 持仓 {tv} 可用 {av}"
    )
    plan = _build_plan()
    if only:
        wanted = {x.strip() for x in only.split(",") if x.strip()}
        plan = [p for p in plan if p["name"] in wanted]
    if not live:
        for spec in plan:
            print("  PLAN", spec)
        return 0
    if any(spec["side"] == "SELL" for spec in plan) and av < 100:
        print("002851 可用不足 100，无法做卖出用例")
        return 2

    results = []
    for spec in plan:
        results.append(await _case(client, spec))
        await asyncio.sleep(0.8)

    print("\n=== 汇总 ===")
    for r in results:
        print(json.dumps(r, ensure_ascii=False))
    with open("/tmp/qm_qmt_stability_probe_20261030.json", "w", encoding="utf-8") as fh:
        json.dump(results, fh, ensure_ascii=False, indent=1, default=str)

    acct2 = _http_account()
    a2 = acct2.get("asset") or {}
    tv2, av2 = _pos_of(acct2, "002851.SZ")
    print(f"\n收盘前核对: 002851 持仓 {tv2} 可用 {av2}; 现金 {a2.get('cash', 0):,.2f}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--only", default="", help="只跑指定用例名，逗号分隔")
    args = ap.parse_args()
    raise SystemExit(asyncio.run(main(args.live, args.only)))
