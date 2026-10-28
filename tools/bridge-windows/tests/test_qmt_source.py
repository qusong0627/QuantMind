"""QMT 上游渠道测试（假伴生桥，不碰真 QMT）。

守护的是「通达信桥新增 qmt 渠道」的契约：
- 统一行情：QMT tick 必须归一成与 TDX/THS 相同的字段名；
- 账户：``/api/v1/account/query {"source":"qmt"}`` 返回与 TDX 同口径的 asset/positions；
- 委托：``/api/v1/orders/query {"source":"qmt"}`` 返回同口径 orders；
- 健康：``/api/v1/qmt/health`` 如实反映伴生桥在线/离线，不把「没起来」说成「0 资产」；
- 只读：qmt 渠道不得透传下单/撤单方法；
- 失败语义：伴生桥不可达 → 502；伴生桥报错 → 400（不静默返回空账户）。

运行: cd bridge-windows && python3 tests/test_qmt_source.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aiohttp import web  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from src.api.routes import build_app  # noqa: E402
from src.api.qmt_source import (  # noqa: E402
    normalize_orders,
    normalize_positions,
    normalize_trades,
)
from src.market.schema import normalize_qmt  # noqa: E402

TOKEN = "q" * 64
AUTH = {"Authorization": "Bearer " + TOKEN}

ACCOUNT_SNAPSHOT = {
    "account_id": "00000000",
    "updated_at": "2026-10-28 11:16:04",
    "age_s": 2.4,
    "asset": {"cash": 21626929.45, "total_asset": 23817569.95,
              "frozen_cash": 0.0, "market_value": 2200208.2},
    "positions": {
        "600000.SH": {"stock_code": "600000.SH", "volume": 100, "available": 100,
                      "cost": 9.4399001, "stock_name": "浦发银行",
                      "last_price": 9.5, "market_value": 950.0, "pnl": 6.01},
        "501018.SH": {"stock_code": "501018.SH", "volume": 96200, "available": 96200,
                      "cost": 1.9872, "stock_name": "南方原油LOF",
                      "last_price": None, "market_value": None, "pnl": None},
    },
}

ORDERS = [
    {"order_id": "1080000012", "stock_code": "600000.SH", "instrument_name": "浦发银行",
     "side": "BUY", "order_volume": 100, "traded_volume": 100, "price": 9.45,
     "traded_price": 9.44, "status_code": 56, "status": "FILLED",
     "status_msg": "全部成交", "order_remark": "qm-demo"},
]

TRADES = [
    {"trade_id": "T1", "order_id": "1080000012", "stock_code": "600000.SH",
     "instrument_name": "浦发银行", "side": "BUY", "traded_volume": 100,
     "traded_price": 9.44, "traded_amount": 944.0, "traded_at": "20261028 10:31:05",
     "order_remark": "qm-demo"},
]

TICK = {
    "600519.SH": {
        "time": 1793157368000, "timetag": "20261028 11:16:08",
        "lastPrice": 1332.55, "open": 1354.98, "high": 1354.98, "low": 1330.17,
        "lastClose": 1355.9, "amount": 2689281300.0, "volume": 20047,
        "askPrice": [1332.57], "askVol": [1], "bidPrice": [1332.51], "bidVol": [15],
    }
}

RAW_ACCOUNT = [{
    "m_strAccountID": "00000000", "m_strTradingDate": "20261028",
    "m_dBalance": 23818655.75, "m_dAvailable": 21626929.45,
    "m_dInstrumentValue": 2201452.90, "m_dCurrMargin": 2191726.30,
    "m_dPositionProfit": 81464.71, "m_dStockValue": 1845401.0,
    "m_dFundValue": 356051.90, "m_dFrozenCash": 0.0,
    "m_dPreBalance": 23818655.75,
}]


class _FakeCompanion:
    """最小伴生桥：只实现本测试用到的方法，并记录调用。"""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.fail = False

    async def call(self, request: web.Request):
        if self.fail:
            return web.json_response({"success": False, "error": "pipe busy"}, status=400)
        body = await request.json()
        method = body.get("method")
        params = body.get("params") or {}
        self.calls.append((method, params))
        if method == "account_snapshot":
            return web.json_response({"success": True, "result": ACCOUNT_SNAPSHOT})
        if method == "query_orders":
            return web.json_response({"success": True, "result": ORDERS})
        if method == "query_trades":
            return web.json_response({"success": True, "result": TRADES})
        if method == "get_full_tick":
            return web.json_response({"success": True, "result": TICK})
        if method == "query_account_infos":
            return web.json_response({"success": True, "result": RAW_ACCOUNT})
        return web.json_response({"success": False, "error": f"bad method {method}"},
                                 status=400)

    async def health(self, request: web.Request):
        if self.fail:
            return web.json_response({"success": False}, status=500)
        return web.json_response({"success": True, "source": "qmt",
                                  "metrics": {"state": "ok", "queue_len": 0}})


class _FakeTdx:
    def health_check_fast(self, timeout: float = 1.0) -> bool:
        return True

    def get_market_snapshot(self, code):
        return {"Code": code, "Now": 10.0}


class _FakeExecutor:
    class _Guard:
        def stats(self):
            return {"day_count": 0, "day_limit": 1999}

    order_guard = _Guard()


def _app():
    return build_app(TOKEN, _FakeTdx(), _FakeExecutor(), object(), object(), cache_db=None)


def test_normalize_qmt_tick():
    out = normalize_qmt(TICK["600519.SH"])
    assert out["last"] == 1332.55
    assert out["prev_close"] == 1355.9
    assert out["open"] == 1354.98
    assert out["high"] == 1354.98
    assert out["low"] == 1330.17
    assert out["volume"] == 20047
    assert out["amount"] == 2689281300.0
    assert out["change_pct"] == round((1332.55 - 1355.9) / 1355.9 * 100, 4)
    assert out["bids"] == [{"level": 1, "price": 1332.51, "volume": 15}]
    assert out["asks"] == [{"level": 1, "price": 1332.57, "volume": 1}]
    # 缺失字段一律 None，不填 0
    assert out["inside"] is None and out["outside"] is None
    assert normalize_qmt({})["last"] is None


def test_positions_accept_kit_price():
    """实时 RPC 回落的持仓用 kit 的 ``price`` 字段，归一后必须变成 last_price。"""
    positions = normalize_positions({
        "600519.SH": {
            "stock_code": "600519.SH", "volume": 100, "available": 100,
            "cost": 10.0, "price": 11.0, "stock_name": "贵州茅台",
        }
    })
    assert positions[0]["last_price"] == 11.0
    assert positions[0]["pnl"] == 100.0


def test_normalize_kit_shapes():
    """kit 的 OrderSnapshot/TradeSnapshot 不是 quantmind 的改名版：
    字段是 order_sys_id / action / volume / status 数字串 / order_time 秒。
    归一必须认这套，否则真实委托在页面显示成空方向、状态 '55'。"""
    orders = normalize_orders([{
        "order_sys_id": "123", "stock_code": "600519.SH",
        "instrument_name": "贵州茅台", "action": "BUY", "volume": 200,
        "traded_volume": 100, "status": "55", "price": 10.5,
        "traded_price": 10.4, "order_time": 34200, "user_order_id": "qm-1",
    }])
    assert orders[0]["order_id"] == "123"
    assert orders[0]["side"] == "buy"
    assert orders[0]["status"] == "partially_filled"
    assert orders[0]["total_volume"] == 200
    assert orders[0]["filled_volume"] == 100
    assert orders[0]["time"] == "09:30:00"
    assert orders[0]["order_remark"] == "qm-1"

    trades = normalize_trades([{
        "trade_id": "T9", "order_sys_id": "123", "stock_code": "600519.SH",
        "action": "SELL", "volume": 100, "price": 10.6, "amount": 1060.0,
        "traded_at": "20261028 10:00:00", "user_order_id": "qm-1",
    }])
    assert trades[0]["side"] == "sell"
    assert trades[0]["order_id"] == "123"
    assert trades[0]["traded_volume"] == 100
    assert trades[0]["traded_amount"] == 1060.0


async def _run():
    companion = _FakeCompanion()
    companion_app = web.Application()
    companion_app.router.add_post("/api/v1/qmt/call", companion.call)
    companion_app.router.add_get("/health", companion.health)

    async with TestClient(TestServer(companion_app)) as cclient:
        port = cclient.server.port
        os.environ["QMT_SOURCE_URL"] = f"http://127.0.0.1:{port}"
        async with TestClient(TestServer(_app())) as client:
            # 1. 账户：qmt 源 → 与 TDX 同口径
            resp = await client.post("/api/v1/account/query", json={"source": "qmt"},
                                     headers=AUTH)
            body = await resp.json()
            assert resp.status == 200, body
            assert body["channel_used"] == "qmt" and body["account_source"] == "qmt"
            assert body["account_id"] == "00000000"
            assert body["asset"]["currency"] == "CNY"
            assert body["asset"]["balance"] == 23817569.95
            assert body["asset"]["cash"] == 21626929.45
            assert body["asset"]["asset"] == 23817569.95
            assert body["asset"]["market_value"] == 2200208.2
            pos = {p["stock_code"]: p for p in body["positions"]}
            assert pos["600000.SH"]["total_volume"] == 100
            assert pos["600000.SH"]["available_volume"] == 100
            assert pos["600000.SH"]["cost_price"] == 9.4399001
            assert pos["600000.SH"]["stock_name"] == "浦发银行"
            assert pos["600000.SH"]["last_price"] == 9.5
            assert pos["501018.SH"]["last_price"] is None

            # 2. 委托
            resp = await client.post("/api/v1/orders/query", json={"source": "qmt"},
                                     headers=AUTH)
            body = await resp.json()
            assert resp.status == 200, body
            order = body["orders"][0]
            assert order["order_id"] == "1080000012"
            assert order["side"] == "buy"
            assert order["status"] == "filled"
            assert order["total_volume"] == 100
            assert order["filled_volume"] == 100
            assert order["order_price"] == 9.45
            assert body["channel_used"] == "qmt"

            # 3. 成交
            resp = await client.post("/api/v1/orders/query",
                                     json={"source": "qmt", "kind": "trades"},
                                     headers=AUTH)
            body = await resp.json()
            assert resp.status == 200, body
            assert body["trades"][0]["trade_id"] == "T1"
            assert body["trades"][0]["traded_volume"] == 100

            # 3.5 资产口径对账：QMT 总资产 = 现金 + 占用；实时总资产 = 现金 + 市值
            resp = await client.get("/api/v1/qmt/reconcile", headers=AUTH)
            rec = await resp.json()
            assert resp.status == 200, rec
            assert rec["qmt_total_asset"] == 23818655.75
            assert rec["live_total_asset"] == 23828382.35
            assert rec["market_value_minus_occupied"] == 9726.6
            assert rec["position_profit"] == 81464.71

            # 4. 健康
            resp = await client.get("/api/v1/qmt/health", headers=AUTH)
            body = await resp.json()
            assert resp.status == 200 and body["online"] is True
            assert body["metrics"]["state"] == "ok"

            # 5. 统一行情按 prefer=qmt 取 QMT
            resp = await client.get("/api/v1/market/quote",
                                    params={"code": "600519.SH", "prefer": "qmt"},
                                    headers=AUTH)
            body = await resp.json()
            assert resp.status == 200, body
            assert body["data"]["data"]["last"] == 1332.55
            assert body["data"]["source"]["last"] == "qmt"
            assert body["data"]["timings"]["qmt"]["ok"] is True

            # 6. 只读：下单/撤单方法不得被 qmt 渠道透传
            for method in ("order_stock", "cancel_order_stock", "passorder"):
                resp = await client.post("/api/v1/tdx/call",
                                         json={"source": "qmt", "method": method,
                                               "params": {}}, headers=AUTH)
                assert resp.status == 403, f"{method} 应 403，实际 {resp.status}"
            assert all(c[0] not in ("order_stock", "cancel_order_stock", "passorder")
                       for c in companion.calls)

            # 7. 伴生桥报错 → 400 + QMT_SOURCE_ERROR（不伪装成空账户）
            companion.fail = True
            resp = await client.post("/api/v1/account/query", json={"source": "qmt"},
                                     headers=AUTH)
            body = await resp.json()
            assert resp.status == 400, body
            assert body["error"]["code"] == "QMT_SOURCE_ERROR"
            companion.fail = False

            # 8. 伴生桥不可达 → 502 + QMT_SOURCE_UNAVAILABLE
            os.environ["QMT_SOURCE_URL"] = "http://127.0.0.1:9"
            resp = await client.post("/api/v1/account/query", json={"source": "qmt"},
                                     headers=AUTH)
            body = await resp.json()
            assert resp.status == 502, body
            assert body["error"]["code"] == "QMT_SOURCE_UNAVAILABLE"

    print("✅ test_qmt_source 全部通过")


if __name__ == "__main__":
    test_normalize_qmt_tick()
    test_positions_accept_kit_price()
    test_normalize_kit_shapes()
    asyncio.run(_run())
