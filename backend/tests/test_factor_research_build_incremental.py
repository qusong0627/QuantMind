"""增量续拉入口（POST /build?append=true）—— 命令装配与失败方向。

机构级要求：增量构建绝不能重算方向符号。私有库 append 必须显式带
``--direction-file``；没有冻结表就拒绝启动（宁可报错，不可静默用新窗口 IC 定方向）。
"""

from __future__ import annotations

import pytest

from backend.services.engine.factor_research import service


@pytest.fixture()
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(service.store, "artifact_dir", lambda *_a, **_k: tmp_path)
    monkeypatch.setattr(service, "_build_running", lambda _d: None)
    return tmp_path


class _FakeProc:
    pid = 4242


def test_private_append_缺_direction_拒绝启动(env):
    out = service.start_build("private", "CN", start="2025-01-01", append=True)
    assert "error" in out
    assert "direction.json" in out["error"]


def test_private_append_装配冻结方向与区间(env, monkeypatch):
    (env / "direction.json").write_text('{"factors":{}}', encoding="utf-8")
    calls: dict = {}

    def fake_popen(cmd, **kw):
        calls["cmd"] = list(cmd)
        return _FakeProc()

    monkeypatch.setattr(service.subprocess, "Popen", fake_popen)
    out = service.start_build(
        "private", "CN", start="2025-01-01", end="2025-12-31", append=True
    )

    cmd = calls["cmd"]
    assert out["started"] is True
    assert "--append" in cmd
    assert cmd[cmd.index("--start") + 1] == "2025-01-01"
    assert cmd[cmd.index("--end") + 1] == "2025-12-31"
    assert cmd[cmd.index("--direction-file") + 1].endswith("direction.json")
    # 增量也要补写 fallback 方向（冻结表已有条目不被覆盖）
    assert cmd[cmd.index("--direction-out") + 1].endswith("direction.json")


def test_classic_append_明确拒绝(env):
    out = service.start_build("classic", "CN", start="2025-01-01", append=True)
    assert "error" in out
    assert "classic" in out["error"]


def test_private_full_build_写_direction_out(env, monkeypatch):
    calls: dict = {}

    def fake_popen(cmd, **kw):
        calls["cmd"] = list(cmd)
        return _FakeProc()

    monkeypatch.setattr(service.subprocess, "Popen", fake_popen)
    out = service.start_build("private", "CN")

    cmd = calls["cmd"]
    assert out["started"] is True
    assert "--direction-out" in cmd
    assert "--append" not in cmd
    assert "--market" in cmd and cmd[cmd.index("--market") + 1] == "CN"


def test_private_full_rebuild_已有方向时先读后写(env, monkeypatch):
    """全量重建也不能重算 sign：已有 direction.json 时必须同时带 --direction-file。"""
    (env / "direction.json").write_text('{"factors":{}}', encoding="utf-8")
    calls: dict = {}

    def fake_popen(cmd, **kw):
        calls["cmd"] = list(cmd)
        return _FakeProc()

    monkeypatch.setattr(service.subprocess, "Popen", fake_popen)
    out = service.start_build("private", "CN")

    cmd = calls["cmd"]
    assert out["started"] is True
    assert "--direction-file" in cmd
    assert "--direction-out" in cmd
