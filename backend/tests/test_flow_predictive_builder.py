"""资金流预测力产物的纯统计口径测试（不触盘、不加载真实数据）。"""

from __future__ import annotations

import numpy as np
import pandas as pd

from backend.scripts.market_analysis.build_flow_predictive import _deciles, _pattern


def _frames(values: np.ndarray) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    idx = pd.Index(["20260102"])
    cols = [f"s{i}" for i in range(values.shape[1])]
    score = pd.DataFrame(values, index=idx, columns=cols)
    rel = pd.DataFrame(values - 10.0, index=idx, columns=cols)
    mask = pd.DataFrame(True, index=idx, columns=cols)
    return score, rel, mask


def test_deciles_monotone_and_counts():
    score, rel, mask = _frames(np.arange(20, dtype=float).reshape(1, -1))
    rows = _deciles(score, rel, mask, h=1)
    assert [r["q"] for r in rows] == [1, 2, 3, 4, 5]
    assert sum(r["n"] for r in rows) == 20
    means = [r["mean"] for r in rows]
    assert means == sorted(means)
    assert rows[0]["date_win"] == 0.0
    assert rows[-1]["date_win"] == 1.0


def test_pattern_min_n_and_yearly():
    # 2 天 × 300 只 = 600 事件，全部正收益
    idx = pd.Index(["20260102", "20260105"])
    cols = [f"s{i}" for i in range(300)]
    rel = pd.DataFrame(1.0, index=idx, columns=cols)
    mask = pd.DataFrame(True, index=idx, columns=cols)
    sig = pd.DataFrame(True, index=idx, columns=cols)
    st = _pattern(sig, rel, mask, h=5, label="all")
    assert st is not None and st["n"] == 600
    assert st["date_win"] == 1.0 and st["date_mean"] > 0
    assert st["yearly"] == {2026: 1.0}

    # 事件数不足 → None（不拿小样本冒充）
    small_sig = pd.DataFrame(False, index=idx, columns=cols)
    small_sig.iloc[0, :10] = True
    assert _pattern(small_sig, rel, mask, h=5, label="small") is None
