"""market snapshot 新鲜度门控回归测试。

锁的是一条曾导致「标签双向查询断档」的门控缺陷：日期命名 SQLite
（{YYYY-MM-DD}.db，含 tags）**只由离线全量构建产出**，流式刷新只写
latest.json + latest.db 的 sector_mv。旧门控只看 latest.json 的
trade_date——流式刷新把 latest.json 推到新交易日后，离线任务整日
跳过，对应日期库永远补不出来。新门控要求日期库存在才允许跳过。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from backend.services.engine.tasks import celery_tasks


class _FakeProc:
    returncode = 0
    stdout = "[fake] ok"
    stderr = ""


@pytest.fixture()
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Any]:
    """把快照目录指向 tmp，并替换子进程执行为记录器。"""
    calls: list[list[str]] = []

    def _fake_run(cmd: list[str], **kwargs: Any) -> _FakeProc:
        calls.append(cmd)
        return _FakeProc()

    monkeypatch.setenv("QM_MARKET_SNAPSHOT_DIR", str(tmp_path))
    monkeypatch.setenv("QM_QUANTDB_DATA_DIR", str(tmp_path / "quantdb"))
    # run_market_snapshot 在函数体内 import subprocess，模块级无该属性，
    # 只能打在真实 subprocess 模块上。
    monkeypatch.setattr("subprocess.run", _fake_run)
    return {"out_dir": tmp_path, "calls": calls}


def _patch_state(
    monkeypatch: pytest.MonkeyPatch, max_part: str, latest_td: str | None
) -> None:
    monkeypatch.setattr(
        celery_tasks,
        "_snapshot_source_state",
        lambda data_dir, out_dir: (max_part, latest_td),
    )


def test_skips_when_up_to_date_and_dated_db_exists(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, Any]
) -> None:
    """latest.json 已最新且日期库在场 → 跳过，不触发子进程。"""
    _patch_state(monkeypatch, "20260930", "20260930")
    (env["out_dir"] / "2026-09-30.db").write_bytes(b"")

    result = celery_tasks.run_market_snapshot()

    assert result["status"] == "skipped"
    assert result["reason"] == "stale"
    assert env["calls"] == []


def test_forces_rebuild_when_dated_db_missing(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, Any]
) -> None:
    """latest.json 已最新但日期库缺失（流式刷新抢先）→ 强制全量重建。"""
    _patch_state(monkeypatch, "20260930", "20260930")

    result = celery_tasks.run_market_snapshot()

    assert result["status"] == "success"
    assert len(env["calls"]) == 1


def test_runs_when_partition_newer_than_latest(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, Any]
) -> None:
    """库内分区比线上新 → 无论日期库在不在都跑（原有行为不变）。"""
    _patch_state(monkeypatch, "20261001", "20260930")

    result = celery_tasks.run_market_snapshot()

    assert result["status"] == "success"
    assert len(env["calls"]) == 1


def test_runs_when_no_latest_freshness_known(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, Any]
) -> None:
    """读不到线上 latest.json（latest_td=None）→ 直接跑（原有行为不变）。"""
    _patch_state(monkeypatch, "20260930", None)

    result = celery_tasks.run_market_snapshot()

    assert result["status"] == "success"
    assert len(env["calls"]) == 1
