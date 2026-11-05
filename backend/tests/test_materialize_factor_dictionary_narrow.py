"""V5 宽表按需物化（D1）测试。"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pandas as pd
import pytest

from backend.scripts import materialize_factor_dictionary_narrow as m


def _write_source(root: Path, dt: str, rows: int = 3) -> Path:
    target = root / "6_ml_datasets" / "factor_dictionary" / f"dt={dt}"
    target.mkdir(parents=True, exist_ok=True)
    frame = pd.DataFrame(
        {
            "symbol": [f"60000{i}.SH" for i in range(rows)],
            "date": [dt] * rows,
            "fun_roe": [1.0 + i for i in range(rows)],
            "mom_5": [0.1 * i for i in range(rows)],
            "other_col": [9.9] * rows,
        }
    )
    path = target / "data.parquet"
    frame.to_parquet(path, index=False)
    return path


def _source(root: Path) -> None:
    _write_source(root, "20240102")
    _write_source(root, "20240103")


def test_dry_run_does_not_write(tmp_path: Path):
    _source(tmp_path)

    result = m.materialize(
        tmp_path, features=["mom_5", "fun_roe"], start="20240101", end="20240131"
    )

    assert result["status"] == "dry_run"
    assert result["source_partitions"] == 2
    assert result["features"] == ["fun_roe", "mom_5"]  # 排序去重
    assert not (
        tmp_path / "6_ml_datasets" / f"{m.OUT_PREFIX}{result['set_id']}"
    ).exists()


def test_apply_writes_narrow_library_and_generation(tmp_path: Path):
    _source(tmp_path)

    result = m.materialize(
        tmp_path,
        features=["fun_roe", "mom_5"],
        start="20240101",
        end="20240131",
        apply=True,
    )

    assert result["status"] == "applied"
    out = Path(result["out_dir"])
    assert (out / "dt=20240102" / "data.parquet").is_file()
    assert (out / "dt=20240103" / "data.parquet").is_file()
    frame = pd.read_parquet(out / "dt=20240103" / "data.parquet")
    assert list(frame.columns) == ["symbol", "date", "fun_roe", "mom_5"]
    assert "other_col" not in frame.columns
    assert result["rows"] == 6

    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["features"] == ["fun_roe", "mom_5"]
    assert manifest["partitions_written"] == 2
    generation = json.loads((out / "generation.json").read_text())
    active = json.loads((out / "active.json").read_text())
    assert generation["status"] == "active"
    assert active["generation_id"] == generation["generation_id"]


def test_second_apply_is_idempotent(tmp_path: Path):
    _source(tmp_path)
    first = m.materialize(
        tmp_path, features=["fun_roe"], start="20240101", end="20240131", apply=True
    )
    manifest_before = (Path(first["out_dir"]) / "manifest.json").read_text()

    second = m.materialize(
        tmp_path, features=["fun_roe"], start="20240101", end="20240131", apply=True
    )

    assert second["status"] == "up_to_date"
    assert (Path(first["out_dir"]) / "manifest.json").read_text() == manifest_before


def test_source_change_requires_force(tmp_path: Path):
    _source(tmp_path)
    first = m.materialize(
        tmp_path, features=["fun_roe"], start="20240101", end="20240131", apply=True
    )
    # 改最新分区（size/mtime 变化 → source stamp 变化）
    path = _write_source(tmp_path, "20240103", rows=4)
    os.utime(path, (1, 1))

    with pytest.raises(RuntimeError, match="--force"):
        m.materialize(
            tmp_path, features=["fun_roe"], start="20240101", end="20240131", apply=True
        )

    forced = m.materialize(
        tmp_path,
        features=["fun_roe"],
        start="20240101",
        end="20240131",
        apply=True,
        force=True,
    )
    assert forced["status"] == "applied"
    assert forced["out_dir"] == first["out_dir"]


def test_incomplete_marker_blocks_without_force(tmp_path: Path):
    _source(tmp_path)
    first = m.materialize(
        tmp_path, features=["fun_roe"], start="20240101", end="20240131", apply=True
    )
    out = Path(first["out_dir"])
    (out / "_INCOMPLETE").write_text("interrupted", encoding="utf-8")

    with pytest.raises(RuntimeError, match="_INCOMPLETE"):
        m.materialize(
            tmp_path, features=["fun_roe"], start="20240101", end="20240131", apply=True
        )

    forced = m.materialize(
        tmp_path,
        features=["fun_roe"],
        start="20240101",
        end="20240131",
        apply=True,
        force=True,
    )
    assert forced["status"] == "applied"
    assert not (out / "_INCOMPLETE").exists()


@pytest.mark.parametrize("bad", ["", "symbol", "bad-name"])
def test_invalid_features_rejected(bad: str):
    with pytest.raises(ValueError):
        m.normalize_features([bad])
