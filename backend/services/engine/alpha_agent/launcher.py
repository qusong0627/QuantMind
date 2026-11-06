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


def _normalize_seed_factors(value: Any) -> list[dict[str, str]]:
    """Validate bounded, immutable owner-selected factor parent snapshots."""
    from collections.abc import Mapping

    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 3:
        raise ValueError("Invalid parent factor snapshot")
    output: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in value:
        if not isinstance(row, Mapping) or set(row) - {
            "factor_id",
            "factor_name",
            "factor_formulation",
        }:
            raise ValueError("Invalid parent factor snapshot")
        factor_id = row.get("factor_id")
        if not isinstance(factor_id, str) or not factor_id.strip() or len(factor_id) > 256:
            raise ValueError("Invalid parent factor snapshot")
        factor_id = factor_id.strip()
        if factor_id in seen:
            raise ValueError("Parent factor IDs must be unique")
        seen.add(factor_id)
        name = row.get("factor_name")
        formulation = row.get("factor_formulation")
        if (name is not None and not isinstance(name, str)) or (
            formulation is not None and not isinstance(formulation, str)
        ):
            raise ValueError("Invalid parent factor snapshot")
        output.append({
            "factor_id": factor_id,
            "factor_name": (name or "")[:128],
            "factor_formulation": (formulation or "")[:1200],
        })
    return output


def _mining_windows_from_params(value: Any) -> dict[str, str | None]:
    """Extract and validate date fields from the immutable task snapshot."""
    from collections.abc import Mapping

    params = value
    if isinstance(params, str):
        try:
            params = json.loads(params)
        except (TypeError, ValueError):
            params = {}
    if not isinstance(params, Mapping):
        params = {}
    windows = {}
    for key in ("mining_start", "mining_end", "valid_start", "valid_end"):
        value = params.get(key)
        if value is not None and not isinstance(value, str):
            raise ValueError("Persisted factor mining window is invalid")
        windows[key] = value or None
    from backend.services.engine.alpha_agent.mining_windows import build_mining_window_env

    build_mining_window_env(**windows)
    return windows


class TaskStatus(str, Enum):
    PENDING = "pending"
    QUEUED = "queued"
    RUNNING = "running"
    STOPPING = "stopping"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class EvolutionTask:
    task_id: str
    user_id: str
    tenant_id: str = "default"
    market: str = "a_share"
    data_source: str = ""
    datasets: tuple[str, ...] = ()
    mining_start: str | None = None
    mining_end: str | None = None
    valid_start: str | None = None
    valid_end: str | None = None
    universe: str = "csi300"
    universe_symbols: list[str] | None = None
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
    _cancel_confirmed: bool = False
    timeline: list[dict[str, Any]] = field(default_factory=list)
    token_usage: dict[str, Any] = field(default_factory=dict)
    mining_progress: dict[str, Any] = field(default_factory=dict)


class AlphaAgentLauncher:
    """Launches factor evolution tasks via the RD-Agent runner."""

    _RD_AGENT_RUNNER_SCRIPT = str(
        Path(__file__).resolve().parent.parent.parent.parent.parent / "scripts" / "alpha_agent" / "run_rd_agent.py"
    )

    @staticmethod
    def _process_group_has_live_process(pgid: int) -> bool:
        """Check /proc for non-zombie members of an isolated child process group."""
        proc_root = Path("/proc")
        if not proc_root.is_dir():
            return True
        try:
            entries = list(proc_root.iterdir())
        except OSError:
            return True
        for entry in entries:
            if not entry.name.isdigit():
                continue
            try:
                stat = (entry / "stat").read_text()
                fields = stat[stat.rindex(")") + 2:].split()
                if fields[0] != "Z" and int(fields[2]) == pgid:
                    return True
            except (OSError, ValueError, IndexError):
                continue
        return False

    @classmethod
    async def _terminate_process_group(cls, pgid: int) -> bool:
        # OSS-ADAPT: Windows 无 killpg，按“无法确认”降级（本地开发调试用）。
        if not hasattr(os, "killpg"):
            return False
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            return not cls._process_group_has_live_process(pgid)
        deadline = time.monotonic() + 30
        while cls._process_group_has_live_process(pgid) and time.monotonic() < deadline:
            await asyncio.sleep(0.25)
        if cls._process_group_has_live_process(pgid):
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            deadline = time.monotonic() + 30
            while cls._process_group_has_live_process(pgid) and time.monotonic() < deadline:
                await asyncio.sleep(0.25)
        return not cls._process_group_has_live_process(pgid)

    @staticmethod
    def _safe_name(text: str) -> str:
        return (
            "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in str(text or ""))
            or "default"
        )

    def task_dir(
        self, task_id: str, tenant_id: str = "default", user_id: str = ""
    ) -> Path:
        from backend.shared.owner_path import owner_segment, user_scoped_dir

        return user_scoped_dir(self._log_dir, tenant_id, user_id) / owner_segment(
            task_id
        )

    def _resolve_task_dir(self, task: EvolutionTask) -> Path:
        """优先分片路径，其次兼容历史遗留布局。"""
        from backend.shared.owner_path import user_scoped_dir, owner_segment

        sharded = self.task_dir(task.task_id, task.tenant_id, task.user_id)
        if sharded.exists():
            return sharded
        old_sharded = (
            self._log_dir
            / f"t{self._safe_name(task.tenant_id)}"
            / f"u{self._safe_name(task.user_id)}"
            / task.task_id
        )
        if old_sharded.exists():
            return old_sharded
        legacy = self._log_dir / task.task_id
        # OSS-ADAPT: FACTOR_MINING_NODE_URL（远端节点历史路径）OSS 不支持，不回退。
        return sharded if not legacy.exists() else legacy

    @staticmethod
    def _celery_enabled() -> bool:
        return os.getenv("FACTOR_MINING_CELERY", "false").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

    def __init__(self) -> None:
        self._tasks: dict[str, EvolutionTask] = {}
        self._log_dir = self._resolve_log_dir()
        self._log_dir.mkdir(parents=True, exist_ok=True)
        self._load_tasks()

    @staticmethod
    def _resolve_log_dir() -> Path:
        """任务状态/日志目录：显式 LOG_TRACE_PATH > 容器内持久挂载 /data > /tmp。

        此前固定默认 /tmp/alpha_agent_logs，而 /tmp 在容器可写层、未挂载卷，
        容器一旦 --force-recreate 重建，全部挖掘历史记录随之丢失（前端「看不到记录」）。
        """
        env_dir = os.getenv("LOG_TRACE_PATH", "").strip()
        if env_dir:
            return Path(env_dir)
        # 仅 POSIX 下判断 /data：Windows 上 Path("/data") 会解析成当前盘符根目录（D:\data），
        # 会把本地开发的任务日志写到盘根去。
        if os.name == "posix" and Path("/data").is_dir():
            return Path("/data/alpha_agent_logs")
        return Path("/tmp/alpha_agent_logs")

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

    @staticmethod
    def _require_task_store():
        # OSS-ADAPT: AlphaMiningTaskPersistence 已从企业版移植
        # （见 qlib_app/services/rd_agent_persistence.py 末尾）；保留 ImportError
        # 守卫以便缺表/缺依赖时仍报出可读错误。
        try:
            from backend.services.engine.qlib_app.services.rd_agent_persistence import (
                AlphaMiningTaskPersistence,
            )
        except ImportError as exc:
            raise RuntimeError(
                "OSS 缺失 AlphaMiningTaskPersistence，队列调度路径暂不可用"
            ) from exc
        return AlphaMiningTaskPersistence()

    async def run_evolution_blocking(
        self,
        *,
        task_id: str,
        tenant_id: str,
        user_id: str,
        market: str,
        universe: str,
        loop_n: int,
        direction: str = "",
        data_source: str = "",
        llm_overrides: dict[str, str] | None = None,
    ) -> dict[str, Any] | None:
        """worker 内阻塞执行一次挖掘（供 Celery 任务调用），状态回写 DB。"""
        from backend.shared.owner_scope import OwnerScope

        owner = OwnerScope(tenant_id, user_id)
        tenant_id, user_id = owner.tenant_id, owner.user_id
        if market != "a_share":
            raise ValueError("Only A-share mining is supported")

        store = self._require_task_store()
        record = await store.get_for_dispatch(task_id)
        if not record or record.get("tenant_id") != owner.tenant_id or record.get("user_id") != owner.user_id:
            raise ValueError("Persisted factor mining owner does not match the worker")
        params_snapshot = record.get("params_snapshot") or {}
        if isinstance(params_snapshot, str):
            params_snapshot = json.loads(params_snapshot)
        if not isinstance(params_snapshot, dict):
            raise ValueError("Persisted factor mining parameters are invalid")
        seed_factors = _normalize_seed_factors(params_snapshot.get("seed_factors"))
        from backend.services.engine.rd_agent.mining_datasets import resolve_datasets

        task_datasets = resolve_datasets(
            params_snapshot.get("datasets") or ["features_daily", "l1_factors"]
        )
        task_windows = _mining_windows_from_params(params_snapshot)
        source_snapshot = record.get("data_snapshot") or {}
        if isinstance(source_snapshot, str):
            source_snapshot = json.loads(source_snapshot)
        task = EvolutionTask(
            task_id=task_id,
            user_id=user_id,
            tenant_id=owner.tenant_id,
            market=market,
            universe=universe,
            loop_n=loop_n,
            data_source=data_source or "",
            datasets=task_datasets,
            universe_symbols=source_snapshot.get("universe_symbols"),
            **task_windows,
            status=TaskStatus.QUEUED,
            phase="queued",
            progress="等待前序任务完成",
        )
        self._tasks[task_id] = task
        if not llm_overrides:
            from backend.services.engine.alpha_agent.llm_client import (
                resolve_llm_config,
            )

            cfg = resolve_llm_config()
            llm_overrides = cfg.llm_env_overrides() if cfg else None
        provider_uri = await self._resolve_provider_uri(task, data_source)
        seed_path = self._default_seed_path()
        await self._run_evolution(
            task,
            loop_n=loop_n,
            seed=seed_path,
            provider_uri=provider_uri,
            direction=direction or "",
            seed_factors=seed_factors,
            llm_overrides=llm_overrides,
        )
        try:
            if task.status == TaskStatus.COMPLETED:
                await store.update(
                    task_id,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    status="completed",
                    progress="因子挖掘完成",
                )
            elif task.status == TaskStatus.CANCELLED:
                await store.update(
                    task_id,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    expected_status=("stopping",),
                    status="cancelled",
                    error_message="Cancelled by user",
                )
            else:
                await store.update(
                    task_id,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    status="failed",
                    error_message=(task.error_message or "")[:2000],
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[factor-mining] DB回写失败 %s: %s", task_id, exc)
        return task.result

    async def _resolve_provider_uri(
        self, task: EvolutionTask, data_source: str | None
    ) -> str:
        provider_uri = ""
        try:
            from backend.services.engine.rd_agent.market_adapters import get_adapter

            adapter = get_adapter("a_share")  # 仅CN/A股
            provider_uri = adapter.get_qlib_provider_uri()
        except Exception:
            provider_uri = os.getenv("QLIB_PROVIDER_URI", "/data/qlib/cn_data")
        if data_source:
            ds = data_source.lower().strip()
            if ds == "parquet":
                if os.getenv("QM_MINING_SHARED_CFS") == "1":
                    from backend.shared.qlib_paths import is_qlib_provider_ready

                    if not is_qlib_provider_ready(provider_uri):
                        raise ValueError("共享 CFS Qlib 数据不可用，请先完成数据同步")
                    return provider_uri
                from backend.services.engine.rd_agent.rd_loop_wrapper import (
                    RDLoopWrapper,
                )

                quantdb_dir = RDLoopWrapper._resolve_quantdb_dir()
                if quantdb_dir:
                    from backend.services.engine.qlib_data_builder import (
                        ensure_qlib_cache,
                    )

                    preparation = asyncio.create_task(
                        asyncio.to_thread(ensure_qlib_cache, quantdb_dir)
                    )
                    try:
                        provider_uri = await asyncio.shield(preparation)
                    except asyncio.CancelledError:
                        # Do not release serial occupancy while cache work still runs.
                        await preparation
                        raise
                else:
                    provider_uri = "/app/db/feature_snapshots"
            elif ds == "pg":
                provider_uri = "postgresql://localhost:5432/quantmind"
        return provider_uri

    async def run_queued(self, record: dict[str, Any]) -> None:
        from backend.shared.owner_scope import OwnerScope

        owner = OwnerScope(record.get("tenant_id"), record.get("user_id"))
        if any(record.get(key) != value for key, value in owner.sql_params().items()):
            raise ValueError("Persisted task owner is not canonical")
        if record.get("market") != "a_share":
            raise ValueError("Only A-share mining is supported")
        task = EvolutionTask(
            task_id=record["task_id"],
            tenant_id=record["tenant_id"],
            user_id=record["user_id"],
            market=record["market"],
            universe=record["universe"],
            loop_n=record["loop_n"],
            data_source=record.get("data_source") or "",
            **_mining_windows_from_params(record.get("params_snapshot")),
            status=TaskStatus.QUEUED,
            phase="queued",
            progress="等待前序任务完成",
        )
        self._tasks[task.task_id] = task
        request = (record.get("executor_json") or {}).get("request") or {}
        params_snapshot = record.get("params_snapshot") or {}
        if isinstance(params_snapshot, str):
            params_snapshot = json.loads(params_snapshot)
        if not isinstance(params_snapshot, dict):
            raise ValueError("Persisted factor mining parameters are invalid")
        seed_factors = _normalize_seed_factors(params_snapshot.get("seed_factors"))
        from backend.services.engine.rd_agent.mining_datasets import resolve_datasets

        task.datasets = resolve_datasets(
            params_snapshot.get("datasets") or ["features_daily", "l1_factors"]
        )
        task.mining_start = params_snapshot.get("mining_start")
        task.mining_end = params_snapshot.get("mining_end")
        task.valid_start = params_snapshot.get("valid_start")
        task.valid_end = params_snapshot.get("valid_end")
        source_snapshot = record.get("data_snapshot") or {}
        if isinstance(source_snapshot, str):
            source_snapshot = json.loads(source_snapshot)
        task.universe_symbols = source_snapshot.get("universe_symbols")
        from backend.services.engine.alpha_agent.llm_client import resolve_llm_config

        cfg = resolve_llm_config()
        await self._run_evolution_with_budget(
            task,
            loop_n=task.loop_n,
            seed=request.get("seed") or self._default_seed_path(),
            provider_uri=request.get("provider_uri") or "",
            direction=record.get("direction") or "",
            seed_factors=seed_factors,
            llm_overrides=cfg.llm_env_overrides() if cfg else None,
        )

    async def _run_evolution_with_budget(
        self,
        task: EvolutionTask,
        *,
        loop_n: int,
        seed: str,
        provider_uri: str,
        direction: str,
        seed_factors: list[dict[str, Any]],
        llm_overrides: dict[str, str] | None,
    ) -> None:
        """Wait for a durable execution slot before any local subprocess fork."""
        from backend.services.engine.alpha_agent.resource_budget import (
            ExecutionCapacityFull,
            begin_execution,
        )

        store = self._require_task_store()
        from backend.services.engine.alpha_agent.queue import capture_controller

        witness = await asyncio.to_thread(capture_controller)
        try:
            with begin_execution(
                task.task_id,
                task.tenant_id,
                task.user_id,
                executor_kind="engine-local",
                witness=witness,
            ):
                claimed = await store.update(
                    task.task_id,
                    tenant_id=task.tenant_id,
                    user_id=task.user_id,
                    expected_status=("pending", "queued"),
                    status="running",
                    phase="starting",
                    progress="正在启动因子挖掘…",
                )
                if not claimed:
                    return
                if not provider_uri:
                    provider_uri = await self._resolve_provider_uri(task, task.data_source)
                await self._run_evolution(
                    task, loop_n=loop_n, seed=seed, provider_uri=provider_uri,
                    direction=direction, seed_factors=seed_factors,
                    llm_overrides=llm_overrides,
                )
                while task.status == TaskStatus.STOPPING and task.process is not None:
                    task._cancel_confirmed = await self._terminate_process_group(task.process.pid)
                    if task._cancel_confirmed:
                        task.status = TaskStatus.CANCELLED
                        task.phase = "cancelled"
                        self._persist_task(task)
                        await store.confirm_cancelled(task.task_id, tenant_id=task.tenant_id, user_id=task.user_id)
                        break
                    await asyncio.sleep(5)
        except ExecutionCapacityFull:
            # Another engine/Celery executor owns the single slot. The durable
            # queue stays untouched; the dispatcher will retry its oldest entry.
            return

    async def get_task_status(self, task_id: str) -> dict[str, Any] | None:
        task = self._tasks.get(task_id)
        if not task:
            return None
        return {
            "task_id": task.task_id,
            "user_id": task.user_id,
            "tenant_id": task.tenant_id,
            "status": task.status.value,
            "progress": task.progress,
            "phase": task.phase,
            "progress_pct": task.progress_pct,
            "current_loop": task.current_loop,
            "loop_n": task.loop_n,
            "market": task.market,
            "universe": task.universe,
            "data_source": task.data_source,
            "datasets": list(task.datasets),
            "mining_start": task.mining_start,
            "mining_end": task.mining_end,
            "valid_start": task.valid_start,
            "valid_end": task.valid_end,
            "error_message": task.error_message,
            "result": task.result,
            "timeline": task.timeline,
            "token_usage": task.token_usage,
        }

    async def cancel_task(self, task_id: str) -> bool:
        task = self._tasks.get(task_id)
        if not task:
            return False
        if task.status not in (TaskStatus.RUNNING, TaskStatus.PENDING, TaskStatus.QUEUED):
            return False
        task._cancel_requested = True
        if task.process and task.process.poll() is None:
            # start_new_session=True 使子进程自成进程组，本地执行直接终止整个组。
            task._cancel_confirmed = await self._terminate_process_group(task.process.pid)
            if not task._cancel_confirmed:
                logger.warning("Process group stop not confirmed for task %s", task_id)
        else:
            task._cancel_confirmed = True
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
                "tenant_id": task.tenant_id,
                "market": task.market,
                "data_source": task.data_source,
                "datasets": list(task.datasets),
                "mining_start": task.mining_start,
                "mining_end": task.mining_end,
                "valid_start": task.valid_start,
                "valid_end": task.valid_end,
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
                "mining_progress": task.mining_progress,
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
                        tenant_id=data.get("tenant_id", "default"),
                        market=data.get("market", "a_share"),
                        data_source=data.get("data_source", ""),
                        datasets=tuple(data.get("datasets") or ()),
                        mining_start=data.get("mining_start"),
                        mining_end=data.get("mining_end"),
                        valid_start=data.get("valid_start"),
                        valid_end=data.get("valid_end"),
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
                        mining_progress=data.get("mining_progress", {}),
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

    @staticmethod
    def _build_subprocess_env(
        *,
        task: EvolutionTask,
        task_log_dir: Path,
        provider_uri: str,
        llm_overrides: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """构建 RD-Agent 子进程环境变量。

        优先级（后者覆盖前者）：
          容器 env < 市场适配器 get_env_overrides() < 用户个人中心「AI 服务配置」< build_llm_env 归一

        顺序不能调换：adapter 只读 os.getenv，若在用户配置之后 update，
        会把 base/model/key 覆盖回容器占位符（曾导致挖掘 401 Invalid API Key）。
        """
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

        env: dict[str, Any] = {
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
        # 市场适配器环境变量（QLIB_PROVIDER_URI / CHAT_STREAM / CHAT_MAX_TOKENS 等）。
        # 必须在 LLM 覆盖之前应用，见上方优先级说明。
        try:
            from backend.services.engine.rd_agent.market_adapters import get_adapter
            adapter = get_adapter(task.market)
            adapter_env = adapter.get_env_overrides()
            env.update(adapter_env)
        except Exception as e:
            logger.warning("Failed to get market adapter env: %s", e)

        # 用户级 LLM 配置（个人中心「AI 服务配置」）覆盖容器全局 env
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
        # 最后执行：确保 LITELLM_* 与 OPENAI_* 的最终取值一致，不再被后续步骤改写
        build_llm_env(env)
        return env

    async def _run_evolution(
        self,
        task: EvolutionTask,
        *,
        loop_n: int,
        seed: str,
        provider_uri: str,
        direction: str = "",
        seed_factors: list[dict[str, Any]] | None = None,
        llm_overrides: dict[str, str] | None = None,
    ) -> None:
        task.status = TaskStatus.RUNNING
        task.phase = "starting"
        task.progress_pct = 2
        task.progress = "正在启动因子挖掘..."
        self._persist_task(task)

        task_log_dir = self._log_dir / task.task_id
        task_log_dir.mkdir(parents=True, exist_ok=True)

        env = self._build_subprocess_env(
            task=task,
            task_log_dir=task_log_dir,
            provider_uri=provider_uri,
            llm_overrides=llm_overrides,
        )
        # 本地执行路径：RD-Agent 子进程回调引擎沙箱控制面用的任务身份。
        env["QM_FACTOR_TASK_ID"] = task.task_id
        env["QM_FACTOR_TENANT_ID"] = task.tenant_id
        env["QM_FACTOR_USER_ID"] = task.user_id

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
                    # start_new_session=True 使子进程自成进程组，本地确认整个组已退出。
                    task._cancel_confirmed = await self._terminate_process_group(process.pid)
                    if not task._cancel_confirmed:
                        logger.warning("Process group stop not confirmed for task %s", task.task_id)
                    break
                self._update_progress(task, task_log_dir)
                await asyncio.sleep(3)

            if process.poll() is None:
                process.wait(timeout=30)

            try:
                log_fh.close()
            except Exception:
                pass

            if self._process_group_has_live_process(process.pid):
                if not await self._terminate_process_group(process.pid):
                    raise RuntimeError("RD-Agent process group exit not confirmed")

            if task._cancel_requested and task._cancel_confirmed:
                task.status = TaskStatus.CANCELLED
                task.phase = "cancelled"
                task.progress = "任务已取消"
                task.error_message = "Cancelled by user"
            elif task._cancel_requested:
                task.status = TaskStatus.STOPPING
                task.phase = "stopping"
                task.error_message = "Stop requested; waiting for child process group to exit"
            elif process.returncode == 0:
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
            if task.process is not None:
                stopped = await self._terminate_process_group(task.process.pid)
                if not stopped:
                    task._cancel_requested = True
                elif task._cancel_requested:
                    task._cancel_confirmed = True
            if task._cancel_requested and task._cancel_confirmed:
                task.status = TaskStatus.CANCELLED
                task.error_message = "Cancelled by user"
            elif task._cancel_requested:
                task.status = TaskStatus.STOPPING
                task.error_message = "Stop requested; process exit not confirmed"
            else:
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
        """取失败原因：优先 rdagent 的 common_logs.log，缺失时回退子进程 stdout。

        此前只找 common_logs.log，而实际运行目录里往往只有 subprocess_stdout.log，
        于是 error_message 只剩 "Process exited with code 1: "（冒号后为空），
        前端拿不到任何失败原因。
        """
        try:
            logs = sorted(
                log_dir.rglob("common_logs.log"), key=lambda p: p.stat().st_mtime, reverse=True
            )
        except Exception:
            logs = []
        if not logs:
            for name in ("subprocess_stdout.log", "stderr.log"):
                candidate = log_dir / name
                if candidate.is_file():
                    logs = [candidate]
                    break
        if not logs:
            return ""
        try:
            # 日志是 UTF-8；显式指定避免平台默认编码（Windows cp936）读成乱码
            with open(logs[0], encoding="utf-8", errors="replace") as f:
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
