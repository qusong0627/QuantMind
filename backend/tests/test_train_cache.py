"""训练数据缓存（D3）单元测试。"""

from __future__ import annotations

import os
from pathlib import Path

import pandas as pd
import pytest

from backend.shared import train_cache as c


def _frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "symbol": ["SH600000", "SZ000001"],
            "trade_date": ["2026-01-05", "2026-01-05"],
            "label": [0.1, -0.2],
            "f1": [1.0, 2.0],
            "f2": [3.0, 4.0],
        }
    )


def _key(**overrides) -> str:
    params = {
        "market": "CN",
        "factor_source": "l1_factors",
        "source_stamp_value": "schema|20260105|10|dt=20260105:100:1",
        "train_start": "2020-01-01",
        "train_end": "2025-12-31",
        "valid_end": "2025-06-30",
        "test_end": "2025-12-31",
        "horizon": 5,
        "target_mode": "return",
        "features": ["f1", "f2"],
        "factor_field_sources": {"f1": "lib:f1"},
        "pool_symbols": ["SH600000", "SZ000001"],
        "industry_as_feature": False,
        "code_version": "git-abc",
    }
    params.update(overrides)
    return c.build_cache_key(**params)


def test_source_stamp_uses_latest_partition(tmp_path: Path):
    src = tmp_path / "l1_factors"
    for dt in ("20260104", "20260105"):
        part = src / f"dt={dt}"
        part.mkdir(parents=True)
        (part / "data.parquet").write_bytes(b"x" * (10 if dt == "20260104" else 20))
    status = type("S", (), {"schema_hash": "h", "max_date": "2026-01-05", "files": 2})()

    stamp = c.source_stamp(status, src)

    assert "h|2026-01-05|2|" in stamp
    assert "dt=20260105:20:" in stamp


def test_cache_key_is_deterministic_and_sensitive():
    base = _key()
    assert base == _key()
    assert base != _key(features=["f1"])
    assert base != _key(pool_symbols=["SH600000"])
    assert base != _key(source_stamp_value="other")
    assert base != _key(horizon=1)
    assert base != _key(code_version="git-def")
    assert base != _key(factor_field_sources={"f1": "lib2:f1"})


def test_write_and_read_roundtrip(tmp_path: Path):
    frame = _frame()
    key = _key()
    path = c.write_cache(tmp_path, key, frame, ["f1", "f2"], max_total_gb=1)
    assert path is not None and path.is_file()

    got = c.read_cache(tmp_path, key)
    assert got is not None
    restored, features = got
    assert features == ["f1", "f2"]
    pd.testing.assert_frame_equal(frame, restored)


def test_miss_on_wrong_key_or_broken_files(tmp_path: Path):
    frame = _frame()
    key = _key()
    assert c.write_cache(tmp_path, key, frame, ["f1", "f2"]) is not None

    assert c.read_cache(tmp_path, "other-key") is None

    meta = tmp_path / f"train_{key}.json"
    meta.write_text("not-json", encoding="utf-8")
    assert c.read_cache(tmp_path, key) is None


def test_miss_when_feature_missing(tmp_path: Path):
    frame = _frame().drop(columns=["f2"])
    key = _key()
    c.write_cache(tmp_path, key, frame, ["f1", "f2"])
    # 手工把 meta 写回成声明 f2（模拟写侧列不全）
    assert c.read_cache(tmp_path, key) is None


def test_prune_removes_oldest(tmp_path: Path):
    old_key = _key(features=["f1"])
    new_key = _key(features=["f2"])
    old_path = c.write_cache(tmp_path, old_key, _frame(), ["f1"], max_total_gb=1)
    assert old_path is not None
    os.utime(old_path, (1, 1))  # 很旧

    # 上限 = 单文件大小的 1.5 倍：新文件能放下，两份放不下 → 旧文件被 LRU 清掉
    probe = tmp_path / "probe.parquet"
    _frame().to_parquet(probe, index=False)
    limit_gb = probe.stat().st_size * 1.5 / 1024**3
    probe.unlink()

    new_path = c.write_cache(tmp_path, new_key, _frame(), ["f2"], max_total_gb=limit_gb)

    assert new_path is not None
    assert not old_path.exists()


def test_oversize_single_file_not_cached(tmp_path: Path):
    key = _key()
    result = c.write_cache(tmp_path, key, _frame(), ["f1", "f2"], max_total_gb=1e-9)
    assert result is None
    assert not (tmp_path / f"train_{key}.parquet").exists()
