"""内部策略调度 REAL 分支回归：user_id 口径 + 无组合兜底。

背景（2026-09-10 真机 E2E 暴露）：``dispatch_internal_strategy_order`` 把
user_id 转成 int 后直接用于 DB 查询，而 ``orders.user_id`` / ``portfolios.user_id``
是 VARCHAR（库里存 8 位补零字符串）：

1. ``Portfolio.user_id == <int>`` → asyncpg 报
   ``operator does not exist: character varying = integer``；
2. ``create_order(user_id=<int>)`` → asyncpg 报 invalid input（expected str）；
3. ``"1"`` 与库里的 ``"00000001"`` 对不上，幂等查询（client_order_id 去重）永远 miss；
4. 无 portfolios 行时直接 400 ``no active portfolio available``，镜像真单永远发不出去。

用例全部注入假体，不依赖真库。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

from backend.services.live_trading.routers.real_trading_utils import normalize_db_user_id
from backend.services.live_trading.services import internal_strategy_dispatcher as d


class FakeResult:
    """仿 SQLAlchemy Result：只实现调度器用到的取值接口。"""

    def __init__(self, value: Any = None):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class FakeDb:
    """记录每次 execute 的语句（可编译取绑定参数），其余落库动作全部空转。"""

    def __init__(self, results: list[Any] | None = None):
        self.statements: list[Any] = []
        self._results = list(results or [])

    async def execute(self, stmt):
        self.statements.append(stmt)
        return FakeResult(self._results.pop(0) if self._results else None)

    def add(self, obj):  # pragma: no cover - 假体空转
        return None

    async def commit(self):  # pragma: no cover - 假体空转
        return None

    async def refresh(self, obj):  # pragma: no cover - 假体空转
        return None


class FakeOrderService:
    """捕获 create_order 实参。"""

    def __init__(self, db, redis):
        self.calls: list[dict[str, Any]] = []

    async def create_order(self, *, user_id, tenant_id, order_data):
        self.calls.append(
            {"user_id": user_id, "tenant_id": tenant_id, "order_data": order_data}
        )
        return SimpleNamespace(order_id="fake-order-id")


class FakeEngine:
    """捕获 check_order_risk / submit_order 实参。"""

    def __init__(self, db, redis):
        self.risk_user_ids: list[Any] = []

    async def check_order_risk(self, user_id, order):
        self.risk_user_ids.append(user_id)
        return {"passed": True}

    async def submit_order(self, order, tenant_id=None):
        return {"success": True, "message": "fake submit"}


def _run(coro):
    return asyncio.run(coro)


def _bind_params(stmt) -> dict[str, Any]:
    return stmt.compile().params


def test_normalize_db_user_id_forms():
    assert normalize_db_user_id("1") == "00000001"
    assert normalize_db_user_id(1) == "00000001"
    assert normalize_db_user_id("00000001") == "00000001"
    assert normalize_db_user_id("admin") == "admin"
    assert normalize_db_user_id(None) == ""


def test_real_order_with_int_user_id_and_no_portfolio_succeeds():
    """E2E 回归：user_id=1（int 口径）+ 无 portfolios 行 → 按 0 组合落账，不 500。"""
    db = FakeDb(results=[None, None])  # portfolio 快照/兜底查询、幂等查询都给空
    redis = SimpleNamespace()

    captured: dict[str, Any] = {}

    def _fake_order_service(db_arg, redis_arg):
        svc = FakeOrderService(db_arg, redis_arg)
        captured["svc"] = svc
        return svc

    def _fake_engine(db_arg, redis_arg):
        eng = FakeEngine(db_arg, redis_arg)
        captured["eng"] = eng
        return eng

    with patch.object(d, "_fetch_active_portfolio_snapshot", AsyncMock(return_value=None)), \
            patch.object(d, "OrderService", _fake_order_service), \
            patch.object(d, "TradingEngine", _fake_engine):
        result = _run(
            d.dispatch_internal_strategy_order(
                order_data={
                    "symbol": "600036.SH",
                    "side": "BUY",
                    "quantity": 100,
                    "price": 41.0,
                    "order_type": "LIMIT",
                    "trading_mode": "REAL",
                    "client_order_id": "mir-test-1",
                },
                user_id="1",
                tenant_id="default",
                redis=redis,
                db=db,
            )
        )

    assert result["status"] == "success"
    assert result["execution"] == "direct"

    call = captured["svc"].calls[0]
    # 落库口径必须是 8 位字符串（int 会被 asyncpg 拒收 / 与库里行对不上）
    assert call["user_id"] == "00000001"
    assert call["order_data"].portfolio_id == 0

    # 所有面向 DB 的 user_id 绑定参数都必须是字符串，不能再出现 int
    for stmt in db.statements:
        for key, value in _bind_params(stmt).items():
            if key.startswith("user_id"):
                assert isinstance(value, str), f"int user_id 绑定参数: {_bind_params(stmt)}"

    # 风控仍用 int 口径（保留既有规则匹配语义）
    assert captured["eng"].risk_user_ids == [1]


def test_real_order_duplicate_client_order_id_is_skipped():
    """幂等：同 client_order_id 已存在真单时必须命中（user_id 口径不能带偏）。"""
    existing = SimpleNamespace(order_id="existing-order-id")
    db = FakeDb(results=[None, existing])  # ①组合兜底查询空 ②幂等查询命中
    redis = SimpleNamespace()

    with patch.object(d, "_fetch_active_portfolio_snapshot", AsyncMock(return_value=None)), \
            patch.object(d, "OrderService", FakeOrderService), \
            patch.object(d, "TradingEngine", FakeEngine):
        result = _run(
            d.dispatch_internal_strategy_order(
                order_data={
                    "symbol": "600036.SH",
                    "side": "BUY",
                    "quantity": 100,
                    "price": 41.0,
                    "order_type": "LIMIT",
                    "trading_mode": "REAL",
                    "client_order_id": "mir-dup-1",
                },
                user_id="00000001",
                tenant_id="default",
                redis=redis,
                db=db,
            )
        )

    assert result["execution"] == "duplicate_skipped"
    assert result["order_id"] == "existing-order-id"

    # 幂等查询按 8 位口径绑定
    dup_params = _bind_params(db.statements[-1])
    assert "00000001" in dup_params.values()


def test_real_order_uses_portfolio_from_order_data():
    """order_data 显式带 portfolio_id 时优先使用，不再兜底 0。"""
    db = FakeDb(results=[None])
    redis = SimpleNamespace()
    captured: dict[str, Any] = {}

    def _fake_order_service(db_arg, redis_arg):
        svc = FakeOrderService(db_arg, redis_arg)
        captured["svc"] = svc
        return svc

    with patch.object(d, "_fetch_active_portfolio_snapshot", AsyncMock(return_value=None)), \
            patch.object(d, "OrderService", _fake_order_service), \
            patch.object(d, "TradingEngine", FakeEngine):
        result = _run(
            d.dispatch_internal_strategy_order(
                order_data={
                    "symbol": "600036.SH",
                    "side": "BUY",
                    "quantity": 100,
                    "price": 41.0,
                    "order_type": "LIMIT",
                    "trading_mode": "REAL",
                    "portfolio_id": 7,
                },
                user_id="00000001",
                tenant_id="default",
                redis=redis,
                db=db,
            )
        )

    assert result["status"] == "success"
    assert captured["svc"].calls[0]["order_data"].portfolio_id == 7
