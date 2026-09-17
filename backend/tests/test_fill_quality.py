"""F2 保真度与影子 F2 维度测试（T-P6-18/19）：口径纯函数金样 + 交易台块真机。"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.unit
_CST = timezone(timedelta(hours=8))


async def _ensure_fresh_db_pool() -> None:
    """批跑防坑：前序测试的 asyncio.run 会把 asyncpg 连接绑死在已关闭的 loop 上——
    刷新池（与 test_hot_set_builder._ensure_db_pool 同范式）。"""
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



# ── T-P6-18 口径 ────────────────────────────────────────────────────


def test_fidelity_metrics_direction_and_rates():
    from backend.shared.fill_quality import fidelity_metrics

    rows = [
        # 买入成交价 10.10 > 收盘 10.00 → 成本 +100bps
        {"symbol": "600036.SH", "side": "buy", "quantity": 1000, "filled_quantity": 1000,
         "fill_price": 10.10, "status": "filled", "execution_model": "snapshot_core"},
        # 卖出成交价 9.90 < 收盘 10.00 → 成本 +100bps（少收）
        {"symbol": "600036.SH", "side": "sell", "quantity": 1000, "filled_quantity": 1000,
         "fill_price": 9.90, "status": "filled", "execution_model": "synthetic_price"},
        # 部分成交：委托 1000 成交 500
        {"symbol": "000001.SZ", "side": "buy", "quantity": 1000, "filled_quantity": 500,
         "fill_price": 12.00, "status": "pending", "execution_model": "synthetic_price"},
        # 未成交
        {"symbol": "000001.SZ", "side": "buy", "quantity": 100, "filled_quantity": 0,
         "fill_price": None, "status": "rejected", "execution_model": "synthetic_price"},
        # 无参考价（missing_ref）
        {"symbol": "999999.SZ", "side": "buy", "quantity": 100, "filled_quantity": 100,
         "fill_price": 5.00, "status": "filled", "execution_model": "synthetic_price"},
    ]
    out = fidelity_metrics(rows, reference_prices={"600036.SH": 10.0, "000001.SZ": 12.0},
                           configured_slippage_bps=5.0)
    assert out["orders"] == 5 and out["filled_orders"] == 4
    assert out["fill_rate"] == pytest.approx(0.8)
    assert out["partial_ratio"] == pytest.approx(1 / 4)
    dev = out["price_deviation_bps"]
    assert dev["n"] == 3 and dev["missing_ref"] == 1
    assert dev["mean"] == pytest.approx((100 + 100 + 0) / 3, abs=0.5)
    # 滑点实现 = 中位数 − 配置（中位 100bps − 5bps = 95）
    assert out["slippage_realized_bps"] == pytest.approx(95.0, abs=0.5)
    assert out["by_execution_model"] == {"snapshot_core": 1, "synthetic_price": 4}
    assert "caliber" in out


def test_fidelity_metrics_empty_is_honest():
    from backend.shared.fill_quality import fidelity_metrics

    out = fidelity_metrics([], reference_prices={})
    assert out["orders"] == 0 and out["fill_rate"] is None and out["partial_ratio"] is None
    assert out["price_deviation_bps"]["n"] == 0
    assert out["price_deviation_bps"]["mean"] is None


# ── T-P6-19 影子 F2 维度 ────────────────────────────────────────────


def _pair(model: str, *, sim_price: float, real_price: float):
    return {
        "symbol": "600036.SH", "side": "buy",
        "sim_price": sim_price, "sim_quantity": 1000, "sim_execution_model": model,
        "sim_price_source": "snapshot" if model == "snapshot_core" else "redis_series",
        "real_price": real_price, "real_quantity": 1000, "real_commission": 0.0,
        "sim_fee": 0.0, "real_status": "filled", "sim_status": "filled",
        "symbol_mismatch": False, "price_source": "broker_fill",
    }


def test_build_f2_report_filters_snapshot_only():
    from backend.shared.shadow_compare import build_f2_report

    pairs = [_pair("snapshot_core", sim_price=10.05, real_price=10.00),
             _pair("synthetic_price", sim_price=10.20, real_price=10.00)]
    out = build_f2_report(pairs, configured_bps=5.0)
    assert out["sufficient"] is True and out["paired"] == 1
    # 只有 F2 那笔入统计（F1 的 20bps 偏差不得混入）
    assert out["price_deviation"]["n"] == 1
    assert out["partial_fill_diff"]["paired"] == 1

    assert build_f2_report([pairs[1]], configured_bps=5.0)["sufficient"] is False


def test_build_shadow_report_includes_f2_block():
    from backend.shared.shadow_compare import build_shadow_report

    report = build_shadow_report(
        date_str="2026-09-17",
        pairing={"pairs": [_pair("snapshot_core", sim_price=10.05, real_price=10.00)],
                 "sim_only": [], "real_only": [], "symbol_side_mismatch": 0},
        configured_bps=5.0,
    )
    assert "f2" in report and report["f2"]["sufficient"] is True
    # 无 F2 单时如实标注（不冒充）
    report2 = build_shadow_report(
        date_str="2026-09-17",
        pairing={"pairs": [_pair("synthetic_price", sim_price=10.1, real_price=10.0)],
                 "sim_only": [], "real_only": [], "symbol_side_mismatch": 0},
        configured_bps=5.0,
    )
    assert report2["f2"]["sufficient"] is False


# ── 交易台块（I 真机）──────────────────────────────────────────────


@pytest.mark.integration
def test_desk_fidelity_block_against_real_db():
    from sqlalchemy import text as sql_text

    from backend.services.api.routers.desk import _collect_fidelity
    from backend.shared.sync_db import sync_session

    tag = uuid.uuid4().hex[:6]
    symbol = "SH600036"  # 与 desk 采集同形（前缀式落库）
    try:
        with sync_session() as session:
            session.execute(
                sql_text(
                    "INSERT INTO sim_orders (order_id, tenant_id, user_id, portfolio_id, symbol, side, "
                    "order_type, status, quantity, filled_quantity, price, "
                    "average_price, order_value, filled_value, commission, total_fee, "
                    "execution_model, price_source, client_order_id, created_at, updated_at) "
                    "VALUES (gen_random_uuid(), 'default', 10000001, 0, :sym, 'buy', 'market', "
                    "        'filled', 1000, 1000, 10.0, 10.05, 10050, 10050, 5, 5, "
                    "        'snapshot_core', 'snapshot', :cid, NOW(), NOW())"
                ),
                {"sym": symbol, "cid": f"itest-fi-{tag}"},
            )
            session.commit()
        async def _run():
            await _ensure_fresh_db_pool()
            return await _collect_fidelity("default", 10000001)

        out = asyncio.run(_run())
        assert out["available"] is True
        assert out["orders"] >= 1
        assert out["by_execution_model"].get("snapshot_core", 0) >= 1
        assert out["exec_core_mode"] in {"daily", "snapshot"}
        assert "caliber" in out and "source" in out
        # 参考价来自 QuantDB（600036.SH 有收盘）→ 偏差分布有样本
        assert out["price_deviation_bps"]["n"] >= 1
    finally:
        with sync_session() as session:
            session.execute(
                sql_text("DELETE FROM sim_orders WHERE client_order_id = :cid"),
                {"cid": f"itest-fi-{tag}"},
            )
            session.commit()
