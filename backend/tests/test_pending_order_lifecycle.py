"""挂单链路终态持久化测试（v1 台账 sim_orders ↔ V2 投影 simulation_orders 双表一致）。

背景（2026-09-16 夜实测，108 次线上报错）：pending worker 从 V2 投影单构造的是
**内存态** SimOrder（_build_runtime_order），引擎终态写回（mark_rejected 等）在该对象上
commit 无物可落、db.refresh 抛 InvalidRequestError("not persistent")——worker 每轮崩、
订单永久卡 submitted、拒单原因丢失；且 v1 台账行（用户可见订单列表的来源）不被更新。
同族缺口：cancel_order / order_router / order_submission_service 改 v1 后不同步 V2 投影
（撤销的单会被 worker 重新拾取执行、路由单成交后 V2 仍 pending 可被重放）。

覆盖：
1. 单元：mark_rejected/mark_expired 的类型分派（V2 字符串状态 / v1 枚举）+ 瞬态不炸；
2. 真库：worker 拒单路径 → v1 + V2 双表 rejected + 拒因，run_once 不抛错；
3. 真库：worker 成交路径 → V2 镜像 filled；
4. 真库：cancel_order 镜像 V2 cancelled → worker 不再拾取已撤单；
5. 源守卫：在线/路由路径的终态镜像接线存在。
"""

from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

_BACKEND = Path(__file__).resolve().parents[1]
_TEST_TENANT_PREFIX = "t-pending-life"


# ── 纯函数/单元：类型分派 ────────────────────────────────────────────


class _TransientDB:
    """模拟瞬态对象的会话：commit 空转、refresh 抛 InvalidRequestError（与线上同形）。"""

    def __init__(self):
        self.commits = 0

    async def commit(self):
        self.commits += 1

    async def refresh(self, obj):
        from sqlalchemy.exc import InvalidRequestError

        raise InvalidRequestError(
            f"Instance '{type(obj).__name__}' is not persistent within this Session"
        )


def _v1_order():
    from backend.services.simulation.models.order import (
        OrderSide,
        OrderType,
        SimOrder,
    )

    return SimOrder(
        tenant_id="t-unit",
        user_id=1,
        symbol="600036.SH",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=100.0,
    )


def _v2_order():
    from backend.services.simulation.models.order_v2 import SimulationOrderV2

    return SimulationOrderV2(
        tenant_id="t-unit",
        user_id="1",
        account_id="sim:t-unit:1",
        symbol="600036.SH",
        side="buy",
        order_type="market",
        quantity=100.0,
        status="pending",
        trigger_source="manual",
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_mark_terminal_type_dispatch_and_transient_tolerance():
    from backend.services.simulation.models.order import OrderStatus
    from backend.services.simulation.services.execution_engine import (
        SimulationExecutionEngine,
    )

    # str-Enum 陷阱：OrderStatus 是 str 子类，isinstance(status, str) 恒真——
    # 类型分派必须按模型类型（SimulationOrderV2），不能按 status 的 isinstance。
    assert isinstance(OrderStatus.PENDING, str)

    engine = SimulationExecutionEngine(_TransientDB(), manager=None)

    # v1 瞬态：拒单不炸（线上崩溃点）
    v1 = _v1_order()
    await engine.mark_rejected(v1, "unit reject")
    assert v1.status == OrderStatus.REJECTED

    # v1 瞬态：过期降级 REJECTED（PG enum 有 expired 但 Python 枚举没有，
    # 写字符串 "expired" 会让该行以后 ORM 读取 LookupError）
    v1b = _v1_order()
    await engine.mark_expired(v1b, "unit expired")
    assert v1b.status == OrderStatus.REJECTED

    # V2 投影：字符串状态
    v2 = _v2_order()
    await engine.mark_rejected(v2, "unit reject")
    assert v2.status == "rejected"
    assert v2.rejected_reason == "unit reject"

    v2b = _v2_order()
    await engine.mark_expired(v2b, "unit expired")
    assert v2b.status == "expired"
    assert v2b.rejected_reason == "unit expired"


# ── 真库夹具 ────────────────────────────────────────────────────────


async def _ensure_db_pool():
    from sqlalchemy import text as _t

    from backend.shared.database_manager_v2 import close_database, get_session

    try:
        async with get_session(read_only=True) as probe:
            await probe.execute(_t("SELECT 1"))
        return
    except Exception:  # noqa: BLE001
        await close_database()
    async with get_session(read_only=True) as probe:
        await probe.execute(_t("SELECT 1"))


async def _cleanup_tenant(tenant: str):
    from sqlalchemy import text as _t

    from backend.shared.database_manager_v2 import get_session

    async with get_session(read_only=False) as session:
        for table in (
            "simulation_orders",
            "sim_orders",
            "sim_trades",
            "simulation_fills",
            "simulation_cash_ledger",
            "simulation_position_lots",
            "simulation_accounts",
        ):
            try:
                async with session.begin_nested():
                    await session.execute(
                        _t(f"DELETE FROM {table} WHERE tenant_id=:t"), {"t": tenant}
                    )
            except Exception:  # noqa: BLE001 - 表不存在则跳过
                continue
        await session.commit()


async def _assert_no_foreign_pending():
    """worker 扫描是全局的（无租户过滤）——真库夹具先确认无其它 pending 单，
    否则 monkeypatch 的 execute_order 会作用到别人的单上。"""
    from sqlalchemy import text as _t

    from backend.shared.database_manager_v2 import get_session

    async with get_session(read_only=True) as session:
        n = (
            await session.execute(
                _t("SELECT count(*) FROM simulation_orders WHERE status='pending'")
            )
        ).scalar_one()
    assert int(n) == 0, f"真库存在 {n} 条外部 pending 挂单，测试无法隔离，先清理/换环境"


async def _create_order(tenant: str, user: str, *, symbol: str = "600036.SH"):
    """走生产建档路径（v1 行 + V2 投影一次同步），返回 order_id。"""
    from backend.services.simulation.models.order import OrderSide, OrderType
    from backend.services.simulation.schemas.order import SimOrderCreate
    from backend.services.simulation.services.order_service import SimOrderService
    from backend.shared.database_manager_v2 import get_session

    async with get_session(read_only=False) as session:
        service = SimOrderService(session)
        order = await service.create_order(
            tenant,
            user,
            SimOrderCreate(
                symbol=symbol,
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                quantity=100.0,
            ),
        )
        await session.commit()
        return order.order_id


async def _read_both(order_id):
    from sqlalchemy import select

    from backend.services.simulation.models.order import SimOrder
    from backend.services.simulation.models.order_v2 import SimulationOrderV2
    from backend.shared.database_manager_v2 import get_session

    async with get_session(read_only=True) as session:
        v1 = (
            await session.execute(
                select(SimOrder).where(SimOrder.order_id == order_id)
            )
        ).scalar_one()
        v2 = (
            await session.execute(
                select(SimulationOrderV2).where(
                    SimulationOrderV2.order_id == order_id
                )
            )
        ).scalar_one()
    return v1, v2


# ── 真库：worker 拒单路径 ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pending_worker_reject_persists_both_tables(monkeypatch):
    await _ensure_db_pool()
    await _assert_no_foreign_pending()
    from backend.services.simulation.models.order import OrderStatus
    from backend.services.simulation.services.execution_engine import (
        ExecutionResult,
        SimulationExecutionEngine,
    )
    from backend.services.simulation.services.pending_order_worker import (
        SimulationPendingOrderWorker,
    )
    from backend.shared.database_manager_v2 import close_database

    tenant = f"{_TEST_TENANT_PREFIX}-rej-{uuid.uuid4().hex[:6]}"
    try:
        order_id = await _create_order(tenant, "31")

        async def _fake_exec(self, order, *a, **k):
            # 全部 pending 单（= 本测试唯一一条）确定性拒单，不触真账户
            return ExecutionResult(
                success=False,
                message="Quote stale for market order (strict)",
            )

        monkeypatch.setattr(SimulationExecutionEngine, "execute_order", _fake_exec)

        worker = SimulationPendingOrderWorker(interval_seconds=15, batch_size=50)
        processed = await worker.run_once()  # 修复前：InvalidRequestError 抛出
        assert processed >= 1

        v1, v2 = await _read_both(order_id)
        assert v1.status == OrderStatus.REJECTED
        assert v2.status == "rejected"
        assert "stale" in (v2.rejected_reason or "")
    finally:
        await _cleanup_tenant(tenant)
        await close_database()


# ── 真库：worker 成交路径 ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pending_worker_fill_mirrors_projection(monkeypatch):
    await _ensure_db_pool()
    await _assert_no_foreign_pending()
    from backend.services.simulation.models.order import OrderStatus
    from backend.services.simulation.services.execution_engine import (
        ExecutionResult,
        SimulationExecutionEngine,
    )
    from backend.services.simulation.services.pending_order_worker import (
        SimulationPendingOrderWorker,
    )
    from backend.shared.database_manager_v2 import close_database

    tenant = f"{_TEST_TENANT_PREFIX}-fill-{uuid.uuid4().hex[:6]}"
    try:
        order_id = await _create_order(tenant, "32")

        async def _fake_exec(self, order, *a, **k):
            return ExecutionResult(
                success=True,
                price=40.0,
                quantity=100.0,
                commission=5.0,
                market="CN",
                price_source="test",
            )

        async def _fake_apply(self, order, result):
            # 模拟真实 apply_filled 的落库语义（真实实现已由 ledger 金样测试覆盖）
            order.status = OrderStatus.FILLED
            order.filled_quantity = result.quantity
            await self.db.commit()
            return SimpleNamespace(trade_id=uuid.uuid4())

        monkeypatch.setattr(SimulationExecutionEngine, "execute_order", _fake_exec)
        monkeypatch.setattr(SimulationExecutionEngine, "apply_filled", _fake_apply)

        worker = SimulationPendingOrderWorker(interval_seconds=15, batch_size=50)
        assert await worker.run_once() >= 1

        v1, v2 = await _read_both(order_id)
        assert v1.status == OrderStatus.FILLED
        assert v2.status == "filled"
    finally:
        await _cleanup_tenant(tenant)
        await close_database()


# ── 真库：撤销镜像 + worker 不再拾取 ──────────────────────────────────


@pytest.mark.asyncio
async def test_cancel_mirrors_projection_and_worker_skips(monkeypatch):
    await _ensure_db_pool()
    await _assert_no_foreign_pending()
    from backend.services.simulation.models.order import OrderStatus, SimOrder
    from backend.services.simulation.services.execution_engine import (
        SimulationExecutionEngine,
    )
    from backend.services.simulation.services.order_service import SimOrderService
    from backend.services.simulation.services.pending_order_worker import (
        SimulationPendingOrderWorker,
    )
    from backend.shared.database_manager_v2 import close_database, get_session

    tenant = f"{_TEST_TENANT_PREFIX}-cancel-{uuid.uuid4().hex[:6]}"
    try:
        order_id = await _create_order(tenant, "33")

        async with get_session(read_only=False) as session:
            from sqlalchemy import select as _select

            orm = (
                await session.execute(
                    _select(SimOrder).where(SimOrder.order_id == order_id)
                )
            ).scalar_one()
            await SimOrderService(session).cancel_order(orm, "user cancelled")
            await session.commit()

        _, v2 = await _read_both(order_id)
        assert v2.status == "cancelled"  # 修复前：仍为 pending，worker 会重放

        async def _must_not_exec(self, order, *a, **k):
            raise AssertionError(f"worker 拾取了已撤销单: {order.order_id}")

        monkeypatch.setattr(
            SimulationExecutionEngine, "execute_order", _must_not_exec
        )
        worker = SimulationPendingOrderWorker(interval_seconds=15, batch_size=50)
        assert await worker.run_once() == 0

        v1, v2 = await _read_both(order_id)
        assert v1.status == OrderStatus.CANCELLED
        assert v2.status == "cancelled"
    finally:
        await _cleanup_tenant(tenant)
        await close_database()


# ── 源守卫：终态镜像接线 ─────────────────────────────────────────────


@pytest.mark.unit
def test_terminal_state_mirror_wiring_source_guards():
    worker_src = (
        _BACKEND / "services/simulation/services/pending_order_worker.py"
    ).read_text(encoding="utf-8")
    assert "load_runtime_order" in worker_src  # v1 行优先（终态写回 v1 生效）
    # 终态（过期/拒单/成交）均镜像 V2 投影
    assert worker_src.count("sync_order_projection") >= 4

    router_src = (
        _BACKEND / "services/simulation/services/order_router.py"
    ).read_text(encoding="utf-8")
    assert "sync_order_projection" in router_src

    sub_src = (
        _BACKEND / "services/simulation/services/order_submission_service.py"
    ).read_text(encoding="utf-8")
    assert sub_src.count("sync_order_projection") >= 4

    cancel_src = (
        _BACKEND / "services/simulation/services/order_service.py"
    ).read_text(encoding="utf-8")
    # cancel_order 内的镜像（同一函数体：CANCELLED 赋值后伴随 sync）
    assert "order.status = OrderStatus.CANCELLED" in cancel_src

    engine_src = (
        _BACKEND / "services/simulation/services/execution_engine.py"
    ).read_text(encoding="utf-8")
    # 类型分派按模型而不是 isinstance(status, str)
    assert "isinstance(order, SimulationOrderV2)" in engine_src
