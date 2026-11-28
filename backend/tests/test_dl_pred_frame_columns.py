"""DL 全量预测帧必须携带与树模型相同的评估列。

回归背景（2026-11-28）：DLAdapter 原先只用 symbol/trade_date/label 造
pred_df；主模型是 DL 时多模型结果只剩 6 列，eval_report 退化为 label_rank、
tradability=False、residual_ic=None，前端图表缺数据。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

_TRAINING_DIR = Path(__file__).resolve().parents[2] / "docker" / "training"
if str(_TRAINING_DIR) not in sys.path:
    sys.path.insert(0, str(_TRAINING_DIR))

from data.pred_frame import merge_full_pred  # noqa: E402


def _frame() -> pd.DataFrame:
    dates = pd.to_datetime(
        ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08"]
    )
    return pd.DataFrame(
        {
            "symbol": ["SH600000"] * 4,
            "trade_date": dates,
            "label": np.array([0.1, 0.2, 0.3, 0.4], dtype=np.float32),
            "label_return": np.array([0.01, 0.02, 0.03, 0.04], dtype=np.float32),
            "open": np.array([10.0, 10.1, 10.2, 10.3], dtype=np.float32),
            "close": np.array([10.1, 10.2, 10.3, 10.4], dtype=np.float32),
            "fun_float_mv": np.array([1e9, 1.1e9, 1.2e9, 1.3e9], dtype=np.float32),
            "turn_20": np.array([1.0, 1.1, 1.2, 1.3], dtype=np.float32),
            "label_return_h5": np.array([0.05, 0.06, 0.07, 0.08], dtype=np.float32),
        }
    )


def test_merge_full_pred_keeps_all_eval_columns() -> None:
    df = _frame()
    y = df[["symbol", "trade_date"]].copy()
    y["pred"] = np.array([0.01, 0.02, 0.03, 0.04], dtype=np.float32)
    val = df.iloc[2:3]
    test = df.iloc[3:4]

    out = merge_full_pred(df, y, val, test)

    for col in (
        "label_return",
        "open",
        "close",
        "fun_float_mv",
        "turn_20",
        "label_return_h5",
        "pred",
        "split",
    ):
        assert col in out.columns, col
    assert out["split"].tolist() == ["train", "train", "valid", "test"]
    assert out["pred"].notna().all()


def test_merge_full_pred_skips_absent_optional_columns() -> None:
    df = _frame().drop(columns=["open", "close", "fun_float_mv"])
    y = df[["symbol", "trade_date"]].copy()
    y["pred"] = np.array([0.01, 0.02, 0.03, 0.04], dtype=np.float32)

    out = merge_full_pred(df, y, df.iloc[2:3], df.iloc[3:4])

    assert "open" not in out.columns
    assert "close" not in out.columns
    assert "fun_float_mv" not in out.columns
    assert "label_return" in out.columns
    assert "label_return_h5" in out.columns
