"""午后实盘验证（2026-10-30 13:00+）：卖出价格带 / 异步成交闭环 / 突发+撤单。

步骤（全部可回滚，净持仓不变）：
  1. 撤掉一档 002883 挂单（≥15,000 股）腾出可用股，确认 available 恢复；
  2. 卖 100 股 @ 昨收-20% → 期望被 `risk: sell price band` 拒绝（不产生委托）；
  3. 把撤掉的股数按买一价补回；
  4. 市价买 100 股 601398（异步受理）→ 台账里等 poller 回填终态；
  5. 5 笔 002883 限价挂单（+4%，同步模式拿 order_id）→ 全部撤掉，观察受理延迟。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import urllib.request

from backend.services.live_trading.services.qmt_exec_client import (
    QmtExecClient,
    QmtExecError,
)

BRIDGE_URL = os.getenv("TDX_BRIDGE_URL", "http://192.168.31.13:8550").rstrip("/")
BRIDGE_TOKEN = os.getenv("TDX_BRIDGE_TOKEN", "")
ST_SYMBOL = "002883.SZ"
BUY_SYMBOL = "601398.SH"


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


def _acct() -> dict:
    return _http("/api/v1/account/query", {"source": "qmt"})


def _pos(acct: dict, code: str) -> tuple[int, int]:
    for p in acct.get("positions") or []:
        if str(p.get("stock_code") or "").upper() == code:
            return int(p.get("total_volume") or 0), int(p.get("available_volume") or 0)
    return 0, 0


def _quote(code: str) -> dict:
    payload = _http(f"/api/v1/market/quote?code={code}&source=tdx")
    return (payload.get("data") or {}).get("data") or {}


async def _wait_status(
    c: QmtExecClient, oid: str, wanted: set[str], timeout: float = 25.0
) -> str:
    t0 = time.monotonic()
    last = ""
    while time.monotonic() - t0 < timeout:
        try:
            for o in await c.query_orders():
                if str(o.get("order_id")) == str(oid):
                    last = str(o.get("status") or "")
                    if last in wanted:
                        return last
        except QmtExecError:
            pass
        await asyncio.sleep(0.8)
    return last or "UNKNOWN"


async def main() -> int:
    c = QmtExecClient.from_env()
    await c.refresh_settings()
    report: dict = {}

    acct = _acct()
    print(
        "账户:",
        {k: round(acct["asset"][k], 2) for k in ("cash", "market_value", "balance")},
    )
    print("002883 持仓/可用:", _pos(acct, ST_SYMBOL))

    # ---- 1) 撤一档挂单腾可用股
    orders = await c.query_orders(cancelable_only=True)
    tranche = next(
        (
            o
            for o in orders
            if o.get("symbol") == "SZ002883"
            and int(o.get("order_volume") or 0) >= 15000
        ),
        None,
    )
    if tranche is None:
        print("没有可撤的 002883 大档挂单，跳过 1-3")
        report["cancel_skip"] = True
    else:
        oid = str(tranche["order_id"])
        vol = int(tranche["order_volume"])
        await c.cancel_order(order_id=oid, symbol=ST_SYMBOL)
        st = await _wait_status(c, oid, {"CANCELLED", "FILLED"})
        await asyncio.sleep(2.0)
        total2, avail2 = _pos(_acct(), ST_SYMBOL)
        print(f"撤单 {oid}（{vol} 股）→ {st}；可用 {avail2}")
        report["cancel"] = {
            "order_id": oid,
            "volume": vol,
            "status": st,
            "available": avail2,
        }

        # ---- 2) 价格带：-20% 卖单应被拒
        q = _quote(ST_SYMBOL)
        pre = float(q.get("prev_close") or 0) or float(q.get("last") or 0)
        bad_price = round(pre * 0.80, 2)
        try:
            r = await c.submit_order(
                symbol=ST_SYMBOL,
                side="SELL",
                quantity=100,
                order_type="LIMIT",
                price=bad_price,
                client_order_id=f"bandtest-{int(time.time())}",
            )
            print(f"[FAIL] -20% 卖单居然受理了: {r}")
            report["band"] = {"ok": False, "result": r}
        except QmtExecError as exc:
            msg = str(exc)
            ok = "sell price band" in msg
            print(
                f"[{'PASS' if ok else 'FAIL'}] -20% 卖单被拒: {exc.code}: {msg[:120]}"
            )
            report["band"] = {"ok": ok, "code": exc.code, "msg": msg[:200]}

        # ---- 3) 补回该档
        q2 = _quote(ST_SYMBOL)
        bid = float((q2.get("bids") or [{}])[0].get("price") or q2.get("last") or 0)
        try:
            rr = await c.submit_order(
                symbol=ST_SYMBOL,
                side="SELL",
                quantity=vol,
                order_type="LIMIT",
                price=round(bid, 2),
                client_order_id=f"restore-{int(time.time())}",
            )
            print(
                f"补回 {vol} 股 @ {round(bid, 2)} → order {rr.get('order_id') or '(async)'}"
            )
            report["restore"] = {
                "volume": vol,
                "price": round(bid, 2),
                "order_id": rr.get("order_id"),
            }
        except QmtExecError as exc:
            print("补回失败:", exc.code, str(exc)[:120])
            report["restore"] = {"error": f"{exc.code}: {str(exc)[:150]}"}

    # ---- 4) 异步市价买入（台账终态由 poller 回填）
    try:
        t0 = time.monotonic()
        r = await c.submit_order(
            symbol=BUY_SYMBOL,
            side="BUY",
            quantity=100,
            order_type="MARKET",
            price=0.0,
            client_order_id=f"asyncfill-{int(time.time())}",
        )
        accept_ms = round((time.monotonic() - t0) * 1000, 1)
        print(
            f"市价买 100 {BUY_SYMBOL} 受理 {accept_ms}ms "
            f"async={r.get('async')} remark={r.get('remark')}"
        )
        report["async_fill"] = {
            "accept_ms": accept_ms,
            "async": r.get("async"),
            "remark": r.get("remark"),
            "cid": "asyncfill-",
        }
    except QmtExecError as exc:
        print("异步买入失败:", exc.code, str(exc)[:150])
        report["async_fill"] = {"error": f"{exc.code}: {str(exc)[:150]}"}

    # ---- 5) 突发挂单 + 全撤（同步模式拿 order_id）
    saved = dict(c._page_settings or {})
    c._page_settings = {**saved, "async_orders": "false", "record_orders": "false"}
    try:
        q3 = _quote(ST_SYMBOL)
        pre3 = float(q3.get("prev_close") or 0)
        hi = round(pre3 * 1.04, 2)
        placed: list[str] = []
        lat: list[float] = []
        for i in range(5):
            t0 = time.monotonic()
            try:
                rr = await c.submit_order(
                    symbol=ST_SYMBOL,
                    side="SELL",
                    quantity=100,
                    order_type="LIMIT",
                    price=hi,
                    client_order_id=f"burst3-{int(time.time())}-{i}",
                )
                lat.append(round((time.monotonic() - t0) * 1000, 1))
                placed.append(str(rr.get("order_id") or ""))
            except QmtExecError as exc:
                print(f"  #{i} 失败 {exc.code}: {str(exc)[:100]}")
            await asyncio.sleep(0.3)
        print("突发受理:", [p for p in placed if p], "延迟:", lat)
        cancelled = 0
        for oid in [p for p in placed if p]:
            try:
                await c.cancel_order(order_id=str(oid), symbol=ST_SYMBOL)
                cancelled += 1
            except QmtExecError:
                pass
        print(f"撤单 {cancelled}/{len([p for p in placed if p])}")
        report["burst"] = {
            "accepted": len([p for p in placed if p]),
            "cancelled": cancelled,
            "latency_ms": lat,
        }
    finally:
        c._page_settings = saved

    with open("/tmp/qm_afternoon_verify_20261030.json", "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=1, default=str)
    print("\n结果 -> /tmp/qm_afternoon_verify_20261030.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
