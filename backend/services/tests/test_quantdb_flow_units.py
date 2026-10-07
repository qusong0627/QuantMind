"""l2 flow 金额单位归一（万元 ↔ 元）单测 + 读入边界接线契约。

背景：QuantDB ``l2_factors.flow_*`` 金额单位按分区混存（2026-09-21 起万元，
此前近年为元，更早分区又是万元），下游市场分析 / 投研 / 终端按「元」消费——
不归一会在万元分区上产生 1e4 量级错值，且跨单位窗口的趋势序列会单位撕裂。
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from backend.shared.quantdb_flow_units import (
    detect_flow_money_scale_to_yuan,
    normalize_l2_flow_money_to_yuan,
)

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_detect_wan_via_ratio_identity():
    # amount=万元，flow=万元 → flow/amount = ratio
    df = pd.DataFrame(
        {
            "symbol": [f"S{i:04d}.SZ" for i in range(30)],
            "amount": [10000.0] * 30,
            "flow_net_amount": [800.0] * 30,
            "flow_net_ratio": [0.08] * 30,
            "flow_buy_amount": [5000.0] * 30,
        }
    )
    assert detect_flow_money_scale_to_yuan(df) == 1e4
    out = normalize_l2_flow_money_to_yuan(df)
    assert out["flow_net_amount"].iloc[0] == 800.0 * 1e4
    assert out["flow_buy_amount"].iloc[0] == 5000.0 * 1e4
    # ratio 不动
    assert out["flow_net_ratio"].iloc[0] == 0.08


def test_detect_yuan_via_ratio_identity():
    # amount=万元，flow=元 → flow/(amount*1e4) = ratio
    df = pd.DataFrame(
        {
            "symbol": [f"S{i:04d}.SZ" for i in range(30)],
            "amount": [10000.0] * 30,
            "flow_net_amount": [8_000_000.0] * 30,
            "flow_net_ratio": [0.08] * 30,
            "flow_super_net": [1_000_000.0] * 30,
        }
    )
    assert detect_flow_money_scale_to_yuan(df) == 1.0
    out = normalize_l2_flow_money_to_yuan(df)
    assert out["flow_net_amount"].iloc[0] == 8_000_000.0
    assert out["flow_super_net"].iloc[0] == 1_000_000.0


def test_groupby_dt_mixed_units():
    wan = pd.DataFrame(
        {
            "dt": ["20260918"] * 30,
            "amount": [20000.0] * 30,
            "flow_net_amount": [2000.0] * 30,
            "flow_net_ratio": [0.1] * 30,
        }
    )
    yuan = pd.DataFrame(
        {
            "dt": ["20260917"] * 30,
            "amount": [20000.0] * 30,
            "flow_net_amount": [20_000_000.0] * 30,
            "flow_net_ratio": [0.1] * 30,
        }
    )
    df = pd.concat([wan, yuan], ignore_index=True)
    out = normalize_l2_flow_money_to_yuan(df)
    assert out.loc[out["dt"] == "20260918", "flow_net_amount"].iloc[0] == 2000.0 * 1e4
    assert out.loc[out["dt"] == "20260917", "flow_net_amount"].iloc[0] == 20_000_000.0


def test_no_amount_falls_back_to_median_hint():
    """无 amount 列（旧查询未带）时按量级兜底：万元日 |flow| 中位数 ~1e3。"""
    df = pd.DataFrame(
        {
            "symbol": [f"S{i:04d}.SZ" for i in range(30)],
            "flow_net_amount": [900.0] * 30,
        }
    )
    assert detect_flow_money_scale_to_yuan(df) == 1e4
    out = normalize_l2_flow_money_to_yuan(df)
    assert out["flow_net_amount"].iloc[0] == 900.0 * 1e4


def _src(rel: str) -> str:
    return (REPO_ROOT / rel).read_text(encoding="utf-8")


def test_market_analysis_feed_normalizes():
    src = _src("backend/services/api/market_analysis/quantdb_feed.py")
    assert "normalize_l2_flow_money_to_yuan" in src
    # 对账列 amount 必须进入 SELECT（否则只能走量级兜底）
    assert '"symbol, amount, "' in src


def test_market_analysis_service_normalizes():
    src = _src("backend/services/api/market_analysis/quantdb_service.py")
    assert "normalize_l2_flow_money_to_yuan" in src
    assert "flow_net_ratio, dt FROM read_parquet(" in src


def test_research_features_normalizes():
    src = _src("backend/services/api/routers/research_features_service.py")
    assert "normalize_l2_flow_money_to_yuan" in src
    assert 'sources["qdb_l2_factors"]' in src


def test_stock_terminal_flow_group_normalizes():
    src = _src("backend/services/api/routers/stock_terminal.py")
    assert "normalize_l2_flow_money_to_yuan" in src
    # flow 组必须取 amount 列（对账用），且对外输出要剔除
    assert 'c != "amount"' in src


def test_market_snapshot_flow_normalizes():
    src = _src("backend/scripts/market_snapshot/compute.py")
    assert "normalize_l2_flow_money_to_yuan" in src
    src_sa = _src("backend/scripts/market_snapshot/schema_adapter.py")
    assert '"amount"' in src_sa
