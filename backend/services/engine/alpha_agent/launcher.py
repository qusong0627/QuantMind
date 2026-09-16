"""AlphaAgent / RD-Agent 因子挖掘任务启动器

支持两种模式:
1. Legacy AlphaAgent (market=a_share, 使用 alphaagent/)
2. RD-Agent 多市场 (market=a_share|crypto|hong_kong|us_stock, 使用 rdagent/ + market_adapters/)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class EvolutionTask:
    task_id: str
    user_id: str
    market: str = "a_share"
    data_source: str = ""
    universe: str = "csi300"
    status: TaskStatus = TaskStatus.PENDING
    progress: str = ""
    phase: str = "pending"
    progress_pct: int = 0
    loop_n: int = 5
    current_loop: int = 0
    created_at: float = field(default_factory=time.time)
    error_message: str | None = None
    result: dict[str, Any] | None = None
    process: subprocess.Popen | None = None
    _cancel_requested: bool = False
    timeline: list[dict[str, Any]] = field(default_factory=list)
    token_usage: dict[str, Any] = field(default_factory=dict)


class AlphaAgentLauncher:
    """Launches factor evolution tasks via the RD-Agent runner."""

    _RD_AGENT_RUNNER_SCRIPT = str(
        Path(__file__).resolve().parent.parent.parent.parent.parent / "scripts" / "alpha_agent" / "run_rd_agent.py"
    )

    def __init__(self) -> None:
        self._tasks: dict[str, EvolutionTask] = {}
        self._log_dir = Path(os.getenv("LOG_TRACE_PATH", "/tmp/alpha_agent_logs"))
        self._log_dir.mkdir(parents=True, exist_ok=True)
        self._load_tasks()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def start_evolution(
        self,
        user_id: str,
        *,
        market: str = "a_share",
        universe: str = "csi300",
        loop_n: int = 5,
        seed: str | None = None,
        provider_uri: str | None = None,
        direction: str | None = None,
        data_source: str | None = None,
        llm_overrides: dict[str, str] | None = None,
    ) -> str:
        """Start a factor evolution task. Returns task_id.

        llm_overrides: 用户级 LLM 环境变量覆盖（如个人中心配置的 API Key），
        优先于容器全局 env 注入子进程。
        """
        from backend.services.engine.alpha_agent.hw_lock import assert_factor_mining_hardware

        assert_factor_mining_hardware()

        task_id = uuid.uuid4().hex[:16]
        task = EvolutionTask(
            task_id=task_id, user_id=user_id, market=market,
            universe=universe, loop_n=loop_n, data_source=data_source or "",
        )
        self._tasks[task_id] = task

        # Determine provider URI from market adapter if not specified
        if not provider_uri:
            try:
                from backend.services.engine.rd_agent.market_adapters import get_adapter
                adapter = get_adapter(market)
                provider_uri = adapter.get_qlib_provider_uri()
            except Exception:
                provider_uri = os.getenv("QLIB_PROVIDER_URI", "/data/qlib/cn_data")

        # Override provider URI based on data_source
        if data_source:
            ds = data_source.lower().strip()
            if ds == "parquet":
                from backend.services.engine.rd_agent.rd_loop_wrapper import RDLoopWrapper
                quantdb_dir = RDLoopWrapper._resolve_quantdb_dir()
                if quantdb_dir:
                    # 从 QuantDB parquet 构建或更新 Qlib 缓存
                    from backend.services.engine.qlib_data_builder import ensure_qlib_cache
                    provider_uri = ensure_qlib_cache(quantdb_dir)
                else:
                    provider_uri = "/app/db/feature_snapshots"
            elif ds == "pg":
                provider_uri = "postgresql://localhost:5432/quantmind"
            # qlib_bin uses the default provider_uri

        seed_path = seed or self._default_seed_path()

        asyncio.ensure_future(
            self._run_evolution(
                task,
                loop_n=loop_n,
                seed=seed_path,
                provider_uri=provider_uri,
                direction=direction or "",
                llm_overrides=llm_overrides,
            )
        )
        return task_id

    async def get_task_status(self, task_id: str) -> dict[str, Any] | None:
        task = self._tasks.get(task_id)
        if not task:
            return None
        return {
            "task_id": task.task_id,
            "user_id": task.user_id,
            "status": task.status.value,
            "progress": task.progress,
            "phase": task.phase,
            "progress_pct": task.progress_pct,
            "current_loop": task.current_loop,
            "loop_n": task.loop_n,
            "market": task.market,
            "universe": task.universe,
            "data_source": task.data_source,
            "error_message": task.error_message,
            "result": task.result,
            "timeline": task.timeline,
            "token_usage": task.token_usage,
        }

    async def cancel_task(self, task_id: str) -> bool:
        task = self._tasks.get(task_id)
        if not task:
            return False
        if task.status not in (TaskStatus.RUNNING, TaskStatus.PENDING):
            return False
        task._cancel_requested = True
        if task.process and task.process.poll() is None:
            try:
                pgid = os.getpgid(task.process.pid)
                os.killpg(pgid, signal.SIGTERM)
                # Wait up to 5 seconds, then SIGKILL
                for _ in range(10):
                    if task.process.poll() is not None:
                        break
                    await asyncio.sleep(0.5)
                if task.process.poll() is None:
                    os.killpg(pgid, signal.SIGKILL)
                    logger.warning("Force-killed process group %d for task %s", pgid, task_id)
            except ProcessLookupError:
                pass
            except Exception as e:
                logger.warning("Failed to kill process for task %s: %s", task_id, e)
        task.status = TaskStatus.FAILED
        task.error_message = "Cancelled by user"
        self._persist_task(task)
        return True

    async def get_task_log(self, task_id: str, tail: int = 0) -> str | None:
        """Get subprocess stdout log for a task.

        Args:
            tail: If 0, return the full file. If > 0, return the last N lines.
        """
        task = self._tasks.get(task_id)
        if not task:
            return None
        log_file = self._log_dir / task_id / "subprocess_stdout.log"
        if not log_file.exists():
            return None
        try:
            with open(log_file, errors="replace") as f:
                lines = f.readlines()
            if tail > 0:
                return "".join(lines[-tail:])
            return "".join(lines)
        except Exception:
            return None

    async def list_tasks(self, user_id: str | None = None) -> list[dict[str, Any]]:
        results = []
        for task in self._tasks.values():
            if user_id and task.user_id != user_id:
                continue
            results.append(await self.get_task_status(task.task_id))
        return results

    def count_running(self) -> dict[str, Any]:
        """统计 pending/running 任务数（全局 + 按用户），供并发上限校验。"""
        active = (TaskStatus.PENDING, TaskStatus.RUNNING)
        by_user: dict[str, int] = {}
        total = 0
        for task in self._tasks.values():
            if task.status in active:
                total += 1
                by_user[task.user_id] = by_user.get(task.user_id, 0) + 1
        return {"global": total, "by_user": by_user}

    def _persist_task(self, task: EvolutionTask) -> None:
        """Save task state to disk so it survives restarts."""
        state_file = self._log_dir / task.task_id / "task_state.json"
        try:
            state_file.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "task_id": task.task_id,
                "user_id": task.user_id,
                "market": task.market,
                "data_source": task.data_source,
                "universe": task.universe,
                "status": task.status.value,
                "progress": task.progress,
                "phase": task.phase,
                "progress_pct": task.progress_pct,
                "loop_n": task.loop_n,
                "current_loop": task.current_loop,
                "created_at": task.created_at,
                "error_message": task.error_message,
                "timeline": task.timeline,
                "token_usage": task.token_usage,
            }
            with open(state_file, "w") as f:
                json.dump(data, f, ensure_ascii=False, default=str)
        except Exception as e:
            logger.warning("Failed to persist task %s: %s", task.task_id, e)

    def _load_tasks(self) -> None:
        """Reload task states from disk on startup."""
        try:
            for task_dir in self._log_dir.iterdir():
                state_file = task_dir / "task_state.json"
                if not state_file.exists():
                    continue
                try:
                    with open(state_file) as f:
                        data = json.load(f)
                    task = EvolutionTask(
                        task_id=data["task_id"],
                        user_id=data["user_id"],
                        market=data.get("market", "a_share"),
                        data_source=data.get("data_source", ""),
                        universe=data.get("universe", "csi300"),
                        status=TaskStatus(data.get("status", "pending")),
                        progress=data.get("progress", ""),
                        phase=data.get("phase", "pending"),
                        progress_pct=data.get("progress_pct", 0),
                        loop_n=data.get("loop_n", 3),
                        current_loop=data.get("current_loop", 0),
                        created_at=data.get("created_at", time.time()),
                        error_message=data.get("error_message"),
                        timeline=data.get("timeline", []),
                        token_usage=data.get("token_usage", {}),
                    )
                    # Running tasks at startup are likely orphaned
                    if task.status == TaskStatus.RUNNING:
                        task.status = TaskStatus.FAILED
                        task.error_message = "Server restarted while task was running"
                    self._tasks[task.task_id] = task
                except Exception as e:
                    logger.warning("Failed to load task from %s: %s", state_file, e)
            if self._tasks:
                logger.info("Loaded %d tasks from disk", len(self._tasks))
        except Exception as e:
            logger.warning("Failed to load tasks: %s", e)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _default_seed_path(self) -> str:
        in_container = Path("/app/alphaagent/scenarios/qlib/experiment/factor_data_template")
        if in_container.exists():
            return str(in_container)
        project = os.getenv("HOST_PROJECT_PATH", "/opt/quantmind")
        template = Path(project) / "alphaagent" / "scenarios" / "qlib" / "experiment" / "factor_data_template"
        return str(template)

    async def _run_evolution(
        self,
        task: EvolutionTask,
        *,
        loop_n: int,
        seed: str,
        provider_uri: str,
        direction: str = "",
        llm_overrides: dict[str, str] | None = None,
    ) -> None:
        task.status = TaskStatus.RUNNING
        task.phase = "starting"
        task.progress_pct = 2
        task.progress = "正在启动因子挖掘..."
        self._persist_task(task)

        task_log_dir = self._log_dir / task.task_id
        task_log_dir.mkdir(parents=True, exist_ok=True)

        # Build environment
        openai_base = (
            os.getenv("OPENAI_BASE_URL")
            or os.getenv("OPENAI_API_BASE")
            or ""
        )
        openai_api_key = (
            os.getenv("AI_IDE_LLM_API_KEY")
            or os.getenv("AI_IDE_API_KEY")
            or os.getenv("OPENAI_API_KEY")
            or ""
        )
        chat_model = os.getenv("CHAT_MODEL", "")
        system_prompt = os.getenv("ALPHA_AGENT_SYSTEM_PROMPT", "")

        # 用户级 LLM 配置（如个人中心「AI 服务配置」）优先于容器全局 env
        if llm_overrides:
            openai_base = llm_overrides.get("OPENAI_BASE_URL", openai_base)
            openai_api_key = llm_overrides.get("OPENAI_API_KEY", openai_api_key)
            chat_model = llm_overrides.get("CHAT_MODEL", chat_model)

        env = {
            **os.environ,
            "PYTHONPATH": os.getenv("PYTHONPATH") or "/app",
            "LOG_TRACE_PATH": str(task_log_dir),
            "QLIB_PROVIDER_URI": provider_uri,
            "QLIB_FACTOR_UNIVERSE": task.universe,
            "REASONING_MODEL": chat_model,
            "CHAT_STREAM": "false",
            # 回测数据从 2016 年开始 (默认 2008 太慢)
            "QLIB_FACTOR_TRAIN_START": os.getenv("QLIB_FACTOR_TRAIN_START", "2016-01-01"),
            "QLIB_FACTOR_VALID_START": os.getenv("QLIB_FACTOR_VALID_START", "2021-01-01"),
            "QLIB_FACTOR_VALID_END": os.getenv("QLIB_FACTOR_VALID_END", "2022-12-31"),
            "QLIB_FACTOR_TEST_START": os.getenv("QLIB_FACTOR_TEST_START", "2023-01-01"),
            "QLIB_FACTOR_TEST_END": os.getenv("QLIB_FACTOR_TEST_END", "2025-12-31"),
            # 因子处理并行数
            "MULTI_PROC_N": os.getenv("MULTI_PROC_N", "4"),
        }
        if system_prompt:
            env["DEFAULT_SYSTEM_PROMPT"] = system_prompt
        if openai_base:
            env["OPENAI_BASE_URL"] = openai_base
        if openai_api_key:
            env["OPENAI_API_KEY"] = openai_api_key
        if chat_model:
            env["CHAT_MODEL"] = chat_model

        # 补齐 RD-Agent litellm 后端需要的 LITELLM_ 前缀变量（deepseek 优先）
        if llm_overrides:
            # 显式写入 LITELLM_ 覆盖，绕开 build_llm_env 里 os.getenv 的 mock 占位符链
            for _k in ("LITELLM_OPENAI_API_KEY", "LITELLM_OPENAI_API_BASE"):
                if llm_overrides.get(_k):
                    env[_k] = llm_overrides[_k]
        from backend.services.engine.rd_agent.llm_env import build_llm_env
        build_llm_env(env)

        # Add market adapter env overrides (RD-Agent runner used for all markets)
        try:
            from backend.services.engine.rd_agent.market_adapters import get_adapter
            adapter = get_adapter(task.market)
            adapter_env = adapter.get_env_overrides()
            env.update(adapter_env)
        except Exception as e:
            logger.warning("Failed to get market adapter env: %s", e)

        # RD-Agent runner for all markets
        runner_script = self._RD_AGENT_RUNNER_SCRIPT

        cmd = [
            sys.executable,
            "-X", "faulthandler",  # 段错误时输出 Python traceback 便于定位
            runner_script,
            "--task-id", task.task_id,
            "--user-id", task.user_id,
            "--loop-n", str(loop_n),
            "--log-dir", str(task_log_dir),
            "--direction", direction,
            "--universe", task.universe,
            "--market", task.market,
        ]

        logger.info("Starting factor mining: market=%s, script=%s", task.market, runner_script)
        logger.info("Command: %s", " ".join(cmd))

        stdout_log = task_log_dir / "subprocess_stdout.log"

        try:
            log_fh = open(stdout_log, "w")
            process = subprocess.Popen(
                cmd,
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                env=env,
                cwd=str(task_log_dir),
                # start_new_session 等价于 setsid（进程组隔离，取消时可 killpg），
                # 但比 preexec_fn=os.setsid 安全（preexec_fn 在多线程 asyncio 环境会段错误 -11）
                start_new_session=True,
            )
            task.process = process
            self._persist_task(task)

            while process.poll() is None:
                if task._cancel_requested:
                    try:
                        pgid = os.getpgid(process.pid)
                        os.killpg(pgid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    break
                self._update_progress(task, task_log_dir)
                await asyncio.sleep(3)

            if process.poll() is None:
                process.wait(timeout=30)

            try:
                log_fh.close()
            except Exception:
                pass

            if process.returncode == 0:
                task.status = TaskStatus.COMPLETED
                task.phase = "completed"
                task.progress_pct = 100
                task.progress = "因子挖掘完成"
                task.result = self._collect_results(task, task_log_dir)
            else:
                task.status = TaskStatus.FAILED
                error_output = self._tail_error_log(task_log_dir)
                task.error_message = f"Process exited with code {process.returncode}: {error_output}"
                logger.error("Factor mining failed for task %s: %s", task.task_id, task.error_message)

            self._persist_task(task)

        except Exception as e:
            task.status = TaskStatus.FAILED
            task.error_message = str(e)
            logger.exception("Factor mining exception for task %s", task.task_id)
            self._persist_task(task)

    _PHASE_ORDER = [
        ("scenario", "scenario", "初始化场景"),
        ("hypothesis generation", "hypothesis", "生成假设"),
        ("hypothesis generator", "hypothesis", "生成假设"),
        ("experiment generation", "experiment", "设计实验"),
        ("evolving code", "coder", "进化编写代码"),
        ("coder", "coder", "编写因子代码"),
        ("coding", "coder", "编写因子代码"),
        ("runner", "runner", "回测运行因子"),
        ("summarizer", "summarizer", "总结结果"),
    ]

    @staticmethod
    def _find_active_phase(root: Path) -> tuple[str, str, str] | None:
        best: tuple[float, tuple[str, str, str]] = (-1.0, ("", "", ""))
        # Search paths: direct children + direct_exp_gen/ subdirectory
        search_roots = [root]
        deg = root / "direct_exp_gen"
        if deg.is_dir():
            search_roots.append(deg)
        for search_root in search_roots:
            for sub_name, key, label in AlphaAgentLauncher._PHASE_ORDER:
                sub = search_root / sub_name
                if not sub.is_dir():
                    continue
                newest = -1.0
                try:
                    for f in sub.rglob("*"):
                        if f.is_file():
                            try:
                                mt = f.stat().st_mtime
                            except OSError:
                                continue
                            if mt > newest:
                                newest = mt
                except OSError:
                    continue
                if newest > best[0]:
                    best = (newest, (sub_name, key, label))
        return best[1] if best[0] > 0 else None

    def _update_progress(self, task: EvolutionTask, log_dir: Path) -> None:
        r_dir = log_dir / "r"
        d_dir = log_dir / "d"
        loop_dirs = sorted(
            [p for p in log_dir.glob("Loop_*") if p.is_dir()],
            key=lambda p: int(p.name.split("_", 1)[1]) if p.name.split("_", 1)[1].isdigit() else 0,
        )

        candidates: list[tuple[int, Path]] = []
        if r_dir.is_dir():
            candidates.append((0, r_dir))
        if d_dir.is_dir():
            candidates.append((1, d_dir))
        for ld in loop_dirs:
            try:
                idx = int(ld.name.split("_", 1)[1])
            except (IndexError, ValueError):
                continue
            candidates.append((idx, ld))

        if not candidates:
            task.phase = "starting"
            task.progress_pct = 2
            task.progress = "正在启动因子挖掘..."
            return

        best_candidate = candidates[0]
        best_mtime = -1.0
        for loop_idx, cpath in candidates:
            newest = -1.0
            try:
                for f in cpath.rglob("*"):
                    if f.is_file():
                        try:
                            mt = f.stat().st_mtime
                        except OSError:
                            continue
                        if mt > newest:
                            newest = mt
            except OSError:
                continue
            if newest > best_mtime:
                best_mtime = newest
                best_candidate = (loop_idx, cpath)

        task.current_loop = best_candidate[0]
        phase_root = best_candidate[1]

        result = self._find_active_phase(phase_root)
        if result is None:
            task.phase = "starting"
            task.progress_pct = 5
            task.progress = (
                f"Loop {task.current_loop}/{task.loop_n} — 准备中..."
                if task.current_loop
                else "首轮启动中..."
            )
            return

        sub_name, key, label = result
        task.phase = key
        try:
            phase_idx = [p[0] for p in self._PHASE_ORDER].index(sub_name)
        except ValueError:
            phase_idx = 0
        phase_frac = (phase_idx + 1) / len(self._PHASE_ORDER)

        total_units = max(task.loop_n + 1, 1)
        loop_frac = (task.current_loop + phase_frac) / total_units
        task.progress_pct = max(5, min(99, int(loop_frac * 100)))

        loop_tag = (
            "首轮"
            if task.current_loop == 0
            else f"Loop {task.current_loop}/{task.loop_n}"
        )
        task.progress = f"{loop_tag} — {label}"

        # 构建详细时间线
        task.timeline = self._build_timeline(log_dir, task.loop_n)
        task.token_usage = self._aggregate_token_usage(log_dir)

    _PHASE_DIR_MAP = {
        "hypothesis generation": ("hypothesis", "生成假设"),
        "experiment generation": ("experiment", "设计实验"),
        "coder": ("coder", "编写因子代码"),
        "coding": ("coder", "编写因子代码"),
        "runner": ("runner", "回测运行"),
        "running": ("runner", "回测运行"),
        "feedback": ("feedback", "总结反馈"),
    }

    @staticmethod
    def _load_pkl(path: Path) -> Any:
        try:
            import pickle
            with open(path, "rb") as f:
                return pickle.load(f)
        except Exception:
            return None

    def _build_timeline(self, log_dir: Path, loop_n: int) -> list[dict[str, Any]]:
        """从日志目录构建详细时间线"""
        timeline: list[dict[str, Any]] = []

        loop_dirs = sorted(
            [p for p in log_dir.glob("Loop_*") if p.is_dir()],
            key=lambda p: int(p.name.split("_", 1)[1]) if p.name.split("_", 1)[1].isdigit() else 0,
        )

        for loop_dir in loop_dirs:
            try:
                loop_idx = int(loop_dir.name.split("_", 1)[1])
            except (IndexError, ValueError):
                continue

            loop_entry: dict[str, Any] = {
                "loop": loop_idx,
                "label": "首轮" if loop_idx == 0 else f"Loop {loop_idx}/{loop_n}",
                "phases": [],
                "status": "running",
            }

            # direct_exp_gen 下有 hypothesis generation + experiment generation
            deg = loop_dir / "direct_exp_gen"
            if deg.is_dir():
                for phase_dir_name in ("hypothesis generation", "experiment generation"):
                    phase_dir = deg / phase_dir_name
                    if phase_dir.is_dir():
                        phase_info = self._extract_phase_info(phase_dir, phase_dir_name)
                        if phase_info:
                            loop_entry["phases"].append(phase_info)

            # coding
            coding_dir = loop_dir / "coding"
            if coding_dir.is_dir():
                phase_info = self._extract_phase_info(coding_dir, "coding")
                if phase_info:
                    # 提取编码中的因子名
                    phase_info["factors"] = self._extract_coding_factors(coding_dir)
                    loop_entry["phases"].append(phase_info)

            # running
            running_dir = loop_dir / "running"
            if running_dir.is_dir():
                phase_info = self._extract_phase_info(running_dir, "running")
                if phase_info:
                    loop_entry["phases"].append(phase_info)

            # feedback
            feedback_dir = loop_dir / "feedback"
            if feedback_dir.is_dir():
                phase_info = self._extract_phase_info(feedback_dir, "feedback")
                if phase_info:
                    loop_entry["phases"].append(phase_info)

            # 判断 loop 状态
            has_feedback = any(p.get("key") == "feedback" and p.get("status") == "completed" for p in loop_entry["phases"])
            has_runner = any(p.get("key") == "runner" for p in loop_entry["phases"])
            if has_feedback:
                loop_entry["status"] = "completed"
            elif has_runner:
                loop_entry["status"] = "backtesting"
            else:
                loop_entry["status"] = "running"

            timeline.append(loop_entry)

        return timeline

    def _extract_phase_info(self, phase_dir: Path, dir_name: str) -> dict[str, Any] | None:
        """从阶段目录提取时间信息"""
        key, label = self._PHASE_DIR_MAP.get(dir_name, (dir_name, dir_name))

        # 找 time_info pickle
        time_info_files = list(phase_dir.glob("**/time_info/**/*.pkl"))
        start_time = None
        end_time = None
        duration_s = None

        if time_info_files:
            data = self._load_pkl(time_info_files[0])
            if isinstance(data, dict):
                start_time = data.get("start_time")
                end_time = data.get("end_time")
                if start_time and end_time:
                    duration_s = (end_time - start_time).total_seconds()

        # 如果没有 time_info, 用文件 mtime 推断
        if not start_time:
            try:
                files = sorted(phase_dir.rglob("*"), key=lambda f: f.stat().st_mtime)
                if files:
                    start_time = datetime.fromtimestamp(files[0].stat().st_mtime, tz=timezone.utc)
                    end_time = datetime.fromtimestamp(files[-1].stat().st_mtime, tz=timezone.utc)
                    duration_s = (end_time - start_time).total_seconds()
            except Exception:
                pass

        # 找 token_cost
        token_files = list(phase_dir.glob("**/token_cost/**/*.pkl"))
        tokens = {"prompt": 0, "completion": 0, "calls": 0}
        for tf in token_files:
            data = self._load_pkl(tf)
            if isinstance(data, dict):
                tokens["prompt"] += data.get("prompt_tokens", 0) or 0
                tokens["completion"] += data.get("completion_tokens", 0) or 0
                tokens["calls"] += 1

        # 判断状态
        status = "completed" if end_time else ("running" if start_time else "pending")

        entry: dict[str, Any] = {
            "key": key,
            "label": label,
            "status": status,
            "start_time": start_time.isoformat() if start_time else None,
            "end_time": end_time.isoformat() if end_time else None,
            "duration_s": round(duration_s, 1) if duration_s else None,
        }
        if tokens["calls"] > 0:
            entry["tokens"] = tokens
        return entry

    def _extract_coding_factors(self, coding_dir: Path) -> list[str]:
        """从 coding 目录提取正在编码的因子名"""
        factors: list[str] = []
        # 实验生成 pkl 里有因子名
        for pkl_path in coding_dir.glob("**/experiment generation/**/*.pkl"):
            data = self._load_pkl(pkl_path)
            if isinstance(data, list):
                for t in data:
                    name = getattr(t, "factor_name", None) or getattr(t, "name", None)
                    if name:
                        factors.append(name)
            elif data is not None:
                name = getattr(data, "factor_name", None) or getattr(data, "name", None)
                if name:
                    factors.append(name)
        return factors

    @staticmethod
    def _aggregate_token_usage(log_dir: Path) -> dict[str, Any]:
        """汇总所有 LLM token 用量"""
        total_prompt = 0
        total_completion = 0
        total_calls = 0
        models: set[str] = set()

        for pkl_path in log_dir.glob("**/token_cost/**/*.pkl"):
            try:
                import pickle
                with open(pkl_path, "rb") as f:
                    data = pickle.load(f)
                if isinstance(data, dict):
                    total_prompt += data.get("prompt_tokens", 0) or 0
                    total_completion += data.get("completion_tokens", 0) or 0
                    total_calls += 1
                    if data.get("model"):
                        models.add(data["model"])
            except Exception:
                continue

        return {
            "total_prompt_tokens": total_prompt,
            "total_completion_tokens": total_completion,
            "total_calls": total_calls,
            "models": list(models),
        }

    def _collect_results(self, task: EvolutionTask, log_dir: Path) -> dict[str, Any]:
        """Collect results from result.json or log dir."""
        result_file = log_dir / "result.json"
        if result_file.exists():
            try:
                return json.loads(result_file.read_text())
            except Exception:
                pass
        return {
            "total_factors": 0,
            "log_dir": str(log_dir),
            "task_id": task.task_id,
            "market": task.market,
            "message": "Factors persisted to DB by runner script",
        }

    @staticmethod
    def _tail_error_log(log_dir: Path, max_chars: int = 2000) -> str:
        try:
            logs = sorted(log_dir.rglob("common_logs.log"), key=lambda p: p.stat().st_mtime, reverse=True)
            if not logs:
                return ""
            with open(logs[0], errors="replace") as f:
                return f.read()[-max_chars:]
        except Exception:
            return ""


# Singleton
_launcher: AlphaAgentLauncher | None = None


def get_launcher() -> AlphaAgentLauncher:
    global _launcher
    if _launcher is None:
        _launcher = AlphaAgentLauncher()
    return _launcher
