"""daily_forward canonical 收口脚本：dry-run / apply / restore 的不可逆性护栏。"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from backend.scripts.quanthk_canonicalize_kline import (
    BACKUP_REL,
    REL_DIR,
    REMOVED_REL,
    SUMMARY_REL,
    _restore,
    apply,
)


def _write_partition(root: Path, dt: str, rows: list[dict]) -> Path:
    path = root / REL_DIR / f"dt={dt}" / "data.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_parquet(path, index=False)
    return path


def _rows() -> list[dict]:
    return [
        {
            "symbol": "0755.HK",
            "time": "2025-03-10",
            "close": 0.011,
            "release_id": "akshare",
            "published_at": "2026-08-24T22:00:00",
        },
        {
            "symbol": "0755.HK",
            "time": "2025-03-10",
            "close": 1.1,
            "release_id": "paid_hk",
            "published_at": "2026-08-09T13:00:00",
        },
    ]


def test_dry_run_does_not_write(tmp_path: Path):
    # Arrange
    path = _write_partition(tmp_path, "20250310", _rows())

    # Act
    summary = apply(tmp_path, dry_run=True)

    # Assert
    assert summary["partitions_changed"] == 1
    assert summary["rows_before"] == 2
    assert summary["rows_after"] == 1
    assert summary["rows_removed"] == 1
    assert len(pd.read_parquet(path)) == 2
    assert not (tmp_path / BACKUP_REL).exists()


def test_apply_leaves_one_row_and_restore_reverts(tmp_path: Path):
    # Arrange
    path = _write_partition(tmp_path, "20250310", _rows())

    # Act
    summary = apply(tmp_path, dry_run=False)

    # Assert
    out = pd.read_parquet(path)
    assert summary["rows_removed"] == 1
    assert len(out) == 1
    assert out.iloc[0]["release_id"] == "paid_hk"
    assert out.iloc[0]["close"] == 1.1
    assert (tmp_path / BACKUP_REL / "dt=20250310" / "data.parquet").exists()
    assert (tmp_path / REMOVED_REL / "dt=20250310" / "data.parquet").exists()
    assert (tmp_path / SUMMARY_REL).exists()
    saved = json.loads((tmp_path / SUMMARY_REL).read_text(encoding="utf-8"))
    assert saved["rows_removed"] == 1

    # Act: restore
    restored = _restore(tmp_path)

    # Assert
    assert restored["restored_partitions"] == 1
    back = pd.read_parquet(path)
    assert len(back) == 2
    assert set(back["release_id"]) == {"paid_hk", "akshare"}


def test_apply_is_idempotent(tmp_path: Path):
    # Arrange
    path = _write_partition(tmp_path, "20250310", _rows())
    apply(tmp_path, dry_run=False)
    first = pd.read_parquet(path)

    # Act
    summary = apply(tmp_path, dry_run=False)

    # Assert
    assert summary["partitions_changed"] == 0
    assert summary["partitions_skipped"] == 1
    pd.testing.assert_frame_equal(first, pd.read_parquet(path))


def test_clean_partition_is_skipped(tmp_path: Path):
    # Arrange
    _write_partition(
        tmp_path,
        "20261113",
        [
            {
                "symbol": "0001.HK",
                "time": "2026-11-13",
                "close": 60.0,
                "release_id": "akshare",
                "published_at": "2026-11-13T20:00:00",
            },
        ],
    )

    # Act
    summary = apply(tmp_path, dry_run=False)

    # Assert
    assert summary["partitions_changed"] == 0
    assert summary["partitions_skipped"] == 1
