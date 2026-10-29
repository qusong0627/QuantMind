"""主力视角日频序列的纯函数测试（不加载真实 L2）。"""

from __future__ import annotations

import numpy as np
import pandas as pd

from backend.scripts.market_analysis.main_force import latest_block


def test_latest_block_returns_latest_values_and_none_for_nan():
    df = pd.DataFrame(
        {
            "main_net_yi": [100.0, 121.9],
            "turnover_yi": [15000.0, 16421.0],
            "main_net_ratio": [0.006, 0.0075],
            "main_net_z20": [0.2, 0.55],
            "main_breadth": [0.55, 0.65],
            "main_breadth_amt": [0.5, 0.63],
        },
        index=pd.to_datetime(["2026-10-27", "2026-10-28"]),
    )
    out = latest_block(df)
    assert out["date"] == "2026-10-28"
    assert out["main_net_yi"] == 121.9
    assert out["main_net_ratio"] == 0.0075
    assert out["main_breadth_amt"] == 0.63

    df.loc[pd.Timestamp("2026-10-28"), "main_breadth"] = np.nan
    assert latest_block(df)["main_breadth"] is None
