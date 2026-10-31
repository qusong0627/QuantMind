"""resume_paused_containers_from_state 单测（取消/完成回调的显式 resume）。

覆盖：
- 状态文件不存在 → 返回 []；
- 已有 resumed_at → 幂等跳过，不 start 任何容器；
- 坏文件 → 返回 [] 且不抛异常；
- 停止中容器被 start，已在跑的不动，已删除的记 warning 跳过，
  成功后打标 resumed_at / resumed / resumed_by；
- docker sdk 缺失 / from_env 失败 → 返回 [] 且不抛异常；
- qwenpaw 必须在默认保护前缀里（回归 2026-10-31 事故：训练 pause 停掉
  qwenpaw 后 unless-stopped 不自启，且 cancel 杀掉了轮询协程导致无人 resume）。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

try:
    from backend.services.engine.training import local_docker_orchestrator as orch
except Exception as exc:  # pragma: no cover - 重依赖缺失时跳过
    pytest.skip(f"cannot import orchestrator: {exc}", allow_module_level=True)


class _FakeContainer:
    def __init__(self, name: str, status: str = "exited"):
        self.name = name
        self.status = status
        self.started = False

    def start(self):
        self.started = True
        self.status = "running"


class _FakeContainers:
    def __init__(self, existing: dict[str, _FakeContainer]):
        self._existing = existing

    def get(self, name: str) -> _FakeContainer:
        if name not in self._existing:
            raise KeyError(f"no such container: {name}")
        return self._existing[name]


class _FakeClient:
    def __init__(self, existing: dict[str, _FakeContainer]):
        self.containers = _FakeContainers(existing)


def _write_state(work_dir, payload: dict | str) -> None:
    work_dir.mkdir(parents=True, exist_ok=True)
    text = payload if isinstance(payload, str) else json.dumps(payload)
    (work_dir / ".paused_containers.json").write_text(text, encoding="utf-8")


def _patch_client(monkeypatch, existing: dict[str, _FakeContainer]):
    fake_client = _FakeClient(existing)

    class _FakeDockerClient:
        @staticmethod
        def from_env():
            return fake_client

    monkeypatch.setattr(orch, "DockerClient", _FakeDockerClient)
    return fake_client


def _read_state(work_dir) -> dict:
    return json.loads(
        (work_dir / ".paused_containers.json").read_text(encoding="utf-8")
    )


def test_missing_file_returns_empty(tmp_path):
    assert orch.resume_paused_containers_from_state(tmp_path / "nope", "r1") == []


def test_already_resumed_is_idempotent(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    work_dir = tmp_path / "run_done"
    _write_state(
        work_dir,
        {"run_id": "run_done", "paused_at": now, "containers": ["c1"],
         "resumed_at": now, "resumed": ["c1"]},
    )
    existing = {"c1": _FakeContainer("c1", "exited")}
    _patch_client(monkeypatch, existing)

    assert orch.resume_paused_containers_from_state(work_dir, "run_done") == []
    assert existing["c1"].started is False


def test_corrupt_file_never_raises(tmp_path, monkeypatch):
    work_dir = tmp_path / "run_bad"
    _write_state(work_dir, "not-json{{{")
    _patch_client(monkeypatch, {})

    assert orch.resume_paused_containers_from_state(work_dir, "run_bad") == []


def test_resumes_stopped_skips_running_and_gone(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    work_dir = tmp_path / "run_x"
    _write_state(
        work_dir,
        {"run_id": "run_x", "paused_at": now,
         "containers": ["c_stopped", "c_running", "c_gone"]},
    )
    existing = {
        "c_stopped": _FakeContainer("c_stopped", "exited"),
        "c_running": _FakeContainer("c_running", "running"),
    }
    _patch_client(monkeypatch, existing)

    resumed = orch.resume_paused_containers_from_state(
        work_dir, "run_x", resumed_by="cancel-reconcile"
    )

    assert set(resumed) == {"c_stopped", "c_running"}
    assert existing["c_stopped"].started is True
    assert existing["c_running"].started is False

    data = _read_state(work_dir)
    assert data["resumed_at"]
    assert data["resumed_by"] == "cancel-reconcile"
    assert set(data["resumed"]) == {"c_stopped", "c_running"}


def test_docker_unavailable_never_raises(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    work_dir = tmp_path / "run_d"
    _write_state(
        work_dir,
        {"run_id": "run_d", "paused_at": now, "containers": ["c1"]},
    )
    monkeypatch.setattr(orch, "DockerClient", None)

    assert orch.resume_paused_containers_from_state(work_dir, "run_d") == []


def test_qwenpaw_is_protected_by_default():
    assert "qwenpaw" in orch._DEFAULT_PROTECTED_PREFIXES
    assert orch.LocalDockerOrchestrator._is_protected("qwenpaw") is True
    assert orch.LocalDockerOrchestrator._is_protected("quantmind") is True
    assert orch.LocalDockerOrchestrator._is_protected("qm-train-abc") is True
    assert orch.LocalDockerOrchestrator._is_protected("some-random") is False
