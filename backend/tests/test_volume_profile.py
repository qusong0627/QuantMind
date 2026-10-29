"""筹码结构/成交价格分布纯函数测试。"""

from __future__ import annotations

import pandas as pd

from backend.services.api.market_analysis.volume_profile import _profile, _weighted_cost


def test_profile_poc_value_area_and_p14():
    frame = pd.DataFrame(
        {
            "high": [10.0, 10.0, 10.0, 10.0],
            "low": [8.0, 8.0, 8.0, 8.0],
            "close": [9.0, 9.0, 9.0, 9.0],
            "amount": [100.0, 100.0, 100.0, 100.0],
            "volume": [1.0, 1.0, 1.0, 1.0],
        }
    )
    profile, poc, vah, val, p14, nodes = _profile(frame, bins=4)
    assert len(profile) == 4
    assert profile[0]["low"] == 8.0 and profile[-1]["high"] == 10.0
    assert poc is not None and 8.0 <= poc <= 10.0
    assert val is not None and vah is not None and val <= vah
    # 14% 从最低价向上累计：应落在最低箱内
    assert p14 is not None and 8.0 <= p14 < 8.5
    assert nodes and len(nodes) <= 3


def test_weighted_cost_ignores_negative_weights():
    prices = pd.Series([10.0, 20.0, 30.0])
    weights = pd.Series([1.0, -1.0, 1.0])
    assert _weighted_cost(prices, weights) == 20.0
    assert _weighted_cost(prices, pd.Series([0.0, 0.0, 0.0])) is None
