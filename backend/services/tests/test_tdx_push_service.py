"""tdx_push_service._sync_orders_to_pg 测试: 桥当日委托 → orders 表 UPSERT。

修复背景: 原实现只 INSERT + 按 exchange_order_id 去重跳过, 已存在的订单
状态永不更新, 全部停留在 SUBMITTED 后被超时扫描器误标 EXPIRED,
表现为交易记录"全部过期、成交为 0"。现在已存在行用桥最新状态/成交回报刷新。
"""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.sql.elements import TextClause
from sqlalchemy.sql.selectable import Select

from backend.services.live_trading.services.tdx_push_service import (
    TdxPushService,
    estimate_order_fee,
)


class _RowsResult:
    """模拟 execute 返回: fetchall() 给 SELECT, scalar() 给 RETURNING。"""

    def __init__(self, rows):
        self._rows = list(rows)

    def fetchall(self):
        return self._rows

    def scalar(self):
        return self._rows[0] if self._rows else None


class _FakeDb:
    """迷你内存库: 记住 existing 映射, UPDATE/INSERT 记录参数。"""

    def __init__(self, existing=None):
        # {exchange_order_id: order_id}
        self.existing = dict(existing or {})
        self.updates: list[dict] = []
        self.inserts: list[dict] = []

    async def execute(self, stmt, params=None):
        if isinstance(stmt, Select):
            return _RowsResult(
                [(eid, oid) for eid, oid in self.existing.items()]
            )
        if not isinstance(stmt, TextClause):
            return _RowsResult([])
        sql = str(stmt).strip().upper()
        if sql.startswith("UPDATE"):
            self.updates.append(params or {})
            return _RowsResult([])
        if sql.startswith("INSERT"):
            params = params or {}
            new_id = str(uuid4())
            self.inserts.append(params)
            self.existing[params["exchange_order_id"]] = new_id
            return _RowsResult([new_id])
        return _RowsResult([])


def _svc_with(pull_orders_result):
    svc = TdxPushService()

    async def _pull_orders(stock_code: str = ""):
        return list(pull_orders_result)

    svc.pull_orders = _pull_orders
    return svc


_BRIDGE_ORDER = {
    "order_id": "160356",
    "stock_code": "SH600206",
    "time": "093000",
    "side": "buy",
    "status": "filled",
    "order_price": 50.78,
    "filled_price": 50.90,
    "filled_volume": 2400,
    "total_volume": 2400,
}


async def _run_sync(db, orders, user_id="1001"):
    svc = _svc_with(orders)
    await svc._sync_orders_to_pg(
        db=db, tenant_id="default", user_id=user_id, now=datetime(2026, 8, 25, 10, 0, 0)
    )
    return svc


class TestEstimateOrderFee:
    """费用估算: 佣金(万2.5 最低5元, 双边) + 印花税(万5, 仅卖出) + 过户费(万0.1, 双边)。"""

    def test_buy_fee_commission_plus_transfer(self):
        assert estimate_order_fee(100000, "buy") == 26.0  # 25 佣金 + 1 过户

    def test_sell_fee_adds_stamp_tax(self):
        assert estimate_order_fee(100000, "sell") == 76.0  # + 50 印花税

    def test_min_commission_applies(self):
        assert estimate_order_fee(10000, "buy") == 5.1  # 佣金按最低 5 元

    def test_zero_filled_value_no_fee(self):
        assert estimate_order_fee(0, "buy") == 0.0
        assert estimate_order_fee(0, "sell") == 0.0


@pytest.mark.asyncio
async def test_sync_inserts_new_bridge_order():
    db = _FakeDb()
    await _run_sync(db, [_BRIDGE_ORDER])

    assert len(db.inserts) == 1
    row = db.inserts[0]
    assert row["exchange_order_id"] == "160356"
    assert row["status"] == "filled"
    assert row["filled_quantity"] == 2400
    assert row["average_price"] == 50.90
    assert row["filled_value"] == round(2400 * 50.90, 2)
    # 122160 × 0.00025 = 30.54 佣金 + 1.2216 过户费 = 31.76
    assert row["commission"] == 31.76
    assert row["trading_mode"] == "REAL"
    assert row["submitted_at"].hour == 9 and row["submitted_at"].minute == 30
    assert row["filled_at"] == row["submitted_at"]


@pytest.mark.asyncio
async def test_sync_updates_existing_order_with_latest_fill():
    db = _FakeDb(existing={"160356": "pg-order-1"})
    # 桥: 已从 pending 变成 filled
    order = {**_BRIDGE_ORDER}
    await _run_sync(db, [order])

    assert db.inserts == []
    assert len(db.updates) == 1
    upd = db.updates[0]
    assert upd["order_id"] == "pg-order-1"
    assert upd["status"] == "filled"
    assert upd["filled_quantity"] == 2400
    assert upd["average_price"] == 50.90
    assert upd["filled_value"] == round(2400 * 50.90, 2)
    assert upd["commission"] == 31.76
    assert upd["filled_at"] is not None


@pytest.mark.asyncio
async def test_sync_maps_partial_fill_rejected_cancelled():
    db = _FakeDb()
    orders = [
        {
            "order_id": "1001",
            "stock_code": "SH688999",
            "time": "123456",
            "side": "sell",
            "status": "partial_fill",
            "order_price": 10.0,
            "filled_price": 10.05,
            "filled_volume": 300,
            "total_volume": 500,
        },
        {
            "order_id": "1002",
            "stock_code": "SH600000",
            "time": "130000",
            "side": "buy",
            "status": "rejected",
            "order_price": 11.0,
            "filled_price": 0,
            "filled_volume": 0,
            "total_volume": 100,
        },
        {
            "order_id": "1003",
            "stock_code": "SH601000",
            "time": "131500",
            "side": "buy",
            "status": "partial_cancelled",
            "order_price": 12.0,
            "filled_price": 12.1,
            "filled_volume": 200,
            "total_volume": 500,
        },
    ]
    await _run_sync(db, orders)

    by_id = {r["exchange_order_id"]: r for r in db.inserts}
    assert by_id["1001"]["status"] == "partially_filled"
    # 卖出 300×10.05=3015: 佣金最低5 + 印花1.5075 + 过户0.0302 = 6.54
    assert by_id["1001"]["commission"] == 6.54
    assert by_id["1002"]["status"] == "rejected"
    assert by_id["1002"]["commission"] == 0.0  # 未成交无费用
    # 部分撤单: 终态为撤单, 但保留已成交数量/均价
    assert by_id["1003"]["status"] == "cancelled"
    assert by_id["1003"]["filled_quantity"] == 200
    assert by_id["1003"]["average_price"] == 12.1


@pytest.mark.asyncio
async def test_sync_skips_order_without_exchange_id_and_symbol():
    db = _FakeDb()
    orders = [
        {"order_id": "", "stock_code": "SH600000", "status": "filled"},
        {"order_id": "9999", "stock_code": "", "status": "filled"},
    ]
    await _run_sync(db, orders)

    assert db.inserts == []
    assert db.updates == []


# ============ 真单时段闸门（咽喉点） ============

class TestRealOrderSessionGate:
    """``place_order`` 是**真单唯一的物理出口**（滚动单、L2 主单、L2 在途重挂
    三条路都汇到这里）。闸门放这一层而不是各调用点：调用点漂移一次就是一次
    真钱事故——A 股委托在盘外要么被柜台拒、要么被客户端挂成次日单。

    时段事实一律**注入**（``is_trading_time``），不读墙上钟：否则同样的用例
    白天绿、收盘后红。
    """

    @staticmethod
    def _wire(monkeypatch, *, in_session: bool) -> list[tuple[str, dict]]:
        from backend.services.live_trading.services import tdx_push_service as push_mod

        monkeypatch.setattr(push_mod, "is_trading_time", lambda now=None: in_session)
        sent: list[tuple[str, dict]] = []

        async def _fake_post(path: str, payload: dict) -> dict:
            sent.append((path, payload))
            return {"status": "executed", "orders": []}

        monkeypatch.setattr(push_mod.tdx_pusher, "_post", _fake_post)
        return sent

    @pytest.mark.asyncio
    async def test_out_of_session_real_order_never_reaches_the_bridge(self, monkeypatch):
        from backend.services.live_trading.services.tdx_push_service import tdx_pusher

        sent = self._wire(monkeypatch, in_session=False)

        resp = await tdx_pusher.place_order(
            stock_code="600036.SH", side="sell", volume=100, price=12.5
        )

        assert sent == [], "盘外的真单到了桥上——客户端会把它挂成次日单"
        assert resp.get("skipped") == "out_of_session"
        assert resp.get("orders") == []
        # 形状必须是"失败"：滚动/L2 都按 status 分拣 placed/failed，
        # 报成成功会让上游记一条不存在的委托
        assert resp.get("status") != "submitted"
        assert resp.get("status") == "error"

    @pytest.mark.asyncio
    async def test_in_session_the_same_call_goes_through(self, monkeypatch):
        """对照组：闸门不是"恒拒发"（那会让上面那条用例空过）。"""
        from backend.services.live_trading.services.tdx_push_service import tdx_pusher

        sent = self._wire(monkeypatch, in_session=True)

        await tdx_pusher.place_order(
            stock_code="600036.SH", side="sell", volume=100, price=12.5
        )

        assert len(sent) == 1
        path, payload = sent[0]
        assert path.endswith("/api/v1/plans/execute")
        assert payload["orders"][0]["stock_code"] == "600036.SH"

    @pytest.mark.asyncio
    async def test_cancel_is_allowed_out_of_session(self, monkeypatch):
        """撤单不走这道闸：它是**减小风险**的动作，任何时间都该放行。"""
        from backend.services.live_trading.services.tdx_push_service import tdx_pusher

        sent = self._wire(monkeypatch, in_session=False)

        await tdx_pusher.cancel_order(stock_code="600036.SH", order_id="Wtbh-1")

        assert len(sent) == 1, "盘外撤单被闸住了——风险敞口会挂到下一个交易日"
        assert sent[0][0].endswith("/api/v1/orders/cancel")

    def test_the_gate_uses_the_shared_session_predicate(self):
        """窗口口径唯一：不许在这里（或任何调用点）另写一份时间判断。

        另写一份的形态就是"看着差不多"的比小时数——两个口径在节假日、
        集合竞价、尾盘缓冲上必然分叉，而分叉的方向是「多发了真单」。
        """
        from pathlib import Path

        src = (
            Path(__file__).resolve().parents[1]
            / "live_trading/services/tdx_push_service.py"
        ).read_text(encoding="utf-8")
        assert "from backend.services.live_trading.services.trading_session import" in src, (
            "place_order 的时段闸门没走 trading_session 唯一口径"
        )
        assert "is_trading_time()" in src, "闸门没读时段谓词——它就不会拦任何东西"
