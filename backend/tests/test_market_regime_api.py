"""市场阶段 API（backend/services/api/market_analysis/regime.py）单元测试。

只在装有 fastapi 的环境（quantmind 容器）运行；宿主 /usr/bin/python3 无 fastapi 时跳过。
"""

from __future__ import annotations

import pandas as pd
import pytest

pytest.importorskip("fastapi")

from fastapi import HTTPException  # noqa: E402

from backend.services.api.market_analysis.regime import (  # noqa: E402
    _cap,
    _color,
    _pick,
    market_regime,
    regime_sector_stocks,
)


class TestCap:
    def test_bull_cheap_is_max(self):
        cap, score, _ = _cap(True, True, 0.9)
        assert cap == 1.0
        assert score == 2

    def test_bull_expensive_is_capped(self):
        cap, score, _ = _cap(True, True, 0.05)
        assert cap == 0.6
        assert score == 1

    def test_bear_expensive_is_min(self):
        cap, score, _ = _cap(False, False, 0.05)
        assert cap == 0.2
        assert score == -2

    def test_mixed_is_neutral(self):
        cap, score, _ = _cap(True, False, 0.5)
        assert cap == 0.6
        assert score == 0


class TestColor:
    @pytest.mark.parametrize(
        "score,expected",
        [(2, "green"), (1, "green"), (0, "yellow"), (-1, "orange"), (-2, "red")],
    )
    def test_mapping(self, score, expected):
        assert _color(score) == expected


class TestPick:
    def test_picks_latest_on_or_before(self):
        d = pd.DataFrame(
            {"x": [1, 2, 3]},
            index=pd.to_datetime(["2026-11-11", "2026-11-12", "2026-11-13"]),
        )
        assert _pick(d, "2026-11-12") == pd.Timestamp("2026-11-12")
        assert _pick(d, "2026-11-14") == pd.Timestamp("2026-11-13")
        assert _pick(d, None) == pd.Timestamp("2026-11-13")

    def test_bad_date_raises(self):
        d = pd.DataFrame({"x": [1]}, index=pd.to_datetime(["2026-11-13"]))
        with pytest.raises(HTTPException):
            _pick(d, "not-a-date")


class TestEndpoint:
    def test_shape(self):
        r = market_regime(date=None, current_user={})
        assert len(r["layers"]) == 5
        assert {"ceiling", "actual", "label"} <= set(r["exposure"])
        assert len(r["charts"]["dates"]) <= 250
        assert len(r["charts"]["dates"]) == len(r["charts"]["close"])
        assert "current" in r["quadrant"]
        # 2×2 用 2016 起长历史（隔日取样），不能只剩 560 日窗口里的 ~30 点
        assert len(r["quadrant"]["x"]) > 500
        assert len(r["quadrant"]["x"]) == len(r["quadrant"]["y"])
        assert len(r["quadrant"]["x"]) == len(r["quadrant"]["ret"])
        assert len(r["blocks"]) == 6
        assert r["summary"]["score"] == sum(b["score"] for b in r["blocks"])
        assert len(r["block_history"]) > 0
        assert all(len(p["scores"]) == 6 for p in r["block_history"])
        assert len(r["sector"]["list"]) > 50

    def test_sector_stocks_linkage(self):
        r = market_regime(date=None, current_user={})
        sector = r["sector"]["list"][0]["name"]
        s = regime_sector_stocks(sector=sector, date=None, current_user={})
        assert s["sector"] == sector
        assert isinstance(s["stocks"], list)
        if s["stocks"]:
            assert "rs_1010" in s["stocks"][0]

    def test_date_switch_returns_same_or_earlier(self):
        r = market_regime(date="2026-10-28", current_user={})
        assert r["date"] <= "2026-10-28"

    def test_universe_switch(self):
        r = market_regime(date=None, universe="000300.SH", current_user={})
        assert r["universe"] == "000300.SH"
        assert r["universe_label"] == "沪深300"
        assert r["valuation_scope"] == "universe"
        assert len(r["blocks"]) == 6
        assert r["summary"]["score"] == sum(b["score"] for b in r["blocks"])

    def test_bad_universe_raises(self):
        with pytest.raises(HTTPException):
            market_regime(date=None, universe="NOT_AN_INDEX", current_user={})
