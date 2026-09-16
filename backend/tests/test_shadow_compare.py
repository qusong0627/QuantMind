"""T-P2-06 测试：影子对照指标（纯函数唯一实现 shared/shadow_compare.py）。

口径（设计文档 §8.3）：成交价偏差分布（bps，正=相对模拟更差）、成交率/部分成交、
滑点实现值 vs 配置值、模拟-实盘跟踪误差（日收益差，年化 ×√244）。
配对：真单 `mir-{base}` ↔ 模拟单（client_order_id ∪ order_id ∪ remarks 内嵌 cid）。
"""

from __future__ import annotations

import math
from datetime import date

import pytest

from backend.shared.shadow_compare import (
    build_shadow_report,
    compute_fill_stats,
    compute_price_deviation,
    compute_slippage_realization,
    compute_tracking_error,
    mirror_base,
    pair_orders,
)


def _sim(
    order_id="o1",
    cid=None,
    remarks=None,
    symbol="600036.SH",
    side="buy",
    price=10.0,
    qty=100.0,
):
    return {
        "order_id": order_id,
        "client_order_id": cid,
        "remarks": remarks,
        "symbol": symbol,
        "side": side,
        "fill_price": price,
        "filled_quantity": qty,
    }


def _real(
    cid="mir-o1",
    symbol="600036.SH",
    side="buy",
    price=10.0,
    qty=100.0,
    status="filled",
    commission=5.0,
    price_source="broker_fill",
):
    return {
        "client_order_id": cid,
        "symbol": symbol,
        "side": side,
        "average_price": price,
        "filled_quantity": qty,
        "status": status,
        "commission": commission,
        "price_source": price_source,
    }


# ── 配对 ────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_mirror_base_strips_prefix_only():
    assert mirror_base("mir-abc") == "abc"
    assert mirror_base("sim-abc") is None
    assert mirror_base("") is None
    assert mirror_base(None) is None


@pytest.mark.unit
def test_pair_orders_by_order_id_and_cid_and_remarks():
    sims = [
        _sim(order_id="o1"),  # 引擎路径：mirror base = sim_order_id
        _sim(order_id="o2", cid="sim-run-600036.SH-buy"),  # cid 列路径
        _sim(
            order_id="o3", remarks="client_order_id=hc-123 下单"
        ),  # dispatcher remarks 路径
    ]
    reals = [
        _real(cid="mir-o1"),
        _real(cid="mir-sim-run-600036.SH-buy"),
        _real(cid="mir-hc-123"),
        _real(cid="mir-legacy-stress-1", status="rejected", price=0.0, qty=0.0),
    ]
    res = pair_orders(sims, reals)
    assert len(res["pairs"]) == 3
    assert {p["base"] for p in res["pairs"]} == {
        "o1",
        "sim-run-600036.SH-buy",
        "hc-123",
    }
    assert res["sim_only"] == []
    assert len(res["real_only"]) == 1
    assert res["real_only"][0]["client_order_id"] == "mir-legacy-stress-1"
    assert res["symbol_side_mismatch"] == 0


@pytest.mark.unit
def test_pair_orders_flags_symbol_mismatch():
    res = pair_orders(
        [_sim(order_id="o1", symbol="600036.SH", side="buy")],
        [_real(cid="mir-o1", symbol="000001.SZ", side="buy")],
    )
    assert len(res["pairs"]) == 1
    assert res["symbol_side_mismatch"] == 1
    assert res["pairs"][0]["symbol_mismatch"] is True


@pytest.mark.unit
def test_pair_orders_empty_inputs():
    res = pair_orders([], [])
    assert res["pairs"] == [] and res["sim_only"] == [] and res["real_only"] == []
    assert res["symbol_side_mismatch"] == 0


# ── 成交价偏差 ──────────────────────────────────────────────────────


def _pair(
    side="buy", sim_p=10.0, real_p=10.01, sim_q=100.0, real_q=100.0, status="filled"
):
    return {
        "base": "x",
        "symbol": "600036.SH",
        "side": side,
        "sim_price": sim_p,
        "sim_quantity": sim_q,
        "real_price": real_p,
        "real_quantity": real_q,
        "real_status": status,
        "real_commission": 5.0,
        "price_source": "broker_fill",
    }


@pytest.mark.unit
def test_price_deviation_signed_cost_direction():
    # 买入实际更贵 → 正成本；卖出实际更便宜 → 正成本
    buy = compute_price_deviation([_pair(side="buy", real_p=10.01)])
    sell = compute_price_deviation([_pair(side="sell", real_p=9.99)])
    assert buy["n"] == 1 and sell["n"] == 1
    assert buy["mean_bps"] == pytest.approx(10.0, abs=0.01)
    assert sell["mean_bps"] == pytest.approx(10.0, abs=0.01)


@pytest.mark.unit
def test_price_deviation_distribution_stats():
    pairs = [
        _pair(real_p=10.0),  # 0 bps
        _pair(real_p=10.01),  # +10 bps
        _pair(real_p=9.99),  # -10 bps
        _pair(real_p=10.02),  # +20 bps
    ]
    d = compute_price_deviation(pairs)
    assert d["n"] == 4
    assert d["mean_bps"] == pytest.approx(5.0, abs=0.01)
    assert d["median_bps"] == pytest.approx(5.0, abs=0.01)
    assert d["p95_abs_bps"] == pytest.approx(20.0, abs=0.01)
    assert d["abs_mean_bps"] == pytest.approx(10.0, abs=0.01)


@pytest.mark.unit
def test_price_deviation_skips_priceless_pairs():
    d = compute_price_deviation([_pair(real_p=0.0), _pair(sim_p=0.0)])
    assert d["n"] == 0
    assert d["mean_bps"] is None


# ── 成交率/部分成交 ─────────────────────────────────────────────────


@pytest.mark.unit
def test_fill_stats_rate_and_partial():
    pairs = [
        _pair(sim_q=100, real_q=100, status="filled"),
        _pair(sim_q=200, real_q=100, status="partially_filled"),
        _pair(sim_q=100, real_q=0, status="rejected"),
    ]
    s = compute_fill_stats(pairs)
    assert s["sim_quantity"] == 400.0
    assert s["real_quantity"] == 200.0
    assert s["fill_rate"] == pytest.approx(0.5, abs=1e-9)
    assert s["partial_count"] == 1
    assert s["rejected_count"] == 1
    assert s["filled_count"] == 1


@pytest.mark.unit
def test_fill_stats_zero_sim_quantity_no_division():
    s = compute_fill_stats([_pair(sim_q=0, real_q=0)])
    assert s["fill_rate"] is None


# ── 滑点实现值 vs 配置值 ────────────────────────────────────────────


@pytest.mark.unit
def test_slippage_realization_vs_configured():
    pairs = [
        _pair(real_p=10.01),  # |10 bps|
        _pair(real_p=9.99),  # |10 bps|
    ]
    r = compute_slippage_realization(pairs, configured_bps=5.0)
    assert r["n"] == 2
    assert r["realized_abs_mean_bps"] == pytest.approx(10.0, abs=0.01)
    assert r["configured_bps"] == 5.0
    assert r["delta_bps"] == pytest.approx(5.0, abs=0.01)


@pytest.mark.unit
def test_slippage_realization_no_samples():
    r = compute_slippage_realization([], configured_bps=5.0)
    assert r["n"] == 0
    assert r["realized_abs_mean_bps"] is None
    assert r["delta_bps"] is None


# ── 跟踪误差 ────────────────────────────────────────────────────────


@pytest.mark.unit
def test_tracking_error_perfect_tracking():
    sim = [
        (date(2026, 9, 1), 100.0),
        (date(2026, 9, 2), 101.0),
        (date(2026, 9, 3), 102.0),
    ]
    te = compute_tracking_error(sim, list(sim))
    assert te["sufficient"] is True
    assert te["n_returns"] == 2
    assert te["mean_diff_bps"] == pytest.approx(0.0, abs=1e-6)
    assert te["te_ann_bps"] == pytest.approx(0.0, abs=1e-6)


@pytest.mark.unit
def test_tracking_error_detects_drift():
    # sim 每日 +2%、real 每日 +1% → 日差恒 100 bps（std=0，mean=100）
    sim = [
        (date(2026, 9, 1), 100.0),
        (date(2026, 9, 2), 102.0),
        (date(2026, 9, 3), 104.04),
    ]
    real = [
        (date(2026, 9, 1), 100.0),
        (date(2026, 9, 2), 101.0),
        (date(2026, 9, 3), 102.01),
    ]
    te = compute_tracking_error(sim, real)
    assert te["mean_diff_bps"] == pytest.approx(100.0, abs=0.5)
    assert te["std_diff_bps"] == pytest.approx(0.0, abs=0.5)


@pytest.mark.unit
def test_tracking_error_aligns_common_dates_only():
    sim = [
        (date(2026, 9, 1), 100.0),
        (date(2026, 9, 2), 101.0),
        (date(2026, 9, 4), 103.0),  # 9/3 缺失
    ]
    real = [
        (date(2026, 9, 1), 100.0),
        (date(2026, 9, 3), 101.5),  # 9/2 缺失
        (date(2026, 9, 4), 102.0),
    ]
    te = compute_tracking_error(sim, real)
    # 共同日期 = 9/1, 9/4 → 1 个收益（不足 2 个 → insufficient 但给出数据）
    assert te["common_days"] == 2
    assert te["n_returns"] == 1
    assert te["sufficient"] is False


@pytest.mark.unit
def test_tracking_error_insufficient_samples():
    te = compute_tracking_error([(date(2026, 9, 1), 100.0)], [])
    assert te["sufficient"] is False
    assert te["reason"]


# ── 报告组装 ────────────────────────────────────────────────────────


@pytest.mark.unit
def test_build_shadow_report_assembles_sections():
    pairing = pair_orders([_sim(order_id="o1")], [_real(cid="mir-o1", price=10.01)])
    report = build_shadow_report(
        date_str="20260916",
        pairing=pairing,
        configured_bps=5.0,
        tracking={"sufficient": False, "reason": "模拟侧无日度净值"},
    )
    assert report["date"] == "20260916"
    assert report["coverage"]["matched"] == 1
    assert report["coverage"]["real_only"] == 0
    assert report["price_deviation"]["n"] == 1
    assert report["fill"]["fill_rate"] == pytest.approx(1.0)
    assert report["slippage"]["configured_bps"] == 5.0
    assert report["ok"] is True
    assert report["source"]


@pytest.mark.unit
def test_build_shadow_report_not_ok_on_mismatch():
    pairing = pair_orders(
        [_sim(order_id="o1", symbol="600036.SH")],
        [_real(cid="mir-o1", symbol="000001.SZ")],
    )
    report = build_shadow_report(
        date_str="20260916", pairing=pairing, configured_bps=5.0
    )
    assert report["ok"] is False
    assert report["coverage"]["symbol_side_mismatch"] == 1
    assert report["tracking_error"]["sufficient"] is False


@pytest.mark.unit
def test_tracking_error_annualization_constant():
    # 固定常数回归（防年化口径漂移）：244 交易日
    sim = [
        (date(2026, 9, 1), 100.0),
        (date(2026, 9, 2), 101.0),
        (date(2026, 9, 3), 100.0),
    ]
    real = [
        (date(2026, 9, 1), 100.0),
        (date(2026, 9, 2), 100.5),
        (date(2026, 9, 3), 100.0),
    ]
    te = compute_tracking_error(sim, real)
    assert te["te_ann_bps"] == pytest.approx(
        (te["std_diff_bps"] or 0.0) * math.sqrt(244), abs=0.5
    )
