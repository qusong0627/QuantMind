"""T-P1-03 测试：Order/Fill 契约列 + 客户端幂等键。

覆盖：
1. build_sim_client_order_id 纯函数（确定性/缺参/截断）；
2. 列清单与迁移 SQL 幂等形态（两表）；
3. db_init.sql 同步（新装部署）；
4. 三个写入点接线源断言（engine/order_service/stream_consumer）；
5. 两个模型字段存在（SimOrder / trade Order）。
"""

import pytest
from pathlib import Path

from backend.shared.order_contract import (
    MAX_CLIENT_ORDER_ID_LEN,
    ORDER_COLUMNS,
    SIM_ORDER_COLUMNS,
    SOURCE_REBALANCE,
    _missing_for,
    build_sim_client_order_id,
)

_BACKEND = Path(__file__).resolve().parents[1]


def test_build_sim_client_order_id():
    assert (
        build_sim_client_order_id("run_20260914_8933f9af", "600036.SH", "BUY")
        == "sim-run_20260914_8933f9af-600036.SH-buy"
    )
    # 确定性：同输入同键（重跑可观测/去重基础）
    assert build_sim_client_order_id("r1", "000001.SZ", "SELL") == build_sim_client_order_id(
        "r1", "000001.SZ", "sell"
    )
    # 缺参不强造
    assert build_sim_client_order_id("", "000001.SZ", "BUY") is None
    assert build_sim_client_order_id("r1", "", "BUY") is None
    assert build_sim_client_order_id("r1", "000001.SZ", "") is None
    # 截断不超过列宽
    long_id = build_sim_client_order_id("r" * 200, "s" * 60, "buy")
    assert long_id is not None and len(long_id) <= MAX_CLIENT_ORDER_ID_LEN


def test_column_lists():
    assert {n for n, _ in SIM_ORDER_COLUMNS} == {"client_order_id", "source"}
    assert {n for n, _ in ORDER_COLUMNS} == {"price_source", "source"}


def test_missing_columns_logic():
    """安全化自愈：列齐全 → 零 DDL；缺列 → 只列缺口（2026-09-16 锁事故后重构）。"""
    assert _missing_for("sim_orders", {"client_order_id", "source"}) == []
    gaps = _missing_for("sim_orders", {"source"})
    assert gaps == [("client_order_id", "VARCHAR(100)")]
    assert _missing_for("orders", set()) == list(ORDER_COLUMNS)


def test_ensure_is_lock_safe():
    """自愈迁移必须 ① existence 预检（information_schema）② lock_timeout ③ 不抛出。"""
    for name in ("signal_contract.py", "order_contract.py"):
        src = (_BACKEND / "shared" / name).read_text(encoding="utf-8")
        assert "information_schema.columns" in src, name
        assert "lock_timeout" in src, name
        assert "不阻断业务" in src, name


def test_db_init_synced():
    ddl = (_BACKEND / "shared/db_init.sql").read_text(encoding="utf-8")
    assert "T-P1-03 Order 契约列" in ddl
    # orders 侧（带 UNIQUE 的原列）与 sim_orders 侧（列 + T-P2-08 部分唯一索引）
    assert "client_order_id VARCHAR(100) UNIQUE" in ddl
    assert "client_order_id VARCHAR(100)," in ddl
    assert "uq_sim_orders_scope_client_order_id" in ddl
    assert "WHERE client_order_id IS NOT NULL" in ddl


def test_engine_writer_wired():
    """T-P2-01 后：引擎改经 OrderRouter；幂等键合成与封列自愈分别在引擎/Router。"""
    engine_src = (_BACKEND / "services/simulation/engine.py").read_text(encoding="utf-8")
    assert "build_sim_client_order_id(" in engine_src
    assert "SOURCE_REBALANCE" in engine_src
    assert "order_router import" in engine_src

    router_src = (
        _BACKEND / "services/simulation/services/order_router.py"
    ).read_text(encoding="utf-8")
    assert "ensure_order_contract_columns_async()" in router_src
    assert "trigger_source=req.source" in router_src


def test_order_service_writer_wired():
    src = (_BACKEND / "services/simulation/services/order_service.py").read_text(
        encoding="utf-8"
    )
    assert "order.client_order_id = client_order_id" in src
    assert "SOURCE_MANUAL" in src
    assert "ensure_order_contract_columns_async()" in src


def test_stream_consumer_writer_wired():
    src = (_BACKEND / "services/trade/services/execution_stream_consumer.py").read_text(
        encoding="utf-8"
    )
    assert "PRICE_SOURCE_BROKER_FILL" in src
    assert "ensure_order_contract_columns_async()" in src


def test_models_have_contract_fields():
    from backend.services.simulation.models.order import SimOrder
    from backend.services.trade_shared.models.order import Order

    assert hasattr(SimOrder, "client_order_id") and hasattr(SimOrder, "source")
    assert hasattr(Order, "price_source") and hasattr(Order, "source")


def test_source_taxonomy_constants():
    from backend.shared import order_contract as oc

    assert {
        oc.SOURCE_REBALANCE,
        oc.SOURCE_MANUAL,
        oc.SOURCE_INTERNAL,
        oc.SOURCE_MIRROR,
        oc.SOURCE_SLTP,
        oc.SOURCE_SANDBOX,
        oc.SOURCE_TDX_ROLLING,
        oc.SOURCE_HOSTED,
        oc.SOURCE_FORCED_LIQUIDATION,
    } == {
        "rebalance",
        "manual",
        "internal",
        "mirror",
        "sltp",
        "sandbox",
        "tdx_rolling",
        "hosted",
        "forced_liquidation",
    }
    assert SOURCE_REBALANCE == "rebalance"


# ── T-P2-08：幂等键唯一索引（部分索引）+ 重复语义 ─────────────────────


def test_unique_index_migration_source_guards():
    """唯一索引迁移三纪律 + 存量重复前置（有重复不建索引、不静默删行）。"""
    src = (_BACKEND / "shared/order_contract.py").read_text(encoding="utf-8")
    assert "SIM_ORDER_UNIQUE_INDEX" in src
    assert "WHERE client_order_id IS NOT NULL" in src
    assert "HAVING count(*) > 1" in src  # 存量重复预检
    assert "lock_timeout" in src and "不阻断" in src
    assert "repair_sim_order_duplicates" in src  # 修复脚本指路

    # 写入侧：create_order 必须把 IntegrityError 转 DuplicateSimOrderError（不得 500）
    svc = (
        _BACKEND / "services/simulation/services/order_service.py"
    ).read_text(encoding="utf-8")
    assert "class DuplicateSimOrderError" in svc
    assert "except IntegrityError" in svc
    assert "get_sim_order_by_client_order_id" in svc

    # 三条调用方都要转既有 duplicate 语义
    router = (
        _BACKEND / "services/simulation/services/order_router.py"
    ).read_text(encoding="utf-8")
    assert "except DuplicateSimOrderError" in router
    assert "get_sim_order_by_client_order_id" in router  # 台账本体先查（投影可能为空）

    submission = (
        _BACKEND / "services/simulation/services/order_submission_service.py"
    ).read_text(encoding="utf-8")
    assert "except DuplicateSimOrderError" in submission
    assert "get_sim_order_by_client_order_id" in submission

    route = (
        _BACKEND / "services/simulation/routers/simulation_orders.py"
    ).read_text(encoding="utf-8")
    assert "except DuplicateSimOrderError" in route

    # 调度器幂等判定改用 cid 列（remarks 前缀被 mark_rejected 覆写的历史坑）
    dispatcher = (
        _BACKEND / "services/live_trading/services/internal_strategy_dispatcher.py"
    ).read_text(encoding="utf-8")
    assert "SimOrder.client_order_id == client_order_id" in dispatcher


async def _ensure_db_pool_tp208():
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


@pytest.mark.asyncio
async def test_unique_index_real_db_and_duplicate_create_e2e():
    """真库 E2E：索引建成（带 WHERE）→ 同幂等键二次 create_order →
    DuplicateSimOrderError（非 500）且库内仍单行 → 清理。"""
    import uuid as _uuid

    for attempt in range(2):
        try:
            await _ensure_db_pool_tp208()
            break
        except Exception:  # noqa: BLE001
            if attempt == 1:
                pytest.skip("数据库不可用")
    from sqlalchemy import text as sa_text

    from backend.services.simulation.models.order import OrderSide, OrderType
    from backend.services.simulation.schemas.order import SimOrderCreate
    from backend.services.simulation.services.order_service import (
        DuplicateSimOrderError,
        SimOrderService,
    )
    from backend.shared.database_manager_v2 import close_database, get_session
    from backend.shared.order_contract import ensure_sim_order_unique_index_async

    assert await ensure_sim_order_unique_index_async() is True
    async with get_session(read_only=True) as session:
        row = (
            await session.execute(
                sa_text(
                    "SELECT indexdef FROM pg_indexes "
                    "WHERE indexname = 'uq_sim_orders_scope_client_order_id'"
                )
            )
        ).fetchone()
    assert row is not None
    assert "WHERE (client_order_id IS NOT NULL)" in str(row[0])

    user = f"99{_uuid.uuid4().int % 1_000_000:06d}"
    cid = f"sim-e2e-{_uuid.uuid4().hex[:8]}-600036.SH-buy"
    try:
        async with get_session(read_only=False) as session:
            svc = SimOrderService(session)
            first = await svc.create_order(
                "default",
                user,
                SimOrderCreate(
                    portfolio_id=0,
                    client_order_id=cid,
                    symbol="600036.SH",
                    side=OrderSide("buy"),
                    order_type=OrderType("market"),
                    quantity=100.0,
                    price=40.0,
                    remarks="T-P2-08 E2E",
                ),
                trigger_source="manual",
            )
            assert first.client_order_id == cid
        async with get_session(read_only=False) as session:
            svc2 = SimOrderService(session)
            with pytest.raises(DuplicateSimOrderError) as exc:
                await svc2.create_order(
                    "default",
                    user,
                    SimOrderCreate(
                        portfolio_id=0,
                        client_order_id=cid,
                        symbol="600036.SH",
                        side=OrderSide("buy"),
                        order_type=OrderType("market"),
                        quantity=100.0,
                        price=40.0,
                        remarks="T-P2-08 E2E dup",
                    ),
                    trigger_source="manual",
                )
            assert exc.value.client_order_id == cid
            assert str(exc.value.existing.order_id) == str(first.order_id)
        # 库内仍单行（唯一索引兜底生效）
        async with get_session(read_only=True) as session:
            n = (
                await session.execute(
                    sa_text(
                        "SELECT count(*) FROM sim_orders "
                        "WHERE tenant_id='default' AND user_id=:u AND client_order_id=:c"
                    ),
                    {"u": int(user), "c": cid},
                )
            ).scalar_one()
        assert int(n) == 1
    finally:
        async with get_session(read_only=False) as session:
            await session.execute(
                sa_text(
                    "DELETE FROM sim_orders WHERE tenant_id='default' AND client_order_id=:c"
                ),
                {"c": cid},
            )
        await close_database()
