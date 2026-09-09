"""QuantDB 数据目录解析（backend/shared/quantdb_paths.py）单元测试。

背景：便携包（免 Docker）的数据目录是 ``$STORAGE_ROOT/quantdb``，机器上不存在
``/data/quantdb``。凡硬编码该绝对路径的模块都会读空 → 静默降级（行业全变「其他」、
position_score 失真）。本模块是唯一解析事实源，语义必须与
``quantdb_hub._resolve_data_dir()`` 逐条等价（docker 下零行为变化）。
"""
from __future__ import annotations

import pytest

from backend.shared import quantdb_paths


@pytest.mark.unit
def test_env_dir_wins_when_non_empty(tmp_path, monkeypatch):
    root = tmp_path / "quantdb"
    (root / "2_base_sector").mkdir(parents=True)
    monkeypatch.setenv("QM_QUANTDB_DATA_DIR", str(root))

    assert quantdb_paths.resolve_quantdb_dir() == root


@pytest.mark.unit
def test_empty_env_dir_is_skipped(tmp_path, monkeypatch):
    """便携包首启会 mkdir 出空 quantdb；空目录不算命中（否则变成静默空查询）。"""
    empty = tmp_path / "empty"
    empty.mkdir()
    fallback = tmp_path / "fallback"
    (fallback / "x").mkdir(parents=True)
    monkeypatch.setenv("QM_QUANTDB_DATA_DIR", str(empty))
    monkeypatch.setattr(quantdb_paths, "_DEFAULT_DATA_DIRS", [str(fallback)])

    assert quantdb_paths.resolve_quantdb_dir() == fallback


@pytest.mark.unit
def test_missing_env_dir_falls_back(tmp_path, monkeypatch):
    fallback = tmp_path / "fallback"
    (fallback / "x").mkdir(parents=True)
    monkeypatch.setenv("QM_QUANTDB_DATA_DIR", str(tmp_path / "nope"))
    monkeypatch.setattr(quantdb_paths, "_DEFAULT_DATA_DIRS", [str(fallback)])

    assert quantdb_paths.resolve_quantdb_dir() == fallback


@pytest.mark.unit
def test_resolve_subdir_joins_parts(tmp_path, monkeypatch):
    root = tmp_path / "quantdb"
    (root / "a").mkdir(parents=True)
    monkeypatch.setenv("QM_QUANTDB_DATA_DIR", str(root))

    got = quantdb_paths.resolve_quantdb_subdir("2_base_sector", "instrument_detail")

    assert got == root / "2_base_sector" / "instrument_detail"


@pytest.mark.unit
def test_falls_back_to_project_root_candidate(tmp_path, monkeypatch):
    """全部候选落空 → 回项目根 data/quantdb（报错信息才指向真实位置）。"""
    monkeypatch.delenv("QM_QUANTDB_DATA_DIR", raising=False)
    monkeypatch.setattr(quantdb_paths, "_DEFAULT_DATA_DIRS", [str(tmp_path / "nope")])
    project_root = tmp_path / "proj"
    (project_root / "data" / "quantdb" / "x").mkdir(parents=True)
    monkeypatch.setattr(quantdb_paths, "_PROJECT_ROOT", project_root)

    assert quantdb_paths.resolve_quantdb_dir() == project_root / "data" / "quantdb"


@pytest.mark.unit
def test_project_root_candidate_missing_returns_last_default(tmp_path, monkeypatch):
    """连项目根候选都没有 → 返回最后一个候选（让调用方报错更清晰）。"""
    monkeypatch.delenv("QM_QUANTDB_DATA_DIR", raising=False)
    last = tmp_path / "last"
    monkeypatch.setattr(quantdb_paths, "_DEFAULT_DATA_DIRS", [last.as_posix()])
    monkeypatch.setattr(quantdb_paths, "_PROJECT_ROOT", tmp_path / "proj")

    assert quantdb_paths.resolve_quantdb_dir() == last


@pytest.mark.unit
def test_hub_resolver_equivalent(tmp_path, monkeypatch):
    """零差异护栏：与 quantdb_hub._resolve_data_dir() 结果一致。"""
    pytest.importorskip("pandas")
    root = tmp_path / "quantdb"
    (root / "x").mkdir(parents=True)
    monkeypatch.setenv("QM_QUANTDB_DATA_DIR", str(root))

    from backend.services.engine.data_platform.quantdb_hub import _resolve_data_dir

    assert quantdb_paths.resolve_quantdb_dir() == _resolve_data_dir()
