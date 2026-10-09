"""web「更新系统」→ updater 容器的启动规格。

为什么值得单独锁：这条链路把「点一下按钮」翻译成「在宿主上跑 deploy/update.sh」，
中间隔着 docker socket、bind mount、以及一整套 env。任何一环写错都不会在
本机开发时暴露 —— 按钮点下去只返回 started，真正的失败要等 updater 容器退出后
去 data/update.log 里翻。

历史踩坑（2026-10）：
1. ``_PROJECT_DIR`` 硬编码 ``/opt/quantmind``，而容器实际从别的路径跑
   → 日志全文只有 ``bash: /opt/quantmind/deploy/update.sh: No such file or directory``。
2. 曾试图在 API 容器里预检脚本存在性 —— 那是错的：API 容器只挂载了项目下的
   子目录（backend/scripts/config/data/…，**没有 deploy/**），在那边检查会把所有
   正常部署判成「脚本不存在」。存在性检查只能放在 updater 容器内（它按
   ``{project}:{project}`` 挂了整个项目目录）。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:  # pragma: no cover - 环境相关
    from backend.services.api.routers.admin import system_update as su
except Exception as _exc:  # noqa: BLE001
    su = None
    _IMPORT_ERR = _exc


pytestmark = pytest.mark.skipif(
    su is None, reason="system_update 依赖不可用（需容器环境）"
)


@pytest.fixture(autouse=True)
def _restore_module_constants():
    """``importlib.reload`` 会重算模块级常量，别把结果漏给后面的用例。

    reload 是测「常量读 env」的唯一手段，但它改的是模块对象本身；不还原的话
    后面用 ``su._PROJECT_DIR`` 的用例会拿到上一个用例留下的值（自比自仍然过，
    但断言就不再指向真实配置了）。
    """
    snapshot = (su._PROJECT_DIR, su._SCRIPT_PATH, su._LOG_PATH)
    yield
    su._PROJECT_DIR, su._SCRIPT_PATH, su._LOG_PATH = snapshot


def _render_cmd(monkeypatch, tmp_path: Path, *, script_exists: bool) -> tuple[str, Path]:
    """把模块常量指到临时目录后渲染出真实会被执行的那条 shell 命令。"""
    project = tmp_path / "proj"
    (project / "data").mkdir(parents=True, exist_ok=True)
    script = project / "deploy" / "update.sh"
    if script_exists:
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text('#!/bin/bash\necho "__SCRIPT_RAN__"\n', encoding="utf-8")

    monkeypatch.setattr(su, "_PROJECT_DIR", str(project))
    monkeypatch.setattr(su, "_SCRIPT_PATH", str(script))
    monkeypatch.setattr(su, "_LOG_PATH", str(project / "data" / "update.log"))

    spec = su._build_container_spec("quantmind-oss:latest")
    return spec["Cmd"][2], project / "data" / "update.log"


def test_script_check_is_valid_bash(monkeypatch, tmp_path):
    """渲染出来的命令必须是合法 bash —— 拼错一个分号就是一个跑不起来的更新。"""
    cmd, _ = _render_cmd(monkeypatch, tmp_path, script_exists=True)

    proc = subprocess.run(["bash", "-n"], input=cmd, text=True, capture_output=True)

    assert proc.returncode == 0, proc.stderr


def test_runs_the_script_when_present(monkeypatch, tmp_path):
    """脚本在时正常执行，且输出落进 update.log（状态接口读的就是它）。"""
    cmd, log_path = _render_cmd(monkeypatch, tmp_path, script_exists=True)

    proc = subprocess.run(["bash", "-c", cmd], text=True, capture_output=True)

    assert proc.returncode == 0, proc.stderr
    assert "__SCRIPT_RAN__" in log_path.read_text(encoding="utf-8")


def test_missing_script_writes_an_actionable_message(monkeypatch, tmp_path):
    """脚本缺失时：非零退出，且日志里是一句能照着改的话，不是 bash 的 No such file。"""
    cmd, log_path = _render_cmd(monkeypatch, tmp_path, script_exists=False)

    proc = subprocess.run(["bash", "-c", cmd], text=True, capture_output=True)

    assert proc.returncode != 0
    log = log_path.read_text(encoding="utf-8")
    assert "更新脚本不存在" in log
    # 必须点名变量，否则用户不知道该改什么
    assert "QUANTMIND_PROJECT_DIR" in log
    # 且不能是单纯把 bash 的报错抛出去
    assert "No such file or directory" not in log


def test_project_dir_is_bind_mounted_whole(monkeypatch):
    """必须整目录挂载（含 deploy/），否则 updater 容器根本看不到脚本。"""
    spec = su._build_container_spec("img")

    assert f"{su._PROJECT_DIR}:{su._PROJECT_DIR}:rw" in spec["HostConfig"]["Binds"]


def test_project_dir_is_configurable(monkeypatch):
    """目录可配（默认 /opt/quantmind）：非标准路径的部署不用改代码。"""
    monkeypatch.setenv("QUANTMIND_PROJECT_DIR", "/srv/custom-mind")
    import importlib

    reloaded = importlib.reload(su)
    try:
        assert reloaded._PROJECT_DIR == "/srv/custom-mind"
        assert reloaded._SCRIPT_PATH == "/srv/custom-mind/deploy/update.sh"
    finally:
        monkeypatch.delenv("QUANTMIND_PROJECT_DIR", raising=False)
        importlib.reload(su)


def test_project_dir_defaults_to_opt_quantmind(monkeypatch):
    monkeypatch.delenv("QUANTMIND_PROJECT_DIR", raising=False)
    import importlib

    reloaded = importlib.reload(su)
    try:
        assert reloaded._PROJECT_DIR == "/opt/quantmind"
    finally:
        importlib.reload(su)


def test_git_safe_directory_covers_the_project_dir(monkeypatch):
    """updater 容器内 git 的 safe.directory 必须指向项目目录。

    不配的话容器内 git 会以 dubious ownership 拒绝所有命令，update.sh 的
    「检测到未提交代码改动」判断会被误触发而直接中止。
    """
    spec = su._build_container_spec("img")
    env = spec["Env"]

    assert f"QUANTMIND_PROJECT_DIR={su._PROJECT_DIR}" in env
    assert "GIT_CONFIG_KEY_0=safe.directory" in env
    assert f"GIT_CONFIG_VALUE_0={su._PROJECT_DIR}" in env


def test_ref_and_remote_are_passed_through_only_when_set(monkeypatch):
    """QUANTMIND_REF/REMOTE 由运维显式配置；没设就不要替它决定分支。"""
    monkeypatch.delenv("QUANTMIND_REF", raising=False)
    monkeypatch.delenv("QUANTMIND_REMOTE", raising=False)
    env = su._build_container_spec("img")["Env"]
    assert not any(v.startswith("QUANTMIND_REF=") for v in env)
    assert not any(v.startswith("QUANTMIND_REMOTE=") for v in env)

    monkeypatch.setenv("QUANTMIND_REF", "next")
    monkeypatch.setenv("QUANTMIND_REMOTE", "gitee")
    env = su._build_container_spec("img")["Env"]
    assert "QUANTMIND_REF=next" in env
    assert "QUANTMIND_REMOTE=gitee" in env


def test_updater_container_is_not_managed_by_compose(monkeypatch):
    """updater 容器不能被打上 compose 标签：update.sh 会 force-recreate main 服务，
    若 updater 属于同一 compose project，它自己会被一起干掉，更新跑到一半中断。"""
    spec = su._build_container_spec("img")

    for key in spec["Labels"]:
        assert not key.startswith("com.docker.compose")


def test_docker_socket_is_mounted_into_updater(monkeypatch):
    """没有 socket 就没法执行 docker compose，更新会在构建阶段静默失败。"""
    spec = su._build_container_spec("img")

    assert f"{su._SOCKET}:/var/run/docker.sock" in spec["HostConfig"]["Binds"]


@pytest.mark.skipif(os.name != "posix", reason="仅 POSIX")
def test_allowed_write_target_is_the_project_log(monkeypatch, tmp_path):
    """日志写在项目 data/ 下 —— 状态接口读的 /data/update.log 是同一个文件。"""
    _, log_path = _render_cmd(monkeypatch, tmp_path, script_exists=False)

    assert log_path.name == "update.log"
    assert log_path.parent.name == "data"
