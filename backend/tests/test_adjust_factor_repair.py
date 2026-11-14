"""复权因子 step 修复：inf/异常拆并股不得污染 cumprod。"""

from __future__ import annotations

import numpy as np
import pandas as pd

from backend.shared.adjust_factor_repair import repair_factor_frame


def _factor(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows)


def _close(pairs: list[tuple[str, float]]) -> pd.Series:
    return pd.Series({pd.Timestamp(d): v for d, v in pairs}, dtype="float64")


def test_inf_step_is_replaced_and_factor_stays_finite():
    # Arrange
    df = _factor(
        [
            {"time": "2026-05-07", "adj_step": 1.0, "adj_factor": 1.0},
            {"time": "2026-05-08", "adj_step": np.inf, "adj_factor": np.inf},
            {"time": "2026-05-11", "adj_step": 1.0, "adj_factor": np.inf},
        ]
    )
    close = _close([("2026-05-07", 89.8), ("2026-05-08", 87.6), ("2026-05-11", 90.7)])

    # Act
    out, stats = repair_factor_frame(df, close)

    # Assert
    assert stats["repaired"] == 1
    assert stats["inf_after"] == 0
    assert np.isfinite(out["adj_factor"]).all()
    assert out["adj_step"].tolist() == [1.0, 1.0, 1.0]


def test_fake_3400_step_is_neutralized_when_canonical_is_flat():
    # Arrange
    df = _factor(
        [
            {"time": "2026-05-07", "adj_step": 1.0, "adj_factor": 0.918},
            {"time": "2026-05-08", "adj_step": 3400.0, "adj_factor": 3121.16},
        ]
    )
    close = _close([("2026-05-07", 89.8), ("2026-05-08", 87.6)])

    # Act
    out, _ = repair_factor_frame(df, close)

    # Assert
    assert out["adj_step"].tolist() == [1.0, 1.0]
    assert abs(out["adj_factor"].iloc[-1] - 0.918) < 1e-9


def test_real_100x_split_is_kept():
    # Arrange
    df = _factor(
        [
            {"time": "2020-04-20", "adj_step": 1.0, "adj_factor": 1.0},
            {"time": "2020-04-21", "adj_step": 100.0, "adj_factor": 100.0},
        ]
    )
    close = _close([("2020-04-20", 0.038), ("2020-04-21", 3.8)])

    # Act
    out, _ = repair_factor_frame(df, close)

    # Assert
    assert out["adj_step"].tolist() == [1.0, 100.0]
    assert out["adj_factor"].iloc[-1] == 100.0


def test_normal_dividend_step_is_kept():
    # Arrange
    df = _factor(
        [
            {"time": "2025-05-15", "adj_step": 1.0, "adj_factor": 1.0},
            {"time": "2025-05-16", "adj_step": 0.98, "adj_factor": 0.98},
        ]
    )
    close = _close([("2025-05-15", 100.0), ("2025-05-16", 100.0)])

    # Act
    out, _ = repair_factor_frame(df, close)

    # Assert
    assert out["adj_step"].tolist() == [1.0, 0.98]


def test_first_factor_is_preserved_as_base():
    # Arrange
    df = _factor(
        [
            {"time": "2026-05-08", "adj_step": 1.0, "adj_factor": 0.918},
            {"time": "2026-05-11", "adj_step": 1.0, "adj_factor": 0.918},
        ]
    )
    close = _close([("2026-05-08", 87.6), ("2026-05-11", 90.7)])

    # Act
    out, _ = repair_factor_frame(df, close)

    # Assert
    assert out["adj_factor"].iloc[0] == 0.918
    assert out["adj_factor"].iloc[-1] == 0.918


def test_missing_canonical_close_neutralizes_extreme_step():
    # Arrange
    df = _factor(
        [
            {"time": "2026-05-08", "adj_step": 3400.0, "adj_factor": 3400.0},
        ]
    )

    # Act
    out, _ = repair_factor_frame(df, pd.Series(dtype="float64"))

    # Assert
    assert out["adj_step"].tolist() == [1.0]
    assert out["adj_factor"].tolist() == [1.0]
