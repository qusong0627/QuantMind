"""WP3 样本权重：可交易 mask / ST PIT / 时间衰减（纯函数 + 临时目录）。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pandas as pd
import pytest

_MODULE_PATH = (
    Path(__file__).resolve().parents[2] / "docker" / "training" / "sample_weights.py"
)
_spec = importlib.util.spec_from_file_location("qm_sample_weights", _MODULE_PATH)
sw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sw)


def _frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "symbol": ["SH600000", "SH600000", "SZ000001"],
            "trade_date": pd.to_datetime(["2024-01-02", "2024-06-03", "2024-01-02"]),
        }
    )


def test_to_suffix_converts_prefix():
    out = sw.to_suffix(pd.Series(["SH600000", "000001.SZ"]))
    assert list(out) == ["600000.SH", "000001.SZ"]


def test_time_decay_only_when_requested():
    frame = _frame()
    w = sw.build_weights(
        frame, quantdb_dir=None, config={"time_decay_half_life_days": 365}, with_decay=True
    )
    assert w[1] == pytest.approx(1.0)
    assert 0.6 < w[0] < 0.8  # 153 天半衰一年
    w_val = sw.build_weights(
        frame, quantdb_dir=None, config={"time_decay_half_life_days": 365}, with_decay=False
    )
    assert list(w_val) == [1.0, 1.0, 1.0]


def test_tradeable_mask_zeroes_blocked(tmp_path: Path):
    mask_dir = tmp_path / "5_technical_derived" / "trade_mask"
    mask_dir.mkdir(parents=True)
    pd.DataFrame(
        {
            "dt": [20240102, 20240102],
            "symbol": ["600000.SH", "000001.SZ"],
            "blocked_long": [True, False],
            "blocked_short": [False, False],
        }
    ).to_parquet(mask_dir / "trade_mask.parquet")

    w = sw.build_weights(
        _frame(), quantdb_dir=tmp_path, config={"tradeable_mask": True}, with_decay=False
    )
    assert w[0] == 0.0
    assert w[1] == 1.0
    assert w[2] == 1.0


def test_st_cache_zeroes_rows(tmp_path: Path):
    cache = tmp_path / "factor_research" / "st_pit_cache"
    cache.mkdir(parents=True)
    pd.DataFrame(
        {"time": ["20240102", "20240103"], "is_st": [True, False]}
    ).to_parquet(cache / "000001.SZ.parquet")

    w = sw.build_weights(
        _frame(), quantdb_dir=tmp_path, config={"st": True}, with_decay=False
    )
    assert w[2] == 0.0
    assert w[0] == 1.0


def test_missing_mask_fails_closed(tmp_path: Path):
    with pytest.raises(RuntimeError):
        sw.build_weights(
            _frame(), quantdb_dir=tmp_path, config={"tradeable_mask": True}, with_decay=False
        )


def test_non_cn_rejected(tmp_path: Path):
    with pytest.raises(RuntimeError):
        sw.build_weights(
            _frame(),
            quantdb_dir=tmp_path,
            config={"tradeable_mask": True},
            market="HK",
            with_decay=False,
        )
