"""QuantDB SDK 版本 / V5 列集契约（B2）测试。"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pandas as pd
import pytest

from backend.shared import quantdb_contract as c


@pytest.fixture(autouse=True)
def _clear_golden_cache():
    c.golden_columns.cache_clear()
    yield
    c.golden_columns.cache_clear()


def test_golden_v5_schema_has_571_unique_columns():
    cols = c.golden_columns()
    assert len(cols) == 571
    assert len(set(cols)) == 571
    assert {
        "symbol",
        "date",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "amount",
    } <= set(cols)


@pytest.mark.parametrize(
    "installed,ok",
    [
        ("0.5.0", True),
        ("0.5.9", True),
        ("v0.5.1", True),
        ("0.4.4", False),
        ("0.6.0", False),
        ("", False),
    ],
)
def test_sdk_version_check(monkeypatch, installed: str, ok: bool):
    monkeypatch.setattr(c, "sdk_version", lambda: installed)
    result = c.check_sdk_version()
    assert result["ok"] is ok
    assert result["expected"] == "0.5.0"


def _write_v5(path: Path, columns: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({col: [0.0] for col in columns}).to_parquet(path, index=False)


def test_schema_matches_golden(tmp_path: Path):
    path = tmp_path / "dt=20261104" / "data.parquet"
    _write_v5(path, list(c.golden_columns()))

    result = c.check_factor_dictionary_schema(path)

    assert result["available"] is True
    assert result["ok"] is True
    assert result["added"] == []
    assert result["removed"] == []
    assert result["actual_count"] == 571


def test_schema_drift_reports_added_and_removed(tmp_path: Path):
    cols = list(c.golden_columns())
    dropped = cols.pop(10)
    cols.append("x_brand_new_factor")
    path = tmp_path / "dt=20261104" / "data.parquet"
    _write_v5(path, cols)

    result = c.check_factor_dictionary_schema(path)

    assert result["ok"] is False
    assert dropped in result["removed"]
    assert "x_brand_new_factor" in result["added"]


def test_schema_unavailable_when_not_synced(tmp_path: Path):
    result = c.check_factor_dictionary_schema(root=tmp_path)

    assert result["available"] is False
    assert result["ok"] is None


def test_latest_partition_discovery(tmp_path: Path):
    _write_v5(
        tmp_path
        / "6_ml_datasets"
        / "factor_dictionary"
        / "dt=20261104"
        / "data.parquet",
        ["symbol", "date"],
    )
    _write_v5(
        tmp_path
        / "6_ml_datasets"
        / "factor_dictionary"
        / "dt=20261103"
        / "data.parquet",
        ["symbol", "date"],
    )

    latest = c.latest_factor_dictionary_partition(tmp_path)

    assert latest is not None
    assert latest.parent.name == "dt=20261104"


def test_contract_summary_shape(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(c, "sdk_version", lambda: "0.5.0")

    summary = c.contract_summary(tmp_path)

    assert summary["sdk"]["ok"] is True
    assert summary["factor_dictionary"]["available"] is False


def test_missing_golden_does_not_crash(monkeypatch, tmp_path: Path):
    missing = tmp_path / "missing.json"
    monkeypatch.setattr(c, "GOLDEN_PATH", missing)
    path = tmp_path / "dt=20261104" / "data.parquet"
    _write_v5(path, ["symbol", "date"])

    result = c.check_factor_dictionary_schema(path)

    assert result["available"] is True
    assert result["ok"] is None
    assert "金样缺失" in result["message"]


def test_dockerfile_installs_pinned_sdk_from_official_index():
    """镜像构建层必须能装上钉版 SDK，且不继承默认镜像源。

    2026-11-06 实测：默认 ``PIP_INDEX_URL``（清华）对 ``quantdb-sdk``
    返回 403，构建在该层以 "No matching distribution found" 失败；
    后果是镜像重建不可用 → 容器 recreate 后 SDK 退回镜像内旧版本
    （运行态 0.3.3 vs 契约 0.5.0，管理页一直亮红条）。
    """
    dockerfile = (
        Path(__file__).resolve().parents[2] / "docker" / "Dockerfile.oss"
    ).read_text(encoding="utf-8")
    pinned = re.compile(
        r"--index-url https://pypi\.org/simple[^\n]*\n\s*"
        rf'"quantdb-sdk=={re.escape(c.EXPECTED_SDK_VERSION)}"'
    )
    assert pinned.search(dockerfile), (
        "Dockerfile.oss 的 quantdb-sdk 安装行必须钉 "
        f"{c.EXPECTED_SDK_VERSION} 且显式 --index-url https://pypi.org/simple"
    )
