"""``extend_fused_dataset`` 的单元测试：股票池选择、左连接、缺失列 NaN、幂等跳过。"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from backend.scripts.extend_fused_dataset import (
    build_partition,
    extend_all,
    extend_dataset,
    resolve_universe_source,
)


def _write(library: Path, day: str, frame: pd.DataFrame) -> None:
    target = library / f"dt={day}"
    target.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(target / "data.parquet", index=False)


def _dataset(tmp_path: Path, features: list[str], sources: dict[str, str]) -> Path:
    root = tmp_path / "fused_test"
    root.mkdir()
    (root / "meta.json").write_text(
        json.dumps({"features": features, "sources": sources}, ensure_ascii=False),
        encoding="utf-8",
    )
    return root


def test_resolve_universe_source_picks_library_with_most_features(tmp_path: Path) -> None:
    # Arrange：lib_a 覆盖 2 个特征，lib_b 覆盖 1 个
    sources = {
        "a": str(tmp_path / "lib_a"),
        "b": str(tmp_path / "lib_a"),
        "c": str(tmp_path / "lib_b"),
    }

    # Act
    picked = resolve_universe_source(sources, ["a", "b", "c"])

    # Assert
    assert picked == str(tmp_path / "lib_a")


def test_build_partition_left_joins_and_fills_missing_with_nan(tmp_path: Path) -> None:
    # Arrange：lib_a 是股票池（2 只），lib_b 只有 SH1 的 c
    lib_a = tmp_path / "lib_a"
    lib_b = tmp_path / "lib_b"
    _write(
        lib_a,
        "20261124",
        pd.DataFrame({"symbol": ["SH1", "SH2"], "a": [1.0, 2.0], "b": [3.0, 4.0]}),
    )
    _write(lib_b, "20261124", pd.DataFrame({"symbol": ["SH1"], "c": [5.0]}))
    root = _dataset(
        tmp_path,
        ["a", "b", "c"],
        {"a": str(lib_a), "b": str(lib_a), "c": str(lib_b)},
    )

    # Act
    result = build_partition(
        root,
        "20261124",
        ["a", "b", "c"],
        {"a": str(lib_a), "b": str(lib_a), "c": str(lib_b)},
        str(lib_a),
    )

    # Assert
    assert result["status"] == "written"
    assert result["rows"] == 2
    frame = pd.read_parquet(root / "dt=20261124" / "data.parquet").set_index("symbol")
    assert frame.loc["SH1", "c"] == 5.0
    assert pd.isna(frame.loc["SH2", "c"])
    assert frame.loc["SH2", "a"] == 2.0
    assert set(frame["dt"]) == {20261124}


def test_build_partition_marks_whole_missing_library_as_nan(tmp_path: Path) -> None:
    # Arrange：lib_b 缺 20261124 分区
    lib_a = tmp_path / "lib_a"
    lib_b = tmp_path / "lib_b"
    _write(lib_a, "20261124", pd.DataFrame({"symbol": ["SH1"], "a": [1.0]}))
    root = _dataset(
        tmp_path,
        ["a", "c"],
        {"a": str(lib_a), "c": str(lib_b)},
    )

    # Act
    result = build_partition(
        root,
        "20261124",
        ["a", "c"],
        {"a": str(lib_a), "c": str(lib_b)},
        str(lib_a),
    )

    # Assert
    assert result["missing_features"] == ["c"]
    frame = pd.read_parquet(root / "dt=20261124" / "data.parquet")
    assert pd.isna(frame.loc[0, "c"])


def test_build_partition_skips_existing_without_force(tmp_path: Path) -> None:
    # Arrange：已有分区写入哨兵值
    lib_a = tmp_path / "lib_a"
    _write(lib_a, "20261124", pd.DataFrame({"symbol": ["SH1"], "a": [1.0]}))
    root = _dataset(tmp_path, ["a"], {"a": str(lib_a)})
    _write(root, "20261124", pd.DataFrame({"symbol": ["SH1"], "a": [99.0], "dt": [20261124]}))

    # Act：默认跳过
    skipped = build_partition(
        root, "20261124", ["a"], {"a": str(lib_a)}, str(lib_a)
    )
    # Act：force 覆盖
    forced = build_partition(
        root, "20261124", ["a"], {"a": str(lib_a)}, str(lib_a), force=True
    )

    # Assert
    assert skipped["status"] == "skipped"
    assert forced["status"] == "written"
    assert pd.read_parquet(root / "dt=20261124" / "data.parquet").loc[0, "a"] == 1.0


def test_extend_dataset_only_fills_days_after_existing_and_updates_meta(
    tmp_path: Path,
) -> None:
    # Arrange：股票池来源库有两天，数据集只建了第一天
    lib_a = tmp_path / "lib_a"
    for day in ("20261124", "20261125"):
        _write(lib_a, day, pd.DataFrame({"symbol": ["SH1"], "a": [1.0]}))
    root = _dataset(tmp_path, ["a"], {"a": str(lib_a)})
    _write(root, "20261124", pd.DataFrame({"symbol": ["SH1"], "a": [1.0], "dt": [20261124]}))

    # Act
    result = extend_dataset(root)

    # Assert：只补 20261125，不重写已有日
    assert result["status"] == "ok"
    assert result["targets"] == 1
    assert result["written"] == 1
    assert (root / "dt=20261125" / "data.parquet").is_file()
    meta = json.loads((root / "meta.json").read_text(encoding="utf-8"))
    assert meta["last_extended_dt"] == "20261125"


def test_extend_dataset_dry_run_does_not_write(tmp_path: Path) -> None:
    # Arrange
    lib_a = tmp_path / "lib_a"
    _write(lib_a, "20261124", pd.DataFrame({"symbol": ["SH1"], "a": [1.0]}))
    root = _dataset(tmp_path, ["a"], {"a": str(lib_a)})

    # Act
    result = extend_dataset(root, dry_run=True)

    # Assert
    assert result["status"] == "dry_run"
    assert result["targets"] == ["20261124"]
    assert not (root / "dt=20261124").exists()


def test_extend_all_ignores_directories_without_meta(tmp_path: Path) -> None:
    # Arrange
    base = tmp_path / "6_ml_datasets"
    (base / "fused_no_meta").mkdir(parents=True)
    lib_a = tmp_path / "lib_a"
    _write(lib_a, "20261124", pd.DataFrame({"symbol": ["SH1"], "a": [1.0]}))
    root = base / "fused_ok"
    root.mkdir()
    (root / "meta.json").write_text(
        json.dumps({"features": ["a"], "sources": {"a": str(lib_a)}}),
        encoding="utf-8",
    )

    # Act
    results = extend_all(tmp_path)

    # Assert
    assert [item["dataset"].split("/")[-1] for item in results] == ["fused_ok"]
    assert results[0]["status"] == "ok"


def test_beat_schedule_registers_fused_extend_entry(tmp_path: Path | None = None) -> None:
    # Arrange / Act
    from backend.services.engine.qlib_app.celery_config import beat_schedule

    # Assert：04:30（CUSTOM 03:00 重建之后、08:00 盘前推理之前）
    entry = beat_schedule["fused-dataset-extend-daily"]
    assert entry["task"] == "engine.tasks.extend_fused_datasets"
