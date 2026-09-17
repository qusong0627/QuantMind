"""识别引擎检测器金样（T-P6-14）：四类检测正反例 + 边界（宁缺毋假）。"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


# ── 市场异动（量价）──────────────────────────────────────────────────


def test_volume_surge_triggers_with_time_correction():
    from backend.services.engine.anomaly_detectors import detect_volume_price

    # 半日进度（frac=0.5）：现量 = 日均可量 4 倍 → 量比 8 触发（含时间校正）
    out = detect_volume_price(
        {"600036.SH": {"price": 40.0, "pct_chg": 0.01, "now_volume": 4_000.0,
                       "avg_daily_volume": 1_000.0}},
        volume_ratio_min=3.0, elapsed_fraction=0.5,
    )
    kinds = {d.kind for d in out}
    assert "volume_surge" in kinds
    surge = next(d for d in out if d.kind == "volume_surge")
    assert surge.metrics["volume_ratio"] == pytest.approx(8.0)
    assert surge.severity == "critical"  # ≥2× 阈值


def test_volume_surge_negative_and_time_correction_blocks_early_false_positive():
    from backend.services.engine.anomaly_detectors import detect_volume_price

    # 早盘 10 分钟（frac=0.05）现量=日均 0.2 倍 → 量比 4 触发；同数据全日 frac=1 则为 0.2 不触发
    quotes = {"600036.SH": {"price": 40.0, "pct_chg": 0.0, "now_volume": 200.0,
                            "avg_daily_volume": 1_000.0}}
    early = detect_volume_price(quotes, volume_ratio_min=3.0, elapsed_fraction=0.05)
    full = detect_volume_price(quotes, volume_ratio_min=3.0, elapsed_fraction=1.0)
    assert any(d.kind == "volume_surge" for d in early)
    assert not any(d.kind == "volume_surge" for d in full)


def test_price_limit_states_and_surge():
    from backend.services.engine.anomaly_detectors import detect_volume_price

    out = detect_volume_price(
        {
            "600000.SH": {"price": 11.0, "pct_chg": 0.10, "limit_up": 11.0},     # 涨停
            "000001.SZ": {"price": 9.0, "pct_chg": -0.10, "limit_down": 9.0},    # 跌停
            "300750.SZ": {"price": 100.0, "pct_chg": 0.07},                      # 大幅上行（未触档）
            "601318.SH": {"price": 50.0, "pct_chg": -0.06},                      # 大幅下行（未触档）
        },
        price_pct_min=0.05,
    )
    kinds = {(d.subject, d.kind) for d in out}
    assert ("600000.SH", "price_limit_up") in kinds
    assert ("000001.SZ", "price_limit_down") in kinds
    assert ("300750.SZ", "price_surge") in kinds
    assert ("601318.SH", "price_surge") in kinds
    sev = {d.subject: d.severity for d in out}
    assert sev["300750.SZ"] == "info" and sev["601318.SH"] == "warn"
    # 跌停比涨停更危险：critical 归否决链，这里断言 warn（否决链只吃 critical）
    assert sev["000001.SZ"] == "warn"


def test_volume_price_skips_suspended_and_missing_fields():
    from backend.services.engine.anomaly_detectors import detect_volume_price

    out = detect_volume_price(
        {
            "600000.SH": {"price": 11.0, "pct_chg": 0.2, "is_suspended": "true"},  # 停牌
            "000001.SZ": {"price": None, "pct_chg": 0.3},                          # 缺价
            "300750.SZ": {"price": 10.0},                                          # 缺 pct
            "601318.SH": {"price": 0.0, "pct_chg": 0.5},                           # 非法价
        }
    )
    assert out == []


# ── 账户异常 ────────────────────────────────────────────────────────


def test_account_cancel_ratio_and_min_orders():
    from backend.services.engine.anomaly_detectors import detect_account_anomaly

    orders = [{"status": "cancelled"}] * 7 + [{"status": "filled"}] * 3
    out = detect_account_anomaly(orders, [], cancel_ratio_min=0.6, min_orders=5, subject="u1")
    assert any(d.kind == "account_cancel_ratio" for d in out)
    hit = next(d for d in out if d.kind == "account_cancel_ratio")
    assert hit.metrics["cancel_ratio"] == pytest.approx(0.7)
    # 样本不足（4 < 5）不判
    assert detect_account_anomaly(orders[:4], [], min_orders=5, subject="u1") == []


def test_account_concentration():
    from backend.services.engine.anomaly_detectors import detect_account_anomaly

    positions = [
        {"symbol": "600036.SH", "market_value": 600_000},
        {"symbol": "000001.SZ", "market_value": 400_000},
    ]
    out = detect_account_anomaly([], positions, concentration_max=0.5, subject="u2")
    assert any(d.kind == "account_concentration" for d in out)
    hit = next(d for d in out if d.kind == "account_concentration")
    assert hit.metrics["top_symbol"] == "600036.SH"
    # 市值过小不判（min_position_value 守护）
    small = [{"symbol": "600036.SH", "market_value": 100}]
    assert detect_account_anomaly([], small, min_position_value=1_000.0, subject="u2") == []


# ── 数据异常 ────────────────────────────────────────────────────────


def test_data_jump_gap_zero_volume():
    from backend.services.engine.anomaly_detectors import detect_data_anomaly

    # 跳变：11.0 → 20.0（+82%），远超涨跌停包络
    latest = {"date": "2026-09-14", "close": 20.0, "volume": 1000,
              "limit_up": 12.1, "limit_down": 9.9}
    prev = {"date": "2026-09-11", "close": 11.0}
    out = detect_data_anomaly(latest, prev, subject="600036.SH")
    assert any(d.kind == "data_jump" for d in out)

    # 缺口：期望前一日 09-11，实际 09-10
    gap = detect_data_anomaly(
        {"date": "2026-09-14", "close": 11.0, "volume": 1000},
        {"date": "2026-09-10", "close": 11.0},
        expected_prev_date="2026-09-11", subject="600036.SH",
    )
    assert any(d.kind == "data_gap" for d in gap)

    # 零成交
    zero = detect_data_anomaly({"date": "2026-09-14", "close": 11.0, "volume": 0}, None,
                               subject="600036.SH")
    assert any(d.kind == "data_zero_volume" for d in zero)

    # 正常样本：不误报
    ok = detect_data_anomaly(
        {"date": "2026-09-14", "close": 11.1, "volume": 1000, "limit_up": 12.1, "limit_down": 9.9},
        {"date": "2026-09-11", "close": 11.0},
        expected_prev_date="2026-09-11", subject="600036.SH",
    )
    assert ok == []


# ── 模型异常 ────────────────────────────────────────────────────────


def test_model_ic_drop_absolute_and_relative():
    from backend.services.engine.anomaly_detectors import detect_model_anomaly

    # 绝对：短窗为负 → critical
    out = detect_model_anomaly("m1", {"ic_5": -0.02, "ic_20": 0.03, "n_5": 5, "n_20": 20})
    assert out and out[0].kind == "model_ic_drop" and out[0].severity == "critical"

    # 相对骤降：0.05 → 0.01（-80% > 50%）→ warn（短窗仍为正）
    rel = detect_model_anomaly("m2", {"ic_5": 0.01, "ic_20": 0.05, "n_5": 5, "n_20": 20})
    assert rel and rel[0].severity == "warn"

    # 样本不足 → 不判
    assert detect_model_anomaly("m3", {"ic_5": -0.1, "ic_20": 0.03, "n_5": 2, "n_20": 20}) == []

    # 健康 → 不判
    assert detect_model_anomaly("m4", {"ic_5": 0.04, "ic_20": 0.05, "n_5": 5, "n_20": 20}) == []
