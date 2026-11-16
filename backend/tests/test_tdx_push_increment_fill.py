"""TDX 桥累计台阶增量 → ``trades`` 行 + 分账账本（细案乙2/乙3/乙4）。

背景（2026-10-14）：决策轮真单（``600338.SH`` buy 800 @ 11.83，
``orders.agent='glm-5.3-flash'``，``trading_mode=REAL``）成交后，
``tdx_push_service._sync_orders_to_pg`` 只刷 orders 表——``trades`` 面停在
09-24、``qm_agent_ledger_*`` 冻结在 10-09。本组用例钉住补上的四条纪律：

* **基线从 trades 读**：已有 ``filled_quantity=800`` 但零成交行的历史单，
  照样补出一行 ``cum-`` 成交（拿 orders 的前值当基线会永远算出 0 增量）；
* **幂等**：重投（桥 30s 重发同一回执 / poller 重启补拉）不新增行、不重记分账；
* **成交瞬时只取一次**：``trades.executed_at`` 与分账 ``filled_at`` 记同一瞬时
  （北京墙钟，由桥 ``time`` HHMM 派生；分账是 aware 列，交接前贴北京时区——
  naive 直传会被 ``as_utc`` 当成 UTC，显示偏 8 小时）；
* **无价不落行 / 无键不落行**（乙4：计数 + 告警，不静默）。

无库运行：``_FakeSession`` 只应答本链用到的五种 SQL。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from backend.services.live_trading.services import tdx_push_service as tps
from backend.services.trade_shared.models.enums import (
    OrderSide,
    TradeAction,
    TradingMode,
)
from backend.services.trade_shared.models.trade import Trade
from backend.shared.utc_datetime import as_utc

#: ``sync_account_to_pg`` 传入的 ``now``：naive UTC（北京 13:01 的 UTC 面）
NOW = datetime(2026, 10, 14, 5, 1, 23)

#: 桥报 HHMM 的墙钟时区（容器 TZ=Asia/Shanghai）；分账 filled_at 必须带它
CN_TZ = ZoneInfo("Asia/Shanghai")

_LOG = "backend.services.live_trading.services.tdx_push_service"


def _bridge_order(**over: Any) -> dict:
    """桥 ``/api/v1/orders/query`` 实测回执形状（2026-10-14 实探）。"""
    base: dict[str, Any] = {
        "order_id": "202617",
        "stock_code": "600338.SH",
        "time": "130123",
        "side": "buy",
        "status": "filled",
        "order_price": 11.96,
        "filled_price": 11.83,
        "filled_volume": 800,
        "total_volume": 800,
    }
    base.update(over)
    return base


def _preload_row(**over: Any) -> tuple:
    """``orders`` 预载行：与 select 列序一致。"""
    base: dict[str, Any] = {
        "exchange_order_id": "202617",
        "order_id": "b61d6445-7677-4249-82fb-0b09e6adce18",
        "status": "filled",
        "portfolio_id": 0,
        "filled_quantity": 800.0,
        "average_price": 11.83,
        "agent": "glm-5.3-flash",
    }
    base.update(over)
    return (
        base["exchange_order_id"],
        base["order_id"],
        base["status"],
        base["portfolio_id"],
        base["filled_quantity"],
        base["average_price"],
        base["agent"],
    )


class _FakeResult:
    def __init__(self, *, rows: tuple = (), scalar: Any = None) -> None:
        self._rows = list(rows)
        self._scalar = scalar

    def fetchall(self):
        return self._rows

    def scalar(self):
        return self._scalar

    def first(self):
        return self._rows[0] if self._rows else None


class _FakeSession:
    """只应答本链用到的五种 SQL：预载 / 基线 SUM / dedupe / UPDATE / INSERT。"""

    def __init__(
        self,
        *,
        preload: tuple = (),
        baseline: float = 0.0,
        duplicate_in_db: bool = False,
        new_order_id: str | None = None,
    ) -> None:
        self.preload = list(preload)
        self.baseline = baseline
        self.duplicate_in_db = duplicate_in_db
        self.new_order_id = new_order_id or str(uuid.uuid4())
        self.added: list[Any] = []

    async def execute(self, stmt, params=None):
        sql = str(stmt)
        if "sum(trades.quantity)" in sql:
            return _FakeResult(scalar=self.baseline)
        if "INSERT INTO orders" in sql:
            return _FakeResult(scalar=self.new_order_id)
        if "UPDATE orders" in sql:
            return _FakeResult()
        if "orders.exchange_order_id" in sql:
            return _FakeResult(rows=self.preload)
        if "trades.trade_id" in sql:
            return _FakeResult(rows=[(uuid.uuid4(),)] if self.duplicate_in_db else [])
        raise AssertionError(f"未预期的 SQL：{sql}")

    def add(self, obj) -> None:
        self.added.append(obj)


@pytest.fixture
def ledger_calls(monkeypatch) -> list[dict]:
    """假 ``post_fill_for_order``：记录调用参数（真契约由其自身测试覆盖）。"""
    calls: list[dict] = []

    async def _fake(
        session,
        *,
        order,
        fill_key,
        quantity,
        price,
        filled_at=None,
        trade_date=None,
    ):
        calls.append(
            {
                "order": order,
                "fill_key": fill_key,
                "quantity": quantity,
                "price": price,
                "filled_at": filled_at,
                "trade_date": trade_date,
            }
        )
        return None

    monkeypatch.setattr(tps, "post_fill_for_order", _fake)
    return calls


def _service(orders: list[dict]) -> tps.TdxPushService:
    svc = tps.TdxPushService()

    async def _pull():
        return list(orders)

    svc.pull_orders = _pull
    return svc


def _sync(svc, db):
    return asyncio.run(
        svc._sync_orders_to_pg(db=db, tenant_id="default", user_id="10000001", now=NOW)
    )


class _CaptureSession(_FakeSession):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.sqls: list[str] = []

    async def execute(self, stmt, params=None):
        self.sqls.append(str(stmt))
        return await super().execute(stmt, params)


def test_preload_is_scoped_to_current_trade_day(ledger_calls):
    """跨日同号委托不能被今日回报更新（2026-11-16 审计）。"""
    db = _CaptureSession(preload=[])
    _sync(_service([_bridge_order()]), db)
    preload = next(s for s in db.sqls if "orders.exchange_order_id" in s)
    assert "orders.created_at" in preload


# ---------------------------------------------------------------- 累计台阶增量


def test_historical_order_gets_backfilled_trade_and_ledger(ledger_calls):
    """验收形状：盘后重投 600338 已成交回执 → 补出 cum- 成交行 + 分账一笔。"""
    # Arrange：本地行已 filled_quantity=800，但 trades 面零 cum- 行（基线 0）
    db = _FakeSession(preload=[_preload_row()], baseline=0.0)
    svc = _service([_bridge_order()])

    # Act
    fills = _sync(svc, db)

    # Assert：一行成交，键=累计台阶，价=累计均价（首档无前值）
    assert len(db.added) == 1
    trade = db.added[0]
    assert isinstance(trade, Trade)
    assert trade.exchange_trade_id == "cum-20261014-202617-800"
    assert trade.order_id == "b61d6445-7677-4249-82fb-0b09e6adce18"
    assert trade.quantity == 800
    assert trade.price == pytest.approx(11.83)
    assert trade.trade_value == pytest.approx(9464.0)
    assert trade.symbol == "600338.SH"
    assert trade.side is OrderSide.BUY
    assert trade.trade_action is TradeAction.BUY_TO_OPEN
    assert trade.trading_mode is TradingMode.REAL
    assert trade.exchange_name == "tdx_bridge"
    # 成交瞬时 = 桥 time 130123 的北京墙钟（毫秒归零）
    assert trade.executed_at == datetime(2026, 10, 14, 13, 1, 0)
    assert trade.commission > 0

    # Assert：分账一笔，归属 glm-5.3-flash，与成交行同一个瞬时
    assert len(ledger_calls) == 1
    call = ledger_calls[0]
    assert call["fill_key"] == trade.exchange_trade_id
    assert call["quantity"] == 800
    assert call["price"] == pytest.approx(11.83)
    # 分账 filled_at 是 aware 列（as_utc：naive 一律当 UTC）：北京墙钟须贴好
    # 时区再交接，否则 13:01 成交落成 13:01Z、界面显示 21:01（2026-10-14 实况，
    # glm-5.3-flash 首笔补记踩中）。
    assert call["filled_at"] == datetime(2026, 10, 14, 13, 1, 0, tzinfo=CN_TZ)
    assert as_utc(call["filled_at"]) == datetime(
        2026, 10, 14, 5, 1, 0, tzinfo=timezone.utc
    )
    assert call["trade_date"] == trade.executed_at
    assert call["order"].agent == "glm-5.3-flash"
    assert call["order"].side == "buy"

    # 状态已是 filled 的老单：不重复当「首次成交通知」
    assert fills == []


def test_repoll_same_step_writes_nothing(ledger_calls):
    """桥 30s 重发同一回执 → 基线（trades 累计）与回报持平 ⇒ 无增量、零写入。"""
    db = _FakeSession(preload=[_preload_row()], baseline=800.0)
    svc = _service([_bridge_order()])

    assert _sync(svc, db) == []
    assert db.added == []
    assert ledger_calls == []


def test_incremental_step_uses_marginal_price(ledger_calls):
    """第二档：增量 500 股按「(累均×累量 − 前值)/增量」定价，对齐柜面金额。"""
    db = _FakeSession(
        preload=[
            _preload_row(status="partial", filled_quantity=300.0, average_price=11.9)
        ],
        baseline=300.0,
    )
    svc = _service([_bridge_order(time="130500")])

    _sync(svc, db)

    assert len(db.added) == 1
    trade = db.added[0]
    assert trade.quantity == 500
    # (11.83*800 − 11.9*300) / 500 = 5894/500
    assert trade.price == pytest.approx(11.788, rel=1e-9)
    assert trade.exchange_trade_id == "cum-20261014-202617-800"
    assert trade.executed_at == datetime(2026, 10, 14, 13, 5, 0)
    assert ledger_calls[0]["filled_at"] == datetime(
        2026, 10, 14, 13, 5, 0, tzinfo=CN_TZ
    )
    assert ledger_calls[0]["quantity"] == 500


def test_duplicate_key_already_in_trades_writes_nothing(ledger_calls):
    """键已在 trades（重投且基线查询未覆盖到）→ 先查后插拦下，不双记。"""
    db = _FakeSession(preload=[_preload_row()], baseline=0.0, duplicate_in_db=True)
    svc = _service([_bridge_order()])

    _sync(svc, db)

    assert db.added == []
    assert ledger_calls == []


def test_order_without_agent_still_records_trade(ledger_calls):
    """成交是账户级事实，照落 trades；归属为空由账本侧按纪律静默跳过。"""
    db = _FakeSession(preload=[_preload_row(agent=None)], baseline=0.0)
    svc = _service([_bridge_order()])

    _sync(svc, db)

    assert len(db.added) == 1
    assert ledger_calls[0]["order"].agent == ""


def test_new_bridge_order_fill_links_inserted_row(ledger_calls):
    """本地没有该委托：INSERT 拿到的新 order_id 必须直接成为成交行的外键。"""
    db = _FakeSession(preload=(), baseline=0.0)
    svc = _service([_bridge_order()])

    _sync(svc, db)

    assert len(db.added) == 1
    assert db.added[0].order_id == db.new_order_id
    assert ledger_calls[0]["order"].order_id == db.new_order_id


# ---------------------------------------------------------------- 乙4：不静默


def test_unpriceable_step_is_counted_and_warned(ledger_calls, caplog):
    """市价单成交价缺失（累均 0 且无限价可退）→ 不落 0 元成交，但每轮告警。"""
    db = _FakeSession(preload=[_preload_row(average_price=None)], baseline=0.0)
    svc = _service([_bridge_order(order_price=0, filled_price=None)])

    with caplog.at_level(logging.WARNING, logger=_LOG):
        _sync(svc, db)

    assert db.added == []
    assert ledger_calls == []
    assert any("取不到价 1 笔" in r.message for r in caplog.records)


# ---------------------------------------------------------------- 边际价纯函数


def test_marginal_price_first_step_equals_cumulative_average():
    assert tps._marginal_fill_price(
        delta_qty=800,
        prior_qty=0.0,
        prior_avg=None,
        cum_qty=800,
        cum_avg=11.83,
        fallback_price=0.0,
    ) == pytest.approx(11.83)


def test_marginal_price_degrades_to_fallback_then_zero():
    # 累均坏但有限价 → 用限价
    assert tps._marginal_fill_price(
        delta_qty=100,
        prior_qty=0.0,
        prior_avg=None,
        cum_qty=100,
        cum_avg=0.0,
        fallback_price=10.0,
    ) == pytest.approx(10.0)
    # 全坏 → 0（调用方不落该行）
    assert (
        tps._marginal_fill_price(
            delta_qty=100,
            prior_qty=0.0,
            prior_avg=None,
            cum_qty=100,
            cum_avg=0.0,
            fallback_price=0.0,
        )
        == 0.0
    )
    # 无增量 → 0
    assert (
        tps._marginal_fill_price(
            delta_qty=0,
            prior_qty=100.0,
            prior_avg=10.0,
            cum_qty=100,
            cum_avg=10.0,
            fallback_price=10.0,
        )
        == 0.0
    )
