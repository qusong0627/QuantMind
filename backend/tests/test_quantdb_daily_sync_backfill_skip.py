"""L1 OHLCV backfill 在 native 模式下的降级语义测试。

回归背景：免 docker（AutoDL native_python）节点只推 backend_min/ 最小
子树，不含 backend/scripts。每次开训同步都会打印一条看着像报错的
"L1 OHLCV backfill failed: No module named 'backend.scripts'"。
修复后：缺 backend.scripts 时降级 info + status=skipped（预期非故障）；
backfill 自身依赖缺失（如 pandas）与其他异常仍按 warning + status=error。
"""

from __future__ import annotations

import builtins
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import backend.scripts.quantdb_daily_sync as qds


def _drive(monkeypatch, import_error: Exception | None) -> dict:
    """驱动 run_daily_sync 到 backfill 块并返回结果（同步/外部数据源全打桩）。"""
    monkeypatch.setattr(
        qds,
        "sync_parquet",
        lambda *a, **kw: {"synced": 0, "up_to_date": 0, "errors": []},
    )
    monkeypatch.setattr(qds, "_sync_extra_sources", lambda **kw: {})

    if import_error is not None:
        real_import = builtins.__import__

        def _fake_import(name, *args, **kwargs):
            if name == "backend.scripts.backfill_l1_ohlcv":
                raise import_error
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _fake_import)

    return qds.run_daily_sync(datasets=["l1_factors"], parquet_only=True)


def test_missing_backend_scripts_downgraded_to_skipped(monkeypatch) -> None:
    """native 节点缺 backend.scripts → info 降级、status=skipped。"""
    err = ModuleNotFoundError(
        "No module named 'backend.scripts'", name="backend.scripts"
    )
    result = _drive(monkeypatch, err)

    assert result["l1_ohlcv_backfill"] == {
        "status": "skipped",
        "reason": "module unavailable: backend.scripts",
    }


def test_missing_backend_scripts_submodule_also_skipped(monkeypatch) -> None:
    """缺整棵 backend.scripts.* 子树（如 backfill_l1_ohlcv 模块本身）同判。"""
    err = ModuleNotFoundError(
        "No module named 'backend.scripts.backfill_l1_ohlcv'",
        name="backend.scripts.backfill_l1_ohlcv",
    )
    result = _drive(monkeypatch, err)

    assert result["l1_ohlcv_backfill"]["status"] == "skipped"
    assert "backend.scripts.backfill_l1_ohlcv" in result["l1_ohlcv_backfill"]["reason"]


def test_missing_third_party_dependency_still_error(monkeypatch) -> None:
    """backfill 自身依赖缺失（如无 pandas）仍按 error 报出，语义不变。"""
    err = ModuleNotFoundError("No module named 'pandas'", name="pandas")
    result = _drive(monkeypatch, err)

    assert result["l1_ohlcv_backfill"] == {"status": "error", "reason": str(err)}


def test_other_exception_still_error(monkeypatch) -> None:
    """非 ImportError 异常不受本次改动影响，仍为 error。"""
    result = _drive(monkeypatch, RuntimeError("boom"))

    assert result["l1_ohlcv_backfill"]["status"] == "error"
    assert "boom" in result["l1_ohlcv_backfill"]["reason"]
