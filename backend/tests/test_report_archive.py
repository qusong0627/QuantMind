"""报告档案根解析：优先级顺序 + 「写侧读侧同源」这条历史事故的守卫。"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.shared import report_archive as RA


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """把两个候选根都指向 tmp_path 下的可控目录，避免依赖容器真实文件系统。"""
    new_dir = tmp_path / "new"
    legacy = tmp_path / "legacy"
    monkeypatch.setattr(RA, "RESULTS_DIR", new_dir)
    monkeypatch.setattr(RA, "LEGACY_RESULTS_DIR", legacy)
    monkeypatch.delenv(RA.RESULTS_ENV, raising=False)
    return new_dir, legacy


def test_env_优先于一切(isolated, tmp_path, monkeypatch):
    new_dir, legacy = isolated
    new_dir.mkdir()
    legacy.mkdir()
    custom = tmp_path / "custom"
    custom.mkdir()
    monkeypatch.setenv(RA.RESULTS_ENV, str(custom))
    assert RA.resolve_results_dir() == custom


def test_env_指向不存在的目录时视为未配置(isolated, monkeypatch, tmp_path):
    """配了空/坏路径必须继续走回退链，而不是返回一个不存在的目录。"""
    new_dir, _ = isolated
    new_dir.mkdir()
    monkeypatch.setenv(RA.RESULTS_ENV, str(tmp_path / "nope"))
    assert RA.resolve_results_dir() == new_dir


def test_env_为空白字符串时视为未配置(isolated, monkeypatch):
    new_dir, _ = isolated
    new_dir.mkdir()
    monkeypatch.setenv(RA.RESULTS_ENV, "   ")
    assert RA.resolve_results_dir() == new_dir


def test_新目录缺失时回退旧目录(isolated):
    new_dir, legacy = isolated
    legacy.mkdir()
    assert RA.resolve_results_dir() == legacy


def test_两者都不存在时返回默认根且不创建(isolated):
    """**这条就是历史事故的守卫**：解析不得有建目录的副作用。

    一旦解析顺手 mkdir，下次解析结果就变了 —— 老档案会在 UI 里"消失"。
    """
    new_dir, legacy = isolated
    got = RA.resolve_results_dir()
    assert got == new_dir
    assert not new_dir.exists(), "解析函数不得创建目录（副作用会改变下次解析结果）"
    assert not legacy.exists()


def test_archive_root_与_resolve_同源():
    assert RA.archive_root() == RA.resolve_results_dir()


def test_服务端路由与共享模块同源(isolated, monkeypatch):
    """router 的 ``_resolve_results_dir`` 必须委托给本模块，不能各留一份。"""
    pytest.importorskip("fastapi")
    from backend.services.engine.routers import trading_agents as ta

    new_dir, _ = isolated
    new_dir.mkdir()
    assert Path(ta._resolve_results_dir()) == new_dir


def test_ensure_report_dir_创建并拒绝越界名(tmp_path, monkeypatch):
    monkeypatch.setattr(RA, "RESULTS_DIR", tmp_path)
    monkeypatch.delenv(RA.RESULTS_ENV, raising=False)
    p = RA.ensure_report_dir("因子研究")
    assert p.is_dir() and p.parent == tmp_path
    for bad in ("", ".", "..", "a/b", "a\\b"):
        with pytest.raises(ValueError):
            RA.ensure_report_dir(bad)
