"""下单渠道路由：通达信 / QMT 的字段归一与分流（纯函数，Linux 可测）。

接口矩阵（桥对外）：

  通达信：POST /api/v1/tdx/orders/{buy,sell,cancel,query}
  QMT   ：POST /api/v1/qmt/orders/{buy,sell,cancel,query}
  混合  ：POST /api/v1/orders/route  （每笔带 channel，按渠道分流）

两边的字段口径不同，路由层必须显式转换，不能把一套参数硬塞给另一套：

  TDX：stock_code / side(buy|sell) / volume / price_type(0=限价,1=市价) / price / notify
  QMT：stock_code / action(BUY|SELL) / volume / price_type(LIMIT|LATEST_PRICE) / price /
       remark(client_order_id) / strategy_name
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.api.order_routing import (  # noqa: E402
    normalize_order,
    qmt_cancel_params,
    qmt_order_params,
    split_orders,
    tdx_plan_dict,
)

TDX_BUY = {
    "channel": "tdx", "symbol": "600519.SH", "side": "buy",
    "volume": 100, "order_type": "limit", "price": 1332.55,
}
QMT_SELL = {
    "channel": "qmt", "symbol": "002883.SZ", "side": "sell",
    "volume": 60000, "order_type": "limit", "price": 8.59,
    "client_order_id": "qm-test-1",
}


def test_normalize_tdx_and_qmt():
    t = normalize_order(TDX_BUY)
    assert t["channel"] == "tdx" and t["symbol"] == "600519.SH"
    assert t["side"] == "buy" and t["volume"] == 100
    assert t["price"] == 1332.55 and t["order_type"] == "limit"

    q = normalize_order(QMT_SELL)
    assert q["channel"] == "qmt" and q["side"] == "sell"
    assert q["client_order_id"] == "qm-test-1"

    # 默认渠道
    d = normalize_order({"symbol": "000001.SZ", "side": "buy", "volume": 100,
                         "order_type": "market"}, default_channel="tdx")
    assert d["channel"] == "tdx" and d["price"] is None

    # 渠道必须可识别
    for bad in ({"channel": "futu", "symbol": "600519.SH", "side": "buy", "volume": 1},
                {"channel": "tdx", "side": "buy", "volume": 1},
                {"channel": "tdx", "symbol": "600519.SH", "side": "hold", "volume": 1},
                {"channel": "tdx", "symbol": "600519.SH", "side": "buy", "volume": 0},
                {"channel": "tdx", "symbol": "600519.SH", "side": "buy",
                 "volume": 100, "order_type": "limit"}):
        try:
            normalize_order(bad)
        except ValueError:
            continue
        raise AssertionError("应拒绝: %r" % (bad,))


def test_qmt_requires_client_order_id():
    try:
        normalize_order({"channel": "qmt", "symbol": "600519.SH", "side": "buy",
                         "volume": 100, "order_type": "market"})
    except ValueError as exc:
        assert "client_order_id" in str(exc)
    else:
        raise AssertionError("QMT 缺 client_order_id 应拒绝")


def test_split_orders_keeps_channels():
    tdx, qmt = split_orders([normalize_order(TDX_BUY), normalize_order(QMT_SELL)])
    assert len(tdx) == 1 and tdx[0]["symbol"] == "600519.SH"
    assert len(qmt) == 1 and qmt[0]["symbol"] == "002883.SZ"


def test_tdx_plan_dict_maps_price_type():
    plan = tdx_plan_dict("p1", [normalize_order(TDX_BUY)])
    assert plan["plan_id"] == "p1"
    o = plan["orders"][0]
    assert o["stock_code"] == "600519.SH" and o["side"] == "buy"
    assert o["price_type"] == 0 and o["order_type"] == "limit"
    assert o["price"] == 1332.55

    m = tdx_plan_dict("p2", [normalize_order({
        "channel": "tdx", "symbol": "000001.SZ", "side": "sell",
        "volume": 100, "order_type": "market",
    })])
    assert m["orders"][0]["price_type"] == 1
    assert m["orders"][0]["order_type"] == "market"


def test_qmt_params_maps_action_and_price_type():
    p = qmt_order_params(normalize_order(QMT_SELL))
    assert p["stock_code"] == "002883.SZ"
    assert p["action"] == "SELL"
    assert p["volume"] == 60000
    assert p["price"] == 8.59
    assert p["price_type"] == "LIMIT"
    assert p["remark"] == "qm-test-1"
    assert p["wait_settlement"] is True

    m = qmt_order_params(normalize_order({
        "channel": "qmt", "symbol": "510300.SH", "side": "buy",
        "volume": 100, "order_type": "market", "client_order_id": "qm-mkt",
    }))
    assert m["action"] == "BUY"
    assert m["price_type"] == "LATEST_PRICE"  # 不是 "MARKET"（kit 别名里没有）
    assert m["price"] == 0.0


def test_qmt_cancel_params():
    p = qmt_cancel_params({"channel": "qmt", "order_id": "123",
                           "symbol": "002883.SZ", "account_id": "40327478"})
    assert p["order_sys_id"] == "123"
    assert p["stock_code"] == "002883.SZ"
    assert p["account_id"] == "40327478"


if __name__ == "__main__":
    for fn in (test_normalize_tdx_and_qmt, test_qmt_requires_client_order_id,
               test_split_orders_keeps_channels, test_tdx_plan_dict_maps_price_type,
               test_qmt_params_maps_action_and_price_type, test_qmt_cancel_params):
        fn()
    print("✅ test_order_routing 全部通过")
