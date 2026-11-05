"""D3 训练缓存与 load_data 的集成测试。

用临时 QuantDB 因子源跑两次 load_data：
1. 第一次 miss → 正常读盘并落缓存；
2. 第二次同源/同特征/同窗口 → 命中缓存（把 read_range 打桩成抛错也能返回）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

TRAINING_ROOT = Path(__file__).resolve().parents[2] / "docker" / "training"
sys.path.insert(0, str(TRAINING_ROOT))

from data import loading as L  # noqa: E402


def _write_source(root: Path, days: int = 60) -> None:
    base = root / "6_ml_datasets" / "test_factors"
    dates = pd.bdate_range("2025-01-02", periods=days)
    for idx, day in enumerate(dates):
        part = base / f"dt={day.strftime('%Y%m%d')}"
        part.mkdir(parents=True)
        frame = pd.DataFrame(
            {
                "symbol": ["600000.SH", "600001.SH", "000001.SZ", "000002.SZ"],
                "time": [day.strftime("%Y%m%d")] * 4,
                "open": [10.0 + idx * 0.01] * 4,
                "high": [10.2 + idx * 0.01] * 4,
                "low": [9.8 + idx * 0.01] * 4,
                "close": [10.1 + idx * 0.01] * 4,
                "volume": [1_000_000.0] * 4,
                "amount": [10_000_000.0] * 4,
                "mom_ret_1d": [0.001] * 4,
                "mom_ret_5d": [0.005] * 4,
                "is_st": [0] * 4,
                "f1": [float(idx)] * 4,
                "f2": [float(idx) * 2] * 4,
            }
        )
        frame.to_parquet(part / "data.parquet", index=False)


def _call(root: Path, cache: Path):
    return L.load_data(
        "2025-01-02",
        "2025-03-20",
        ["f1", "f2"],
        target_horizon_days=1,
        target_mode="return",
        cache_dir=str(cache),
        local_dir=str(root),
        market="CN",
        factor_source="test_factors",
        quantdb_dir=str(root),
        pool_symbols=["SH600000", "SZ000001"],
    )


def test_load_data_writes_and_hits_cache(tmp_path: Path, monkeypatch):
    root = tmp_path / "quantdb"
    cache = tmp_path / "cache"
    _write_source(root)

    first_df, first_features = _call(root, cache)
    assert not first_df.empty
    assert first_features == ["f1", "f2"]
    cached_files = list(cache.glob("train_*.parquet"))
    assert len(cached_files) == 1

    # 命中路径不应再调用 read_range
    from backend.services.engine.data_platform.quantdb_factor_reader import (
        QuantDBFactorReader,
    )

    def _boom(*args, **kwargs):
        raise AssertionError("cache hit should not call read_range")

    monkeypatch.setattr(QuantDBFactorReader, "read_range", _boom)

    second_df, second_features = _call(root, cache)
    assert second_features == first_features
    pd.testing.assert_frame_equal(first_df, second_df)
