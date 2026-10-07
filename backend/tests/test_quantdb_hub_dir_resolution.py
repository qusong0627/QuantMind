"""QuantDB 数据目录自动重解析回归测试。

回归背景：单例可能在「初始化数据」完成**之前**创建——``_resolve_data_dir``
跳过空目录 fallback 到错误路径，长驻进程（api/engine/stream/celery）此后
一直读旧路径，必须重启容器才能恢复。修复后 ``data_dir`` 属性在缓存目录
无数据且非显式指定时重新解析，并按代数重建线程 DuckDB 连接。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.services.engine.data_platform import quantdb_hub
from backend.services.engine.data_platform.quantdb_hub import QuantDBDataHub


@pytest.fixture()
def dirs(tmp_path: Path) -> dict[str, Path]:
    """两个候选目录：A 空（错误 fallback）、B 有数据（初始化完成后）。"""
    a = tmp_path / "empty"
    a.mkdir()
    b = tmp_path / "populated"
    b.mkdir()
    (b / "1_kline_data").mkdir()
    return {"a": a, "b": b}


def test_reresolves_when_cached_dir_is_empty(
    monkeypatch: pytest.MonkeyPatch, dirs: dict[str, Path]
) -> None:
    """缓存目录空且重解析给出有数据目录 → data_dir 切换、代数推进。"""
    # 单例创建时解析到空目录 A（模拟「初始化数据」尚未完成）
    monkeypatch.setattr(quantdb_hub, "_resolve_data_dir", lambda: dirs["a"])
    hub = QuantDBDataHub()
    assert hub.data_dir == dirs["a"]
    assert hub._dir_generation == 0

    # 「初始化数据」补齐 B 后，重解析应切换到 B
    monkeypatch.setattr(quantdb_hub, "_resolve_data_dir", lambda: dirs["b"])
    assert hub.data_dir == dirs["b"]
    assert hub._dir_generation == 1
    assert hub.available is True


def test_no_generation_bump_when_resolution_unchanged(
    monkeypatch: pytest.MonkeyPatch, dirs: dict[str, Path]
) -> None:
    """重解析结果与缓存相同（仍无数据）→ 不推进代数、不误报变更。"""
    monkeypatch.setattr(quantdb_hub, "_resolve_data_dir", lambda: dirs["a"])
    hub = QuantDBDataHub()

    assert hub.data_dir == dirs["a"]
    assert hub._dir_generation == 0


def test_explicit_dir_never_reresolves(
    monkeypatch: pytest.MonkeyPatch, dirs: dict[str, Path]
) -> None:
    """显式指定目录（各市场子类）→ 即使目录为空也不重解析。"""
    monkeypatch.setattr(quantdb_hub, "_resolve_data_dir", lambda: dirs["b"])
    hub = QuantDBDataHub(data_dir=dirs["a"])

    assert hub.data_dir == dirs["a"]
    assert hub._dir_generation == 0


def test_duck_conn_rebuilt_after_reresolve(
    monkeypatch: pytest.MonkeyPatch, dirs: dict[str, Path]
) -> None:
    """代数推进后线程连接重建；未推进时复用同一连接。"""
    monkeypatch.setattr(quantdb_hub, "_resolve_data_dir", lambda: dirs["a"])
    hub = QuantDBDataHub()

    conn1 = hub._get_duck_conn()
    assert hub._get_duck_conn() is conn1  # 同代复用

    monkeypatch.setattr(quantdb_hub, "_resolve_data_dir", lambda: dirs["b"])
    _ = hub.data_dir  # 触发重解析，代数 +1
    conn2 = hub._get_duck_conn()

    assert conn2 is not conn1  # 换代重建，旧路径下的空视图不再复用
    assert hub._local.conn_gen == hub._dir_generation


def test_available_triggers_reresolve(
    monkeypatch: pytest.MonkeyPatch, dirs: dict[str, Path]
) -> None:
    """available 经 data_dir 触发重解析：目录补齐后由 False 变 True。"""
    monkeypatch.setattr(quantdb_hub, "_resolve_data_dir", lambda: dirs["a"])
    hub = QuantDBDataHub()
    assert hub.available is False

    monkeypatch.setattr(quantdb_hub, "_resolve_data_dir", lambda: dirs["b"])
    assert hub.available is True
    assert hub.data_dir == dirs["b"]
