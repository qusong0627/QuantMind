"""TTM 多数据集修复脚本测试（2026-09-17 扩面）：逐格替换规则/两种 mv 来源/保守性。

修复纪律：仅 |源−重算|/|重算| > 25% 才替换；无法重算（缺季报/新股）保留原值；
l1 系的 mv 取 valuation（fun_total_mv 单位不可信）。本测试锁死这三条。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backend.scripts.repair_ttm_datasets import DATASETS, _fix_frame


def _ttm_frame():
    return pd.DataFrame(
        {
            "symbol": ["600036.SH", "000001.SZ", "300750.SZ"],
            "np_ttm": [1.0e10, 4.3e10, np.nan],
            "rev_ttm": [3.0e10, 8.0e10, 1.0e10],
            "total_mv": [6.0e10, 2.3e11, 5.0e11],
        }
    )


@pytest.mark.unit
def test_fix_frame_threshold_and_conservative():
    """>25% 才替换；偏差小保留原值；无法重算（NaN）保留原值。"""
    spec = DATASETS["features_daily"]
    part = pd.DataFrame(
        {
            "symbol": ["600036.SH", "000001.SZ", "300750.SZ"],
            "net_profit_ttm": [1.0e10, 6.4e9, 7.0e9],   # 600036 一致；000001 坏(6.4e9 vs 4.3e10)；300750 无法重算
            "revenue_ttm": [3.0e10, 8.0e10, 1.0e10],
            "pe_ttm": [6.0, 108.96, 5.0],                # 600036 对；000001 坏；300750 无法重算
            "ps_ttm": [2.0, 2.875, 50.0],
        }
    )
    fixed, counts = _fix_frame(part, _ttm_frame(), spec)
    # 阈值：600036 全部保留；000001 np/pe 被替换（rev/ps 一致保留）
    assert fixed.loc[0, "net_profit_ttm"] == pytest.approx(1.0e10)
    assert fixed.loc[1, "net_profit_ttm"] == pytest.approx(4.3e10)
    assert fixed.loc[1, "pe_ttm"] == pytest.approx(2.3e11 / 4.3e10, rel=1e-6)
    assert fixed.loc[2, "net_profit_ttm"] == pytest.approx(7.0e9)  # NaN 重算 → 保守保留
    assert fixed.loc[2, "pe_ttm"] == pytest.approx(5.0)
    assert counts["net_profit_ttm"] == 1 and counts["pe_ttm"] == 1
    assert counts["revenue_ttm"] == 0 and counts["ps_ttm"] == 0


@pytest.mark.unit
def test_fix_frame_l1_uses_valuation_mv_and_ep_inverse():
    """l1 系：fun_pe=mv/np_ttm（valuation mv）、fun_ep=1/fun_pe；坏值替换、好值保留。"""
    spec = DATASETS["l1_l2_factors"]
    part = pd.DataFrame(
        {
            "symbol": ["600036.SH", "000001.SZ"],
            "fun_pe": [6.0, 108.96],
            "fun_ep": [1 / 6.0, 0.0092],
        }
    )
    fixed, counts = _fix_frame(part, _ttm_frame(), spec)
    assert fixed.loc[0, "fun_pe"] == pytest.approx(6.0)            # 5.9999… 保留（<25%）
    assert fixed.loc[1, "fun_pe"] == pytest.approx(2.3e11 / 4.3e10, rel=1e-6)
    assert fixed.loc[1, "fun_ep"] == pytest.approx(4.3e10 / 2.3e11, rel=1e-6)
    assert counts == {"fun_pe": 1, "fun_ep": 1}


@pytest.mark.unit
def test_fix_frame_keeps_signs_for_loss_makers():
    """亏损股（TTM 为负）：pe 为负是合法值，重算同样为负 → 不误伤。"""
    spec = DATASETS["features_daily"]
    ttm = pd.DataFrame(
        {"symbol": ["000002.SZ"], "np_ttm": [-8.8e10], "rev_ttm": [1.0e11], "total_mv": [8.0e10]}
    )
    part = pd.DataFrame(
        {"symbol": ["000002.SZ"], "net_profit_ttm": [-8.8e10], "revenue_ttm": [1.0e11],
         "pe_ttm": [8.0e10 / -8.8e10], "ps_ttm": [0.8]}
    )
    fixed, counts = _fix_frame(part, ttm, spec)
    assert counts == {"net_profit_ttm": 0, "revenue_ttm": 0, "pe_ttm": 0, "ps_ttm": 0}
