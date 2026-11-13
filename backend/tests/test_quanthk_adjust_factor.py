"""复权因子链（Yahoo Adj Close 口径）的契约测试。

锁三件事：
1. 以既有因子文件在重叠日的值做比例锚，历史段不变；
2. Yahoo 比例在除息日阶跃 → 新因子按总回报口径延续（不丢股息）；
3. 新行 symbol/time/adj_step 完整，可直接回写因子文件。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backend.scripts.quanthk_adjust_factor import _factor_from_yahoo, _update_factor


def _base() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "symbol": ["00700", "00700"],
            "time": [pd.Timestamp("2026-05-07").date(), pd.Timestamp("2026-05-08").date()],
            "close_raw": [477.4, 471.4],
            "preclose": [463.0, 477.4],
            "adj_step": [1.0, 1.0],
            "adj_factor": [0.9141169, 0.9141169],
        }
    )


def _yahoo() -> pd.DataFrame:
    # 2026-05-15 除息：Adj Close/Close 由 0.9885 阶跃到 1.0
    return pd.DataFrame(
        {
            "date": [
                pd.Timestamp("2026-05-08"),
                pd.Timestamp("2026-05-11"),
                pd.Timestamp("2026-05-15"),
                pd.Timestamp("2026-05-18"),
            ],
            "close": [471.4, 464.4, 456.4, 449.2],
            "ratio": [0.98848, 0.98848, 1.0, 1.0],
        }
    )


def test_update_factor_keeps_history_and_applies_dividend_step():
    base = _base()
    full, new = _update_factor("00700", base, _yahoo())
    assert full is not None and new is not None and len(new) == 3
    # 历史段原样保留
    hist = full[full["time"].astype(str) <= "2026-05-08"]
    assert hist["adj_factor"].tolist() == base["adj_factor"].tolist()
    # 重叠日比例锚：2026-05-08 因子不变
    assert full.loc[full["time"].astype(str) == "2026-05-08", "adj_factor"].iloc[0] == pytest.approx(
        0.9141169
    )
    # 除息日阶跃：1.0 / 0.98848 ≈ 1.01165
    new = new.sort_values("time")
    step = float(new.loc[new["time"].astype(str) == "2026-05-15", "adj_step"].iloc[0])
    assert step == pytest.approx(1.0 / 0.98848, rel=1e-4)
    # 新行 symbol 完整、无 NaN
    assert set(new["symbol"]) == {"00700"}
    assert new[["time", "adj_factor", "adj_step"]].notna().all().all()


def test_update_factor_no_new_dates_returns_base():
    base = _base()
    yahoo = _yahoo()
    yahoo = yahoo.loc[yahoo["date"] <= pd.Timestamp("2026-05-08")]
    full, new = _update_factor("00700", base, yahoo)
    assert full is not None
    assert new is None
    assert len(full) == len(base)


def test_update_factor_without_overlap_uses_first_ratio():
    base = _base()
    yahoo = pd.DataFrame(
        {
            "date": [pd.Timestamp("2026-06-01"), pd.Timestamp("2026-06-02")],
            "close": [450.0, 455.0],
            "ratio": [1.0, 1.0],
        }
    )
    full, new = _update_factor("00700", base, yahoo)
    assert new is not None and len(new) == 2
    # 无重叠：以 base 末值 / Yahoo 首个比例归一，因子保持常数
    assert new["adj_factor"].iloc[0] == pytest.approx(0.9141169)
    assert np.isfinite(new["adj_step"]).all()


def test_factor_from_yahoo_normalises_earliest_to_one():
    """无付费历史的新标的：Yahoo 全段建因子，最早=1，首行 step=1。"""
    yahoo = pd.DataFrame(
        {
            "date": [
                pd.Timestamp("2024-10-01"),
                pd.Timestamp("2025-05-15"),
                pd.Timestamp("2026-11-13"),
            ],
            "close": [10.0, 11.0, 12.0],
            "ratio": [0.9, 0.95, 1.0],
        }
    )
    f = _factor_from_yahoo("01234", yahoo)
    assert f is not None and len(f) == 3
    assert set(f["symbol"]) == {"01234"}
    assert f["adj_factor"].iloc[0] == pytest.approx(1.0)
    assert f["adj_factor"].iloc[-1] == pytest.approx(1.0 / 0.9)
    assert f["adj_step"].iloc[0] == pytest.approx(1.0)
    assert np.isnan(f["preclose"].iloc[0])
