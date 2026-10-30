"""TFT 调仓收尾（2026-10-30）：补卖 14 笔 + 买入 10 笔。

约束（桥侧 risk_gate，pipe_redis_bridge.py）：
  - 单笔金额 <= 200,000 → 超额按整百股拆分
  - 限价相对昨收偏离 <= 5% → 超限改用 MARKET（price_type=LATEST_PRICE）
  - 30 笔/分钟 → 每笔间隔 2.4s
  - ST/退 名称黑名单：卖出同样拦截（本次不处理）
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import time
from typing import Any

import redis  # noqa: F401  # 确保依赖可用（容器内已有）
from backend.services.live_trading.services.qmt_exec_client import (
    QmtExecClient,
    QmtExecError,
)

BRIDGE_URL = os.getenv("TDX_BRIDGE_URL", "http://192.168.31.13:8550")
BRIDGE_TOKEN = os.getenv("TDX_BRIDGE_TOKEN", "")
BUY_LIST = [
    "601869.SH",
    "002384.SZ",
    "002281.SZ",
    "300475.SZ",
    "300750.SZ",
    "300308.SZ",
    "002851.SZ",
    "600183.SH",
    "300757.SZ",
    "603256.SH",
]
TARGET_VALUE = 100_000.0
MAX_ORDER_AMOUNT = 200_000.0
MAX_DEV_PCT = 5.0
ORDER_INTERVAL_S = 2.4
RUN_TAG = "qm2-" + time.strftime("%Y%m%d")
BLACKLISTED_NAMES = ("ST", "退")


def _round2(v: float) -> float:
    return round(float(v) + 1e-9, 2)


def http_account() -> dict[str, Any]:
    import urllib.request

    req = urllib.request.Request(
        f"{BRIDGE_URL}/api/v1/account/query",
        data=json.dumps({"source": "qmt"}).encode(),
        headers={
            "Authorization": f"Bearer {BRIDGE_TOKEN}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310
        return json.loads(resp.read().decode())


async def fetch_ticks(client: QmtExecClient, symbols: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for i in range(0, len(symbols), 50):
        data = await client.get_full_tick(symbols[i : i + 50])
        if isinstance(data, dict):
            for k, v in data.items():
                if isinstance(v, dict):
                    out[str(k).upper()] = v
    return out


def _prices(tick: dict) -> tuple[float, float, float]:
    last = float(tick.get("lastPrice") or 0)
    pre = float(tick.get("lastClose") or 0)
    bids = tick.get("bidPrice") or []
    asks = tick.get("askPrice") or []
    bid1 = float(bids[0]) if bids and bids[0] else 0.0
    ask1 = float(asks[0]) if asks and asks[0] else 0.0
    return last, bid1, ask1, pre


def _is_blacklisted(name: str) -> bool:
    return any(kw in (name or "") for kw in BLACKLISTED_NAMES)


def build_sells(positions: list[dict], ticks: dict[str, dict]) -> list[dict]:
    out = []
    for p in positions:
        code = str(p.get("stock_code") or "").upper()
        if code in BUY_LIST:
            continue
        vol = int(float(p.get("available_volume") or 0))
        if vol <= 0:
            continue
        name = p.get("stock_name") or ""
        if _is_blacklisted(name):
            continue  # 桥侧名称黑名单：卖出也会拒，跳过
        tick = ticks.get(code) or {}
        last, bid1, _, pre = _prices(tick)
        if bid1 <= 0 and last <= 0:
            continue
        use_market = False
        price = bid1
        if price <= 0:
            use_market = True
        elif pre > 0 and abs(price / pre - 1) * 100.0 > MAX_DEV_PCT:
            use_market = True
        ref = price if price > 0 else (pre or last)
        # 按 20 万上限拆单（整百股）
        chunks: list[int] = []
        remaining = vol
        while remaining > 0:
            max_shares = int(MAX_ORDER_AMOUNT / ref / 100.0) * 100
            if max_shares <= 0:
                max_shares = 100
            take = min(remaining, max_shares)
            if remaining - take < 100:  # 避免留下碎股单独成单
                take = remaining
            chunks.append(take)
            remaining -= take
        for qty in chunks:
            out.append(
                {
                    "symbol": code,
                    "name": name,
                    "quantity": qty,
                    "price": 0.0 if use_market else _round2(price),
                    "order_type": "MARKET" if use_market else "LIMIT",
                    "reference": last,
                }
            )
    return out


def build_buys(positions: list[dict], ticks: dict[str, dict]) -> list[dict]:
    held = {str(p.get("stock_code") or "").upper(): p for p in positions}
    out = []
    for code in BUY_LIST:
        tick = ticks.get(code) or {}
        last, _, ask1, pre = _prices(tick)
        use_market = False
        price = ask1
        if price <= 0:
            use_market = True
        elif pre > 0 and abs(price / pre - 1) * 100.0 > MAX_DEV_PCT:
            use_market = True
        ref = price if price > 0 else (pre or last)
        if ref <= 0:
            continue
        existing_mv = float((held.get(code) or {}).get("market_value") or 0)
        budget = max(0.0, TARGET_VALUE - existing_mv)
        qty = int(math.floor(budget / ref / 100.0) * 100)
        if qty < 100:
            continue
        out.append(
            {
                "symbol": code,
                "name": (held.get(code) or {}).get("stock_name") or "",
                "quantity": qty,
                "price": 0.0 if use_market else _round2(price),
                "order_type": "MARKET" if use_market else "LIMIT",
                "reference": last,
                "amount": _round2(qty * ref),
            }
        )
    return out


async def submit_all(client: QmtExecClient, items: list[dict], side: str) -> list[dict]:
    results = []
    for it in items:
        cid = f"{RUN_TAG}-{side.lower()}-{it['symbol']}-{it['quantity']}"
        rec = {"side": side, **it, "client_order_id": cid}
        try:
            res = await client.submit_order(
                symbol=it["symbol"],
                side=side,
                quantity=it["quantity"],
                order_type=it["order_type"],
                price=it["price"],
                client_order_id=cid,
            )
            rec["ok"] = True
            rec["result"] = res
            print(
                f"  {side:4} {it['symbol']:<11}{it['name']:<9}{it['quantity']:>6} "
                f"{it['order_type']:<6}@{it['price']:>7.2f} -> {res.get('order_id')}"
            )
        except QmtExecError as exc:
            rec["ok"] = False
            rec["error"] = f"{exc.code}: {exc}"
            print(
                f"  {side:4} {it['symbol']:<11}{it['name']:<9}{it['quantity']:>6} 失败: {rec['error'][:120]}"
            )
        results.append(rec)
        await asyncio.sleep(ORDER_INTERVAL_S)
    return results


async def main(live: bool) -> int:
    client = QmtExecClient.from_env()
    await client.refresh_settings()
    print(
        "mode:",
        "LIVE" if live else "DRY-RUN",
        "enabled:",
        client.effective_config()["enabled"],
    )
    acct = http_account()
    positions = acct.get("positions") or []
    asset = acct.get("asset") or {}
    print(
        f"账户: 总资产 {asset.get('balance', 0):,.2f} 现金 {asset.get('cash', 0):,.2f} "
        f"市值 {asset.get('market_value', 0):,.2f} 持仓 {len(positions)} 只 "
        f"(snapshot {acct.get('snapshot_at')}, stale={acct.get('stale')})"
    )
    symbols = [str(p.get("stock_code") or "").upper() for p in positions]
    for code in BUY_LIST:
        if code not in symbols:
            symbols.append(code)
    ticks = await fetch_ticks(client, [s for s in symbols if s])
    print(f"盘口: {len(ticks)}/{len(symbols)}")

    sells = build_sells(positions, ticks)
    buys = build_buys(positions, ticks)
    print(f"\n=== 卖出计划 {len(sells)} 笔 ===")
    for s in sells:
        print(
            f"  SELL {s['symbol']:<11}{s['name']:<9}{s['quantity']:>7} {s['order_type']:<6}"
            f"@{s['price']:>7.2f} (现价 {s['reference']:.2f})"
        )
    print(f"\n=== 买入计划 {len(buys)} 笔 ===")
    for b in buys:
        print(
            f"  BUY  {b['symbol']:<11}{b['name']:<9}{b['quantity']:>7} {b['order_type']:<6}"
            f"@{b['price']:>7.2f} 金额 {b['amount']:>9,.0f}"
        )
    if not live:
        print("\n[DRY-RUN] 未提交。")
        return 0
    print("\n=== 执行卖出 ===")
    sell_res = await submit_all(client, sells, "SELL")
    print("\n=== 执行买入 ===")
    buy_res = await submit_all(client, buys, "BUY")
    await asyncio.sleep(4)
    acct2 = http_account()
    a2 = acct2.get("asset") or {}
    print(
        f"\n账户(后): 总资产 {a2.get('balance', 0):,.2f} 现金 {a2.get('cash', 0):,.2f} "
        f"市值 {a2.get('market_value', 0):,.2f} 持仓 {len(acct2.get('positions') or [])} 只"
    )
    with open("/tmp/qm_tft_finish_20261030.json", "w", encoding="utf-8") as fh:
        json.dump(
            {"before": acct, "after": acct2, "sells": sell_res, "buys": buy_res},
            fh,
            ensure_ascii=False,
            default=str,
            indent=1,
        )
    print("结果 -> /tmp/qm_tft_finish_20261030.json")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true")
    args = ap.parse_args()
    raise SystemExit(asyncio.run(main(args.live)))
