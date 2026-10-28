"""渠道专用交易接口测试（假 TDX 执行器 + 假 QMT 伴生桥）。

守护：
- 通达信/ QMT 买、卖、撤、查各有独立入口；
- /orders/route 按每笔 channel 分流，混合单两边的字段不串；
- QMT 写闸默认关，关着返回 QMT_ORDERS_DISABLED；
- QMT 市价必须映射成 LATEST_PRICE（kit 别名里没有 MARKET）。

运行: cd bridge-windows && python3 tests/test_order_api.py
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aiohttp import web  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from src.api.routes import build_app  # noqa: E402

TOKEN = "o" * 64
AUTH = {"Authorization": "Bearer " + TOKEN}


class _FakeCompanion:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    async def call(self, request: web.Request):
        body = await request.json()
        method = body.get("method")
        params = body.get("params") or {}
        self.calls.append((method, params))
        if method in ("order_stock", "submit_order"):
            return web.json_response({"success": True, "result": {
                "status": "SUBMITTED",
                "user_order_id": params.get("remark"),
                "order_sys_id": "QMT-123",
                "message": "passorder submitted",
            }})
        if method in ("cancel_order_stock", "cancel_order"):
            return web.json_response({"success": True, "result": {
                "status": "CANCELLED",
                "order_sys_id": params.get("order_sys_id") or params.get("order_id"),
            }})
        return web.json_response({"success": False, "error": "bad method"}, status=400)


class _FakeTdx:
    def __init__(self):
        self.cancelled: list[tuple] = []

    def health_check_fast(self, timeout: float = 1.0) -> bool:
        return True

    def get_market_snapshot(self, code):
        return {"Code": code, "Now": 10.0}

    def cancel_order_stock(self, account_id, stock_code, order_id):
        self.cancelled.append((account_id, stock_code, order_id))
        return {"Value": "2", "Msg": "撤单已受理"}

    def query_stock_orders(self, account_id, stock_code="", cancelable_only=False):
        return []


class _FakeExecutor:
    class _Guard:
        def stats(self):
            return {"day_count": 0, "day_limit": 1999}

    order_guard = _Guard()

    def __init__(self):
        self.plans = []

    def resolve_account_id(self, account="", account_type="stock"):
        return 10000001

    def execute_plan(self, plan):
        self.plans.append(plan)
        orders = []
        for o in plan.orders:
            orders.append({
                "stock_code": o.stock_code,
                "side": o.side,
                "volume": o.volume,
                "order_id": "TDX-" + o.stock_code,
                "status": "filled",
                "message": "ok",
                "price_type": o.price_type,
            })
        return {"plan_id": plan.plan_id, "status": "executed",
                "orders": orders, "executed_at": "2026-10-28T13:00:00"}

    def log_trade(self, payload):
        pass


def _app(executor, qmt_orders_enabled=True):
    return build_app(TOKEN, _FakeTdx(), executor, object(), object(),
                     cache_db=None, qmt_orders_enabled=qmt_orders_enabled)


async def _run():
    companion = _FakeCompanion()
    companion_app = web.Application()
    companion_app.router.add_post("/api/v1/qmt/call", companion.call)

    async with TestClient(TestServer(companion_app)) as cclient:
        os.environ["QMT_SOURCE_URL"] = f"http://127.0.0.1:{cclient.server.port}"
        executor = _FakeExecutor()
        async with TestClient(TestServer(_app(executor))) as client:
            # 1. QMT 买入：字段按 QMT 口径，返回 channel/interface
            resp = await client.post("/api/v1/qmt/orders/buy", headers=AUTH, json={
                "symbol": "600519.SH", "volume": 100, "order_type": "limit",
                "price": 1332.55, "client_order_id": "qm-buy-1",
            })
            body = await resp.json()
            assert resp.status == 200, body
            assert body["channel"] == "qmt" and body["interface"] == "qmt_pipe"
            assert body["order"]["order_id"] == "QMT-123"
            assert body["order"]["client_order_id"] == "qm-buy-1"
            method, params = companion.calls[-1]
            assert method == "order_stock"
            assert params["action"] == "BUY"
            assert params["price_type"] == "LIMIT"
            assert params["remark"] == "qm-buy-1"

            # 2. QMT 卖出市价：LATEST_PRICE，不是 MARKET
            resp = await client.post("/api/v1/qmt/orders/sell", headers=AUTH, json={
                "symbol": "002883.SZ", "volume": 60000, "order_type": "market",
                "client_order_id": "qm-sell-1",
            })
            body = await resp.json()
            assert resp.status == 200, body
            method, params = companion.calls[-1]
            assert params["action"] == "SELL"
            assert params["price_type"] == "LATEST_PRICE"
            assert params["price"] == 0.0

            # 3. QMT 撤单
            resp = await client.post("/api/v1/qmt/orders/cancel", headers=AUTH, json={
                "order_id": "QMT-123", "symbol": "600519.SH",
            })
            body = await resp.json()
            assert resp.status == 200, body
            assert body["channel"] == "qmt"
            assert companion.calls[-1][0] == "cancel_order_stock"

            # 4. TDX 买入：走 TradePlan，price_type=0
            resp = await client.post("/api/v1/tdx/orders/buy", headers=AUTH, json={
                "stock_code": "600036.SH", "volume": 100, "order_type": "limit",
                "price": 42.0,
            })
            body = await resp.json()
            assert resp.status == 200, body
            assert body["channel"] == "tdx" and body["interface"] == "tdx_tq"
            assert body["order"]["order_id"] == "TDX-600036.SH"
            plan = executor.plans[-1]
            assert plan.orders[0].price_type == 0
            assert plan.orders[0].order_type == "limit"

            # 5. TDX 卖出市价：price_type=1
            resp = await client.post("/api/v1/tdx/orders/sell", headers=AUTH, json={
                "stock_code": "600036.SH", "volume": 100, "order_type": "market",
            })
            body = await resp.json()
            assert resp.status == 200, body
            assert executor.plans[-1].orders[0].price_type == 1

            # 6. TDX 撤单
            resp = await client.post("/api/v1/tdx/orders/cancel", headers=AUTH, json={
                "stock_code": "600036.SH", "order_id": "TDX-1",
            })
            body = await resp.json()
            assert resp.status == 200, body
            assert body["success"] is True and body["channel"] == "tdx"

            # 7. 混合路由：一笔 TDX + 一笔 QMT，结果按输入顺序
            before_tdx = len(executor.plans)
            before_qmt = len(companion.calls)
            resp = await client.post("/api/v1/orders/route", headers=AUTH, json={
                "plan_id": "mix-1",
                "orders": [
                    {"channel": "tdx", "symbol": "600519.SH", "side": "buy",
                     "volume": 100, "order_type": "limit", "price": 1332.55},
                    {"channel": "qmt", "symbol": "002883.SZ", "side": "sell",
                     "volume": 100, "order_type": "limit", "price": 8.59,
                     "client_order_id": "qm-mix-1"},
                ],
            })
            body = await resp.json()
            assert resp.status == 200, body
            assert body["success"] is True
            assert [r["channel"] for r in body["results"]] == ["tdx", "qmt"]
            assert body["results"][0]["order"]["order_id"] == "TDX-600519.SH"
            assert body["results"][1]["order"]["order_id"] == "QMT-123"
            assert len(executor.plans) == before_tdx + 1
            assert len(companion.calls) == before_qmt + 1

            # 8. 校验：QMT 缺 client_order_id
            resp = await client.post("/api/v1/qmt/orders/buy", headers=AUTH, json={
                "symbol": "600519.SH", "volume": 100, "order_type": "limit",
                "price": 10.0,
            })
            assert resp.status == 400, await resp.text()

        # 9. 写闸默认关
        async with TestClient(TestServer(_app(_FakeExecutor(),
                                              qmt_orders_enabled=False))) as closed:
            resp = await closed.post("/api/v1/qmt/orders/buy", headers=AUTH, json={
                "symbol": "600519.SH", "volume": 100, "order_type": "limit",
                "price": 10.0, "client_order_id": "qm-closed",
            })
            body = await resp.json()
            assert resp.status == 403, body
            assert body["error"]["code"] == "QMT_ORDERS_DISABLED"

    print("✅ test_order_api 全部通过")


if __name__ == "__main__":
    asyncio.run(_run())
