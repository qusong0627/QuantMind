"""backend/scripts/market_composite_judge.py 单元测试（暴露映射）。"""

from __future__ import annotations

import pytest

from backend.scripts.market_composite_judge import base_exposure


class TestBaseExposure:
    def test_trend_and_cheap_is_max(self):
        lo, hi, _ = base_exposure(True, True)
        assert (lo, hi) == (0.8, 1.0)

    def test_trend_but_expensive_is_hold_not_add(self):
        lo, hi, _ = base_exposure(True, False)
        assert (lo, hi) == (0.3, 0.5)

    def test_cheap_but_no_trend_waits(self):
        lo, hi, _ = base_exposure(False, True)
        assert (lo, hi) == (0.4, 0.6)

    def test_no_trend_no_value_is_defensive(self):
        lo, hi, _ = base_exposure(False, False)
        assert (lo, hi) == (0.2, 0.4)

    @pytest.mark.parametrize(
        "above,cheap", [(True, True), (True, False), (False, True), (False, False)]
    )
    def test_interval_is_ordered(self, above, cheap):
        lo, hi, why = base_exposure(above, cheap)
        assert lo < hi
        assert why
