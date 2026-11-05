"""备选链容器投递的回归闸：不许再退回 ``docker cp``。

2026-11-03~05 实锤：quantmind 容器 bind 了 ``/mnt/tdx-shared``（TDX 桥 CIFS 共享盘），
对端 Windows 关机后宿主挂载变 ``Host is down``，docker daemon 为 ``docker cp`` 准备
容器挂载直接失败（``mkdir /mnt/tdx-shared: file exists``）→ 备选链「补推理」一步断掉，
连续三天只能靠 08:00 盘前推理兜底。本测试锁死投递形态：``docker exec -i`` + stdin，
命令里不得出现 ``docker cp``。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # scripts/
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # 仓库根（backend.*）

import postmarket_pipeline as pp  # noqa: E402

SCRIPT = Path("/repo/skills/daily-review/scripts/trigger_inference.py")


class TestRunInContainer:
    def test_delivers_script_via_stdin_without_docker_cp(self, tmp_path, monkeypatch):
        """脚本从 stdin 写入容器 /tmp 再 exec；命令行里没有 docker cp。"""
        script = tmp_path / "trigger_inference.py"
        script.write_bytes(b"print('hi')\n")
        seen: dict = {}

        def fake_run(cmd, **kwargs):
            seen["cmd"] = cmd
            seen["content"] = kwargs["stdin"].read()  # stdin 必须是打开的脚本文件
            return SimpleNamespace(returncode=0)

        monkeypatch.setattr(pp.subprocess, "run", fake_run)

        pp.run_in_container(script, ["--date", "20261104"])

        cmd = seen["cmd"]
        assert cmd[:6] == ["docker", "exec", "-i", "-w", "/app", "quantmind"]
        assert not any(tok == "cp" for tok in cmd)  # 不许退回 docker cp
        assert cmd[6] == "sh" and cmd[7] == "-c"
        inner = cmd[8]
        assert 'cat > "/tmp/qm_exec_trigger_inference_' in inner  # 唯一名，避开旧 cp 残留
        assert 'python3 "/tmp/qm_exec_trigger_inference_' in inner
        assert '"$@"; rc=$?' in inner
        assert "rm -f " in inner and "exit $rc" in inner  # 执行后自删
        assert cmd[-2:] == ["--date", "20261104"]  # $0 占位之后的真实参数
        assert seen["content"] == b"print('hi')\n"

    def test_failure_exits_with_step_error(self, tmp_path, monkeypatch):
        """容器执行失败 → 与 run() 同款「步骤失败」退出，不静默。"""
        script = tmp_path / "trigger_inference.py"
        script.write_bytes(b"raise SystemExit(1)\n")
        monkeypatch.setattr(
            pp.subprocess, "run", lambda cmd, **kw: SimpleNamespace(returncode=1)
        )

        with pytest.raises(SystemExit, match="步骤失败"):
            pp.run_in_container(script, ["--date", "20261104"])

    def test_dry_run_does_not_execute(self, tmp_path, monkeypatch):
        """--dry-run 只打印，不落 io、不进容器。"""
        script = tmp_path / "trigger_inference.py"
        script.write_bytes(b"")

        def boom(*args, **kwargs):
            raise AssertionError("dry-run 不许执行 subprocess")

        monkeypatch.setattr(pp.subprocess, "run", boom)

        pp.run_in_container(script, ["--date", "20261104"], dry_run=True)


class TestStepPicksFallback:
    def test_missing_batch_falls_back_to_stdin_delivery(self, monkeypatch, tmp_path):
        """pick_candidates 失败 → 补推理走 stdin 投递（而不是 docker cp）。"""
        monkeypatch.setattr(pp, "L2_PART", lambda d: tmp_path / "missing")
        monkeypatch.setattr(pp.shutil, "which", lambda name: "/usr/bin/docker")
        monkeypatch.setattr(
            pp.subprocess, "run", lambda cmd, **kw: SimpleNamespace(returncode=1)
        )
        delivered: list = []
        monkeypatch.setattr(
            pp,
            "run_in_container",
            lambda script, argv, **kw: delivered.append((script, argv)),
        )
        args = SimpleNamespace(wait_l2_min=0, picks_only=True, dry_run=False, top=30)

        with pytest.raises(SystemExit):  # 重试 pick_candidates 仍失败 → 退出
            pp.step_picks("20261104", args)

        assert delivered == [(pp.TRIGGER_SCRIPT, ["--date", "20261104"])]
