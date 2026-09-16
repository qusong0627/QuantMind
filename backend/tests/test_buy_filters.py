"""T-P4-04 测试：买入前 K 线过滤（KHunter 4 规则）——纯函数边界 + 解析器 + 真库装载。

四规则（设计 §事前·形态）：rise_from_low≤50% / open_gap≤4% / bias5≤7 / vol_ratio≥0.7。
纪律：数据不足 fail-closed 拒买；基础设施不可用由调用方整步跳过并如实标注。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest


def _bars(  # 构造可控日线序列：默认温和上涨、无跳空、量能平稳（应通过全规则）
    n: int = 10,
    *,
    base: float = 10.0,
    closes=None,
    last_open=None,
    last_close=None,
    last_low=None,
    last_volume=None,
    volumes=None,
):
    series = []
    close_list = closes or [base * (1 + 0.002 * i) for i in range(n)]
    for i, close in enumerate(close_list):
        o = close * 0.999
        low = close * 0.99
        vol = volumes[i] if volumes else 100000.0
        row = {
            "open": o,
            "high": close * 1.01,
            "low": low,
            "close": close,
            "volume": vol,
        }
        series.append(row)
    if last_close is not None:
        series[-1]["close"] = last_close
    if last_open is not None:
        series[-1]["open"] = last_open
    if last_low is not None:
        series[-1]["low"] = last_low
    if last_volume is not None:
        series[-1]["volume"] = last_volume
    return series


# ── 四规则边界 ──────────────────────────────────────────────────────


@pytest.mark.unit
def test_clean_series_passes_all_rules():
    from backend.shared.buy_filters import evaluate_bar_filters

    ok, reasons, evidence = evaluate_bar_filters(_bars())
    assert ok and reasons == []
    assert "rise_from_low" in evidence and "open_gap" in evidence
    assert "bias5" in evidence and "vol_ratio" in evidence


@pytest.mark.unit
def test_rise_from_low_rejects_hot_stock():
    """距低点涨幅：低点 5 → 收盘 8（+60%>50%）→ 拒。"""
    from backend.shared.buy_filters import evaluate_bar_filters

    bars = _bars(closes=[5.0] * 5 + [7.0, 7.5, 8.0, 8.0, 8.0], base=5.0)
    bars[-1]["low"] = 5.0  # 窗口低点=5
    bars[-1]["close"] = 8.0
    bars[-1]["open"] = 7.9
    ok, reasons, evidence = evaluate_bar_filters(bars)
    assert not ok
    assert any("低点涨幅" in r for r in reasons)
    # 窗口低点 = 首根 low（=5.0×0.99）；涨幅 = (8 − 4.95)/4.95
    assert evidence["rise_from_low"] == pytest.approx((8.0 - 4.95) / 4.95, abs=1e-4)


@pytest.mark.unit
def test_open_gap_rejects_high_open():
    from backend.shared.buy_filters import evaluate_bar_filters

    bars = _bars()
    prev_close = bars[-2]["close"]
    bars[-1]["open"] = prev_close * 1.05  # +5% 跳空
    ok, reasons, evidence = evaluate_bar_filters(bars)
    assert not ok
    assert any("开盘跳空" in r for r in reasons)
    assert evidence["open_gap"] == pytest.approx(0.05, abs=1e-6)


@pytest.mark.unit
def test_bias5_rejects_overheated():
    from backend.shared.buy_filters import evaluate_bar_filters

    bars = _bars(closes=[10.0, 10.0, 10.0, 10.0, 10.0, 10.0, 10.0, 10.0, 10.0, 10.0])
    # 当日收盘拉到 10.8 → BIAS5 = (10.8 - 10.24)/10.24 ≈ 5.47%？构造更极端：10.0/10.0/.../11.0
    bars[-1]["close"] = 11.0
    bars[-1]["open"] = 10.1
    ok, reasons, evidence = evaluate_bar_filters(bars)
    assert not ok
    assert any("BIAS5" in r for r in reasons)
    assert evidence["bias5"] > 7.0


@pytest.mark.unit
def test_volume_confirmation_rejects_dry_volume():
    from backend.shared.buy_filters import evaluate_bar_filters

    vols = [100000.0] * 9 + [30000.0]  # 当日量仅 0.3×均量
    bars = _bars(volumes=vols)
    ok, reasons, evidence = evaluate_bar_filters(bars)
    assert not ok
    assert any("量能" in r for r in reasons)
    assert evidence["vol_ratio"] == pytest.approx(0.3, abs=0.01)


@pytest.mark.unit
def test_insufficient_data_fails_closed():
    from backend.shared.buy_filters import evaluate_bar_filters

    ok, reasons, _ = evaluate_bar_filters([{"close": 10.0}])
    assert ok is False and "数据不足" in reasons[0]
    ok2, reasons2, _ = evaluate_bar_filters([])
    assert ok2 is False


@pytest.mark.unit
def test_missing_critical_field_fails_closed():
    from backend.shared.buy_filters import evaluate_bar_filters

    bars = _bars()
    bars[-1]["open"] = None
    ok, reasons, _ = evaluate_bar_filters(bars)
    assert ok is False
    assert any("关键字段" in r for r in reasons)


# ── 解析器（策略 spec risk.buy_filters 表达式）──────────────────────


@pytest.mark.unit
def test_parse_canonical_expressions():
    from backend.shared.buy_filters import parse_buy_filters

    cfg = parse_buy_filters(
        ["rise_from_low<=50%", "open_gap<=4%", "bias5<=7", "vol_ratio>=0.7"]
    )
    assert cfg.max_rise_from_low == pytest.approx(0.50)
    assert cfg.max_open_gap == pytest.approx(0.04)
    assert cfg.max_bias5 == pytest.approx(7.0)
    assert cfg.min_vol_ratio == pytest.approx(0.7)
    # 空输入 → 默认
    assert parse_buy_filters(None) == parse_buy_filters([])


@pytest.mark.unit
def test_parse_rejects_unknown_and_wrong_direction():
    from backend.shared.buy_filters import parse_buy_filters

    with pytest.raises(ValueError):
        parse_buy_filters(["totally_unknown<=1"])
    with pytest.raises(ValueError):
        parse_buy_filters(["bias5>=7"])  # bias5 是上界规则
    with pytest.raises(ValueError):
        parse_buy_filters(["vol_ratio<=1"])  # vol_ratio 是下界规则


# ── apply（机会列表 × 日线）─────────────────────────────────────────


@pytest.mark.unit
def test_apply_buy_filters_keeps_and_rejects_with_evidence():
    from backend.shared.buy_filters import apply_buy_filters

    opps = [
        SimpleNamespace(symbol="600036.SH"),
        SimpleNamespace(symbol="000001.SZ"),
        SimpleNamespace(symbol="600000.SH"),  # 无日线 → fail-closed
    ]
    bars = {
        "600036.SH": _bars(),
        "000001.SZ": _bars(volumes=[100000.0] * 9 + [10000.0]),  # 缩量拒
    }
    kept, rejected = apply_buy_filters(opps, bars)
    assert [o.symbol for o in kept] == ["600036.SH"]
    assert {r["symbol"] for r in rejected} == {"000001.SZ", "600000.SH"}
    assert any(
        "fail-closed" in r["reasons"][0] for r in rejected if r["symbol"] == "600000.SH"
    )


@pytest.mark.unit
def test_runner_wires_buy_filters():
    """接线源断言：runner 在扫描后执行买入前过滤并落 meta（防重构误删）。"""
    from pathlib import Path

    src = (
        Path(__file__).resolve().parents[1] / "services/engine/scanners/runner.py"
    ).read_text(encoding="utf-8")
    assert "apply_buy_filters" in src
    assert "load_recent_daily_bars" in src
    assert '"buy_filters"' in src


# ── 真库装载 ────────────────────────────────────────────────────────


@pytest.mark.unit
def test_real_daily_bars_loader_returns_series():
    """真库装载：9/15 实选 5 只取不复权日线——至少部分有 ≥6 根；结构化返回。"""
    from backend.services.engine.scanners.daily_bars import load_recent_daily_bars

    symbols = ["600648.SH", "600817.SH", "600983.SH", "603357.SH", "603676.SH"]
    bars_by_symbol = load_recent_daily_bars(symbols, end_date="2026-09-15")
    if bars_by_symbol is None:
        pytest.skip("QuantDB hub 不可用")
    series_ok = [s for s, b in bars_by_symbol.items() if len(b) >= 6]
    assert series_ok, (
        f"应有标的取到 ≥6 根日线（实得: { {s: len(b) for s, b in bars_by_symbol.items()} }）"
    )
    sample = bars_by_symbol[series_ok[0]]
    assert set(sample[-1]) >= {"open", "high", "low", "close", "volume", "date"}
