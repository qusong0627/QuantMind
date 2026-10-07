"""「初始化数据」完成后的进程内缓存重置测试。

回归背景：新部署时 /data/quantdb 为空，hub 单例会 fallback 并永久缓存
错误路径；「初始化数据」覆盖目录后若无重置机制，个股终端在重启容器前
一直读不到数据。任务结束时须 reset_instance + 清终端 TTL 缓存，且两者
互不拖累（各自 try/except 隔离）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.services.api.routers import stock_terminal
from backend.services.api.routers.admin.quantdb_console import (
    _reset_quantdb_runtime_caches,
)
from backend.services.engine.data_platform.quantdb_hub import QuantDBDataHub


@pytest.fixture(autouse=True)
def _clean_singleton():
    """用例前后都清理单例，避免污染同进程其他测试。"""
    QuantDBDataHub.reset_instance()
    yield
    QuantDBDataHub.reset_instance()


def test_reset_instance_recreates_singleton() -> None:
    """reset_instance 后 get_instance 返回全新实例。"""
    first = QuantDBDataHub.get_instance()

    QuantDBDataHub.reset_instance()
    second = QuantDBDataHub.get_instance()

    assert second is not first


def test_reset_terminal_caches_clears_data_dir_and_ttl_caches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """终端缓存重置：_DATA_DIR 置空 + 四个 TTL 缓存全部清空。"""
    monkeypatch.setattr(stock_terminal, "_DATA_DIR", Path("/stale/quantdb"))
    stock_terminal._universe_cache.update(
        {"df": "stale", "ts": 123.0, "trade_date": "2026-09-30"}
    )
    stock_terminal._model_options_cache.update({"v": ["stale"], "ts": 123.0})
    stock_terminal._concept_cache.update({"ts": 123.0, "symbol_map": {"x": 1}})
    stock_terminal._index_membership_cache.update({"ts": 123.0, "map": {"x": 1}})

    stock_terminal.reset_terminal_caches()

    assert stock_terminal._DATA_DIR is None
    assert stock_terminal._universe_cache == {"df": None, "ts": 0.0, "trade_date": ""}
    assert stock_terminal._model_options_cache == {"v": None, "ts": 0.0}
    assert stock_terminal._concept_cache == {"ts": 0.0, "symbol_map": {}}
    assert stock_terminal._index_membership_cache == {"ts": 0.0, "map": {}}


def test_runtime_reset_clears_both_hub_and_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """组合重置：hub 单例作废 + 终端缓存清空一并生效。"""
    hub = QuantDBDataHub.get_instance()
    monkeypatch.setattr(stock_terminal, "_DATA_DIR", Path("/stale/quantdb"))

    _reset_quantdb_runtime_caches()

    assert QuantDBDataHub._instance is None
    assert QuantDBDataHub.get_instance() is not hub
    assert stock_terminal._DATA_DIR is None


def test_runtime_reset_tolerates_hub_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """hub 重置抛异常不拖累终端缓存清空（各自 try/except 隔离）。"""

    def _boom(cls):  # noqa: ANN001
        raise RuntimeError("boom")

    monkeypatch.setattr(QuantDBDataHub, "reset_instance", classmethod(_boom))
    monkeypatch.setattr(stock_terminal, "_DATA_DIR", Path("/stale/quantdb"))

    _reset_quantdb_runtime_caches()  # 不抛出

    assert stock_terminal._DATA_DIR is None


def test_runtime_reset_tolerates_terminal_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """终端缓存清空抛异常不拖累 hub 重置（反向隔离）。"""

    def _boom() -> None:
        raise RuntimeError("boom")

    hub = QuantDBDataHub.get_instance()
    monkeypatch.setattr(stock_terminal, "reset_terminal_caches", _boom)

    _reset_quantdb_runtime_caches()  # 不抛出

    assert QuantDBDataHub.get_instance() is not hub
