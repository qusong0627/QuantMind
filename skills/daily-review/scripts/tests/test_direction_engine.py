"""direction_engine 六维方向评分器单元测试（纯函数，合成数据，无 I/O）。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from direction_engine import score_dimensions  # noqa: E402


def _base_stats(**over) -> dict:
    s = {
        "meta": {"trade_date": "2026-08-19"},
        "index": [
            {"name": "上证指数", "missing": False, "vs_ma20": 0.5, "pct": 1.2},
            {"name": "上证指数2", "missing": True},
        ],
        "market": {
            "amount_ma5_yi": 24000.0, "amount_ratio_ma5": 1.1, "up_down_ratio": 1.8,
            "limit_up": 60, "limit_down": 5, "broke_up": 10, "max_streak": 6,
        },
        "sentiment": {"buy_pressure_mean": 0.6, "sell_pressure_mean": 0.4},
        "factors": {
            "l2": {"strong_pct": 0.4, "divergence_mean": 0.4, "super_net_yi": 50.0, "vpin_mean": 0.65},
            "sector_flow": [{"industry": "半导体", "net_yi": 5.0}, {"industry": "银行", "net_yi": -1.0}],
        },
        "news": {
            "n": 100, "bullish": 60, "bearish": 15, "neutral": 25,
            "gold_news": 40, "reverse_news": 5, "golden_hour_total": 30, "golden_hour_bullish": 20,
        },
        "sectors": {"行业板块(一级)": [{"SectorName": "半导体", "avg_pct": 2.5}] * 5},
    }
    s.update(over)
    return s


def test_bullish_market_gives_bullish_direction():
    d = score_dimensions(_base_stats())
    assert d["direction"] in ("看多", "强烈看多")
    assert d["total_score"] > 0


def test_bearish_market_gives_bearish_direction():
    s = _base_stats()
    s["market"].update({"limit_up": 5, "limit_down": 120, "up_down_ratio": 0.2, "broke_up": 30,
                         "amount_ratio_ma5": 0.8, "max_streak": 2})
    s["sentiment"].update({"buy_pressure_mean": 0.2, "sell_pressure_mean": 0.8})
    s["index"][0].update({"vs_ma20": -2.0, "pct": -2.5})
    s["news"].update({"bullish": 15, "bearish": 60, "reverse_news": 40, "gold_news": 5})
    s["factors"]["l2"].update({"strong_pct": 0.05, "super_net_yi": -40.0, "divergence_mean": 0.7})
    d = score_dimensions(s)
    assert d["direction"] in ("看空", "强烈看空")
    assert d["total_score"] < 0


def test_missing_news_lowers_data_completeness_but_not_direction():
    s = _base_stats()
    s["news"] = None
    d = score_dimensions(s)
    assert d["data_completeness"] <= 3  # 缺新闻 → 覆盖星级降级（≠模型置信度）
    assert d["direction"] is not None
    assert "confidence" not in d  # T6-4：旧键已改名，防回潮


def test_strong_signal_pct_high_and_super_inflow_supports_bullish_l2():
    s = _base_stats()
    s["factors"]["l2"] = {"strong_pct": 0.45, "divergence_mean": 0.3, "super_net_yi": 90.0, "vpin_mean": 0.7}
    d = score_dimensions(s)
    l2_dim = next(x for x in d["dimensions"] if x["name"] == "L2 微观")
    assert l2_dim["score"] > 0


def test_direction_threshold_extremes():
    # 非常弱的数据 → 震荡
    s = _base_stats()
    s["index"] = []
    for k in ("news", "factors", "sentiment"):
        s[k] = None
    s["market"].update({"amount_ratio_ma5": 1.0, "limit_up": 20, "limit_down": 18, "broke_up": 10,
                         "up_down_ratio": 1.0, "max_streak": 3})
    s["sectors"] = {}
    d = score_dimensions(s)
    assert d["direction"] == "震荡"


def test_all_dims_reported_with_evidence():
    d = score_dimensions(_base_stats())
    assert len(d["dimensions"]) == 5
    for dim in d["dimensions"]:
        assert dim["score"] is not None
        assert dim["evidence"]