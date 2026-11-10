"""avoid-bottom overlay 的方向回归测试。

pred 越大越好：必须剔除尾部（低 pred），持有前 (1-x)。2026-11-10 首版写反过，
此测试锁住方向，避免把「剔除头部」当成「避雷」。
"""

from __future__ import annotations

import pandas as pd
import pytest

from backend.scripts.model_eval.overlay_backtest import split_by_pred


@pytest.mark.unit
def test_split_by_pred_holds_high_scores_and_excludes_tail():
    frame = pd.DataFrame({"sym": list("abcde"), "pred": [1, 5, 3, 2, 4]})
    hold, bottom = split_by_pred(frame, 0.4)
    assert list(hold["sym"]) == ["b", "e", "c"]
    assert list(bottom["sym"]) == ["d", "a"]


@pytest.mark.unit
def test_split_by_pred_zero_exclude_keeps_all():
    frame = pd.DataFrame({"sym": list("abc"), "pred": [3, 1, 2]})
    hold, bottom = split_by_pred(frame, 0.0)
    assert len(hold) == 3
    assert bottom.empty
