"""退市股因子行级追加（A2 方案 1）测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from backend.scripts import delisted_factor_inject as inj


def _write_official(root: Path) -> Path:
    path = root / "6_ml_datasets" / "factor_defs" / "dt=20260710" / "data.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(
        {
            "symbol": ["000001.SZ", "000002.SZ", "000006.SZ"],
            "time": pd.to_datetime(["2026-07-10"] * 3),
            "risk_fac_momentum": [0.1, 0.2, 0.3],
            "f1": [1.0, 2.0, 3.0],
            "f2": [10.0, 20.0, 30.0],
        }
    ).to_parquet(path, index=False)
    return path


def _write_staging(root: Path) -> Path:
    path = (
        root / "6_ml_datasets" / "_staging_factor_defs" / "dt=20260710" / "data.parquet"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(
        {
            "symbol": ["000001.SZ", "000002.SZ", "000006.SZ", "000004.SZ", "000005.SZ"],
            "time": pd.to_datetime(["2026-07-10"] * 5),
            "risk_fac_momentum": [9.9, 9.9, 9.9, 0.4, 0.5],
            "f1": [1.0, 2.0, 3.0, 4.0, 5.0],
            "f2": [10.0, 20.0, 30.0, 40.0, 50.0],
        }
    ).to_parquet(path, index=False)
    return path


def test_dry_run_does_not_write(tmp_path: Path):
    path = _write_official(tmp_path)
    _write_staging(tmp_path)

    result = inj.inject_date(
        tmp_path,
        library="factor_defs",
        dt="20260710",
        staging_root=tmp_path / "6_ml_datasets" / "_staging_factor_defs",
    )

    assert result.status == "dry_run"
    assert result.added == ["000004.SZ", "000005.SZ"]
    assert len(pd.read_parquet(path)) == 3


def test_apply_appends_only_new_symbols_and_preserves_official(tmp_path: Path):
    path = _write_official(tmp_path)
    _write_staging(tmp_path)

    result = inj.inject_date(
        tmp_path,
        library="factor_defs",
        dt="20260710",
        staging_root=tmp_path / "6_ml_datasets" / "_staging_factor_defs",
        exclude_cols=["risk_fac_momentum"],
        apply=True,
    )

    assert result.status == "applied"
    assert result.rows_before == 3 and result.rows_after == 5
    frame = pd.read_parquet(path).set_index("symbol")
    # 官方三行原样保留（快通道值没有覆盖）
    assert frame.loc["000001.SZ", "risk_fac_momentum"] == 0.1
    assert frame.loc["000001.SZ", "f1"] == 1.0
    # 新增退市股：f1/f2 来自 staging，长窗列强制 NaN
    assert frame.loc["000004.SZ", "f1"] == 4.0
    assert pd.isna(frame.loc["000004.SZ", "risk_fac_momentum"])
    assert pd.isna(frame.loc["000005.SZ", "risk_fac_momentum"])
    assert (
        tmp_path
        / "6_ml_datasets"
        / inj.BACKUP_DIR
        / "factor_defs"
        / "dt=20260710"
        / "data.parquet"
    ).is_file()


def test_second_apply_is_up_to_date(tmp_path: Path):
    path = _write_official(tmp_path)
    _write_staging(tmp_path)
    kwargs = {
        "library": "factor_defs",
        "dt": "20260710",
        "staging_root": tmp_path / "6_ml_datasets" / "_staging_factor_defs",
        "exclude_cols": ["risk_fac_momentum"],
        "apply": True,
    }
    first = inj.inject_date(tmp_path, **kwargs)
    assert first.status == "applied"
    second = inj.inject_date(tmp_path, **kwargs)

    assert second.status == "up_to_date"
    assert second.added == []
    assert len(pd.read_parquet(path)) == 5


def test_missing_partitions_are_skipped(tmp_path: Path):
    assert (
        inj.inject_date(
            tmp_path,
            library="factor_defs",
            dt="20260710",
            staging_root=tmp_path / "staging",
            apply=True,
        ).status
        == "official_missing"
    )
    _write_official(tmp_path)
    assert (
        inj.inject_date(
            tmp_path,
            library="factor_defs",
            dt="20260710",
            staging_root=tmp_path / "staging",
            apply=True,
        ).status
        == "staging_missing"
    )


def test_manifest_records_added_symbols(tmp_path: Path):
    _write_official(tmp_path)
    _write_staging(tmp_path)
    result = inj.inject_date(
        tmp_path,
        library="factor_defs",
        dt="20260710",
        staging_root=tmp_path / "6_ml_datasets" / "_staging_factor_defs",
        apply=True,
    )
    inj._append_manifest(
        tmp_path,
        {"library": "factor_defs", **result.to_dict()},
    )
    lines = (
        (tmp_path / inj.MANIFEST_REL).read_text(encoding="utf-8").strip().splitlines()
    )
    payload = json.loads(lines[-1])
    assert payload["added_count"] == 2
    assert payload["added"] == ["000004.SZ", "000005.SZ"]
