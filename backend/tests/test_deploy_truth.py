"""backend/shared/version.py 部署真相探针（T7-3 / H10）契约测试。

三条钉死的口径：
1. **运行处工作树优先**：.git 可达时 commit/脏标记全部来自 git 只读探针
   （--no-optional-locks 兼容 ro 挂载、safe.directory=* 兼容容器 root 读宿主
   属主的 .git）。容器与服务器均为 bind mount 活代码，version.json 只是部署
   时刻的声明——声明与运行分叉（热修/并行改动）必须暴露，不许粉饰。
2. **脏面限定 _STATUS_PATHS**：容器内 electron/ 等未挂载目录在 worktree 里
   「缺失」，全量 status 会把它们全部报成 deleted、脏标记恒真。
3. **分叉只在 version.json 在场时判定**：version.txt 是遗留回退、commit 冻结
   在写入时刻，拿它比会把正常开发报成永久分叉（与 test_version.py 的
   「宁可没有提示，不许假提示」同源）。
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

import backend.shared.version as vmod
from backend.services.api.routers import system as system_router

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
}


def _run_git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), "-c", "commit.gpgsign=false", *args],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, **_GIT_ENV},
    )
    return proc.stdout.strip()


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """最小真仓库：next 分支 + 一个已提交的 backend/ 文件。"""
    repo = tmp_path / "repo"
    (repo / "backend").mkdir(parents=True)
    _run_git(repo, "init", "-q", "-b", "next")
    (repo / "backend" / "a.py").write_text("x = 1\n", encoding="utf-8")
    _run_git(repo, "add", "backend/a.py")
    _run_git(repo, "commit", "-q", "-m", "init")
    return repo


def _isolate(
    monkeypatch,
    tmp_path: Path,
    *,
    code_root: Path | None,
    js: dict | None = None,
    txt: str | None = None,
    stamp: dict | None = None,
):
    """把探针的四个来源全部指到 tmp_path，避免读到仓库/容器上的真文件。"""
    no_repo = tmp_path / "_no_repo"
    no_repo.mkdir(exist_ok=True)
    monkeypatch.setattr(vmod, "_CODE_ROOT", code_root or no_repo)

    vjson = tmp_path / "version.json"
    if js is not None:
        vjson.write_text(json.dumps(js, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(vmod, "_VERSION_JSON", vjson)

    vtxt = tmp_path / "version.txt"
    if txt is not None:
        vtxt.write_text(txt, encoding="utf-8")
    monkeypatch.setattr(vmod, "_VERSION_TXT", vtxt)

    stamp_path = tmp_path / "deploy_stamp.json"
    if stamp is not None:
        stamp_path.write_text(json.dumps(stamp), encoding="utf-8")
    monkeypatch.setattr(vmod, "_IMAGE_STAMP", stamp_path)


# ---------------------------------------------------------------------------
# 工作树探针
# ---------------------------------------------------------------------------


def test_git_identity_clean_repo(git_repo: Path):
    head = _run_git(git_repo, "rev-parse", "HEAD")
    ident = vmod.get_git_identity(git_repo)

    assert ident["available"] is True
    assert ident["commit"] == head
    assert ident["short"] == head[:8]
    assert ident["branch"] == "next"
    assert ident["dirty"] is False
    assert ident["dirty_count"] == 0
    assert ident["dirty_sample"] == []


def test_git_identity_dirty_counts_scoped_changes(git_repo: Path):
    (git_repo / "backend" / "a.py").write_text("x = 2\n", encoding="utf-8")

    ident = vmod.get_git_identity(git_repo)

    assert ident["dirty"] is True
    assert ident["dirty_count"] == 1
    assert ident["dirty_sample"] == ["backend/a.py"]


def test_git_identity_ignores_paths_not_mounted_in_container(git_repo: Path):
    """electron/ 未挂进容器：它脏不构成「运行代码脏」，全量 status 会假阳性。"""
    (git_repo / "electron").mkdir()
    (git_repo / "electron" / "junk.ts").write_text("//\n", encoding="utf-8")

    ident = vmod.get_git_identity(git_repo)

    assert ident["dirty"] is False


def test_git_identity_dirty_sample_capped(git_repo: Path):
    for i in range(15):
        (git_repo / "backend" / f"f{i}.py").write_text("x = 1\n", encoding="utf-8")

    ident = vmod.get_git_identity(git_repo)

    assert ident["dirty_count"] == 15
    assert len(ident["dirty_sample"]) == vmod._DIRTY_SAMPLE_LIMIT


def test_git_identity_unavailable_outside_repo(tmp_path: Path):
    """非仓库/未挂载 .git：不可知就报 None，绝不显示成「干净」。"""
    empty = tmp_path / "plain"
    empty.mkdir()

    assert vmod.get_git_identity(empty) == {
        "available": False,
        "commit": "",
        "short": "",
        "branch": "",
        "dirty": None,
        "dirty_count": None,
        "dirty_sample": [],
    }


# ---------------------------------------------------------------------------
# 部署真相组合
# ---------------------------------------------------------------------------


def test_deploy_truth_prefers_runtime_worktree(tmp_path, monkeypatch, git_repo: Path):
    head = _run_git(git_repo, "rev-parse", "HEAD")
    _isolate(
        monkeypatch,
        tmp_path,
        code_root=git_repo,
        js={"version": "v9", "commit": "deadbeef" * 5, "branch": "next"},
    )

    truth = vmod.get_deploy_truth()

    assert truth["source"] == "git-runtime"
    assert truth["commit"] == head
    assert truth["dirty"] is False
    # 声明与运行不一致 → 必须暴露分叉（热修未落库的排障第一线索）
    assert truth["divergence"] == {
        "declared_commit": "deadbeef" * 5,
        "runtime_commit": head,
    }


def test_deploy_truth_no_divergence_when_declared_matches_runtime(
    tmp_path, monkeypatch, git_repo: Path
):
    head = _run_git(git_repo, "rev-parse", "HEAD")
    _isolate(
        monkeypatch,
        tmp_path,
        code_root=git_repo,
        js={"version": "v9", "commit": head, "branch": "next"},
    )

    assert vmod.get_deploy_truth()["divergence"] is None


def test_deploy_truth_legacy_txt_never_diverges(tmp_path, monkeypatch, git_repo: Path):
    """version.txt 是遗留回退、commit 冻结在写入时刻——拿它比会误报永久分叉。"""
    _isolate(monkeypatch, tmp_path, code_root=git_repo, txt="v1-150-g3d32379f\n")

    truth = vmod.get_deploy_truth()

    assert truth["source"] == "git-runtime"
    assert truth["divergence"] is None


def test_deploy_truth_falls_back_to_declared_when_git_unavailable(
    tmp_path, monkeypatch
):
    _isolate(
        monkeypatch,
        tmp_path,
        code_root=None,
        js={"version": "v9", "commit": "abcdef1234567890", "branch": "release"},
    )

    truth = vmod.get_deploy_truth()

    assert truth["source"] == "version-json"
    assert truth["commit"] == "abcdef1234567890"
    assert truth["short"] == "abcdef12"
    assert truth["branch"] == "release"
    assert truth["dirty"] is None  # 不可知 ≠ 干净


def test_deploy_truth_falls_back_to_image_stamp(tmp_path, monkeypatch):
    """源码包部署（无 .git 无 version.json）：镜像戳是最后的可核对身份。"""
    _isolate(
        monkeypatch,
        tmp_path,
        code_root=None,
        stamp={"commit": "cafe0123" * 5, "branch": "main", "torch_device": "cpu"},
    )

    truth = vmod.get_deploy_truth()

    assert truth["source"] == "image-stamp"
    assert truth["commit"] == "cafe0123" * 5
    assert truth["short"] == "cafe0123"
    assert truth["branch"] == "main"
    assert truth["image"]["torch_device"] == "cpu"


def test_deploy_truth_legacy_txt_is_last_resort(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path, code_root=None, txt="v1-150-g3d32379f\n")

    truth = vmod.get_deploy_truth()

    assert truth["source"] == "version-txt"
    assert truth["commit"] == "3d32379f"
    assert truth["dirty"] is None


def test_deploy_truth_unknown_when_nothing_available(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path, code_root=None)

    truth = vmod.get_deploy_truth()

    assert truth["source"] == "unknown"
    assert truth["commit"] == "" and truth["short"] == ""
    assert truth["divergence"] is None


def test_deploy_truth_is_json_serializable(tmp_path, monkeypatch, git_repo: Path):
    _isolate(monkeypatch, tmp_path, code_root=git_repo)

    json.loads(json.dumps(vmod.get_deploy_truth(), ensure_ascii=False))


# ---------------------------------------------------------------------------
# 启动打点
# ---------------------------------------------------------------------------


def test_log_deploy_truth_warns_on_dirty_and_divergence(
    tmp_path, monkeypatch, git_repo: Path, caplog
):
    import logging

    (git_repo / "backend" / "a.py").write_text("x = 3\n", encoding="utf-8")
    _isolate(
        monkeypatch,
        tmp_path,
        code_root=git_repo,
        js={"version": "v9", "commit": "deadbeef" * 5, "branch": "next"},
    )

    with caplog.at_level(logging.INFO, logger="deploy-truth-test"):
        truth = vmod.log_deploy_truth(logging.getLogger("deploy-truth-test"))

    assert truth["source"] == "git-runtime"
    assert "部署真相" in caplog.text and "commit=" in caplog.text
    assert "未提交改动" in caplog.text
    assert "分叉" in caplog.text


def test_log_deploy_truth_quiet_when_clean(
    tmp_path, monkeypatch, git_repo: Path, caplog
):
    import logging

    _isolate(monkeypatch, tmp_path, code_root=git_repo)

    with caplog.at_level(logging.INFO, logger="deploy-truth-test2"):
        vmod.log_deploy_truth(logging.getLogger("deploy-truth-test2"))

    assert "部署真相" in caplog.text
    assert "未提交改动" not in caplog.text
    assert "分叉" not in caplog.text


# ---------------------------------------------------------------------------
# 状态端点
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deploy_truth_endpoint_contract(tmp_path, monkeypatch, git_repo: Path):
    _isolate(monkeypatch, tmp_path, code_root=git_repo)

    payload = await system_router.deploy_truth()

    assert payload["source"] == "git-runtime"
    assert payload["runtime"]["available"] is True
    assert {
        "commit",
        "short",
        "branch",
        "dirty",
        "source",
        "runtime",
        "declared",
        "image",
        "divergence",
    } <= set(payload)
    json.loads(json.dumps(payload, ensure_ascii=False))
