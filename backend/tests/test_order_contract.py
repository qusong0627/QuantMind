"""T-P1-03 测试：Order/Fill 契约列 + 客户端幂等键。

覆盖：
1. build_sim_client_order_id 纯函数（确定性/缺参/截断）；
2. 列清单与迁移 SQL 幂等形态（两表）；
3. db_init.sql 同步（新装部署）；
4. 三个写入点接线源断言（engine/order_service/stream_consumer）；
5. 两个模型字段存在（SimOrder / trade Order）。
"""

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
    # orders 侧（带 UNIQUE 的原列）与 sim_orders 侧（新增列，无 UNIQUE）
    assert "client_order_id VARCHAR(100) UNIQUE" in ddl
    assert "client_order_id VARCHAR(100)," in ddl


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
