"""散户视角日频序列的纯函数测试（不加载真实 L2）。"""

from __future__ import annotations

import numpy as np
import pandas as pd

from backend.scripts.market_analysis.retail_force import latest_retail_block


def test_latest_retail_block_returns_latest_and_none_for_nan():
    df = pd.DataFrame(
        {
            "retail_ratio": [0.02, 0.0317],
            "retail_breadth": [0.55, 0.6354],
            "retail_breadth_amt": [0.5, 0.6827],
            "main_ratio": [0.006, 0.0075],
            "main_breadth": [0.55, 0.6506],
            "retail_net_yi": [20.0, 515.94],
            "retail_chase_corr": [0.2, 0.0603],
            "retail_chase": [0.01, 0.0087],
            "retail_panic": [0.0, 0.0089],
            "divergence": [-0.01, -0.0242],
        },
        index=pd.to_datetime(["2026-10-27", "2026-10-28"]),
    )
    out = latest_retail_block(df)
    assert out["date"] == "2026-10-28"
    assert out["retail_net_yi"] == 515.94
    assert out["retail_chase_corr"] == 0.0603
    df.loc[pd.Timestamp("2026-10-28"), "retail_breadth"] = np.nan
    assert latest_retail_block(df)["retail_breadth"] is None
