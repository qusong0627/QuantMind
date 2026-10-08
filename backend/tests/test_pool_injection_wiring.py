"""因子池注入接线：launcher（写文件+env）→ rd_loop_wrapper（读文件拼 prompt）。

防的是**接线静默断**：注入跨三步——
1. engine 进程 ``prepare_injection`` 写 ``<task_log_dir>/pool_context.md``；
2. Popen 的 env 带 ``QMF_POOL_CONTEXT_PATH``；
3. 子进程 ``rd_loop_wrapper._build_prompt_suffix`` 读文件追加「历史挖掘记忆」段。

任何一跳断了都不报错，只表现为「注入从未生效」——本项目有过先例（prompt
中文注入曾 import 错模块、从未生效，日志上什么都看不出来）。所以每一步
都钉一个断言：丢了 QMF_POOL_CONTEXT_PATH 这一跳，池页数据再全也进不了 LLM。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.services.engine.alpha_agent import launcher as launcher_mod  # noqa: E402
from backend.services.engine.alpha_agent.launcher import (  # noqa: E402
    AlphaAgentLauncher,
    EvolutionTask,
    TaskStatus,
)
from backend.services.engine.mining_plugins import pool_service  # noqa: E402
from backend.services.engine.rd_agent.rd_loop_wrapper import RDLoopWrapper  # noqa: E402


class TestWrapperPoolBlock:
    def _wrapper(self) -> RDLoopWrapper:
        return RDLoopWrapper(market="a_share")

    def test_no_env_no_pool_block(self, monkeypatch):
        monkeypatch.delenv("QMF_POOL_CONTEXT_PATH", raising=False)
        suffix = self._wrapper()._build_prompt_suffix()
        assert "语言要求" in suffix
        assert "历史挖掘记忆" not in suffix

    def test_env_file_content_is_appended(self, tmp_path, monkeypatch):
        ctx = tmp_path / "pool_context.md"
        ctx.write_text("### 历史挖掘记忆（因子池检索，仅供方向参考）\n1. `f1` ...\n")
        monkeypatch.setenv("QMF_POOL_CONTEXT_PATH", str(ctx))
        suffix = self._wrapper()._build_prompt_suffix()
        assert "历史挖掘记忆" in suffix
        assert "`f1`" in suffix

    def test_missing_file_degrades_to_empty(self, tmp_path, monkeypatch):
        monkeypatch.setenv("QMF_POOL_CONTEXT_PATH", str(tmp_path / "nope.md"))
        suffix = self._wrapper()._build_prompt_suffix()
        assert "语言要求" in suffix and "历史挖掘记忆" not in suffix

    def test_empty_file_degrades_to_empty(self, tmp_path, monkeypatch):
        ctx = tmp_path / "empty.md"
        ctx.write_text("   \n")
        monkeypatch.setenv("QMF_POOL_CONTEXT_PATH", str(ctx))
        assert "历史挖掘记忆" not in self._wrapper()._build_prompt_suffix()


class _FakeProc:
    """最小 Popen 桩：立即退出 0；记下 env 供断言。"""

    last: _FakeProc | None = None

    def __init__(self, cmd, **kwargs):
        self.cmd = cmd
        self.env = kwargs.get("env") or {}
        self.cwd = kwargs.get("cwd")
        self.pid = 4242
        self.returncode = 0
        _FakeProc.last = self

    def poll(self):
        return 0

    def wait(self, timeout=None):
        return 0


def _launcher_for(task: EvolutionTask, tmp_path) -> AlphaAgentLauncher:
    obj = AlphaAgentLauncher.__new__(AlphaAgentLauncher)
    obj._tasks = {task.task_id: task}
    obj._log_dir = tmp_path / "logs"
    obj._log_dir.mkdir(parents=True, exist_ok=True)
    obj._collect_results = lambda t, d: {}  # type: ignore[method-assign]
    return obj


class TestLauncherInjectionWiring:
    def _run(self, tmp_path, monkeypatch, injection):
        task = EvolutionTask(
            task_id="t-inject", user_id="u-1", market="a_share", universe="csi300"
        )
        launcher = _launcher_for(task, tmp_path)
        calls: dict[str, object] = {}

        async def fake_prepare(**kwargs):
            calls["prepare"] = kwargs
            return injection

        async def fake_mark(ids):
            calls["mark"] = tuple(ids)
            return len(ids)

        monkeypatch.setattr(pool_service, "prepare_injection", fake_prepare)
        monkeypatch.setattr(pool_service, "mark_retrieved", fake_mark)
        monkeypatch.setattr(launcher_mod.subprocess, "Popen", _FakeProc)

        asyncio.run(
            launcher._run_evolution(
                task, loop_n=1, seed="s", provider_uri="/tmp/x", direction=""
            )
        )
        return task, launcher, calls

    def test_injection_path_lands_in_child_env_and_fatigue_counts(
        self, tmp_path, monkeypatch
    ):
        ctx = tmp_path / "pool_context.md"
        ctx.write_text("digest")
        injection = pool_service.PoolInjection(path=ctx, factor_ids=("f1", "f2"))

        task, launcher, calls = self._run(tmp_path, monkeypatch, injection)

        assert task.status == TaskStatus.COMPLETED
        assert _FakeProc.last is not None
        assert _FakeProc.last.env.get("QMF_POOL_CONTEXT_PATH") == str(ctx)
        prepared = calls["prepare"]
        assert prepared["user_id"] == "u-1"
        assert prepared["market"] == "a_share"
        assert prepared["universe"] == "csi300"
        assert prepared["task_id"] == "t-inject", "本任务必须排除（防自注）"
        assert prepared["log_dir"] == launcher._log_dir / "t-inject"
        assert calls["mark"] == ("f1", "f2"), "spawn 成功后疲劳计数没打通"

    def test_no_injection_means_no_env_and_no_fatigue(self, tmp_path, monkeypatch):
        task, _launcher, calls = self._run(
            tmp_path, monkeypatch, pool_service.PoolInjection(path=None)
        )
        assert task.status == TaskStatus.COMPLETED
        assert "QMF_POOL_CONTEXT_PATH" not in _FakeProc.last.env
        assert "mark" not in calls, "空注入不该记疲劳"
