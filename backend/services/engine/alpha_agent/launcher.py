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
import shutil
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from backend.shared.utc_datetime import UTC, to_utc_iso

logger = logging.getLogger(__name__)


def _created_at_iso(ts: Any) -> str | None:
    """任务创建时刻 → ISO-8601 UTC（带 Z）；取不到就是 None。

    `created_at` 在 `EvolutionTask` 里是 `time.time()` 的 epoch 秒。前端那个全局进度
    面板靠它回答「这个任务是刚刚起的、还是我去吃饭前就挂着的那一个」，所以**绝不能**
    在缺失时回落到 `now()`——那会把一个几小时前的僵尸任务显示成刚提交的。
    """
    try:
        return to_utc_iso(datetime.fromtimestamp(float(ts), UTC))
    except (TypeError, ValueError, OverflowError, OSError):
        return None


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    QUEUED = "queued"
    COMPLETED = "completed"
    FAILED = "failed"


class QueueFullError(RuntimeError):
    """排队深度已达上限：批量派发路径据此逐卡片回可操作错误（不落任务行）。"""


#: 并发/排队上限的环境变量名与默认值（**唯一读取点**：路由 429 判定与排队
#: 判断共用 :meth:`AlphaAgentLauncher.running_capacity` / ``queue_capacity``）。
ENV_MAX_RUNNING_PER_USER = "ALPHA_AGENT_MAX_RUNNING_PER_USER"
ENV_MAX_RUNNING_GLOBAL = "ALPHA_AGENT_MAX_RUNNING_GLOBAL"
ENV_MAX_QUEUED_PER_USER = "ALPHA_AGENT_MAX_QUEUED_PER_USER"
ENV_MAX_QUEUED_GLOBAL = "ALPHA_AGENT_MAX_QUEUED_GLOBAL"
DEFAULT_MAX_RUNNING_PER_USER = 2
DEFAULT_MAX_RUNNING_GLOBAL = 4
DEFAULT_MAX_QUEUED_PER_USER = 20
DEFAULT_MAX_QUEUED_GLOBAL = 50


#: 排空时的 LLM 配置重解析器（注册制，engine 启动期由路由注册）。
#: 排队行**绝不持久化任何密钥**——排空时按 (user_id, tenant_id) 重新解析。
#: 未注册（主机直跑/单测）→ None 覆盖，容器 env 兜底照旧；注册后返回 None
#: （或抛异常）→ 任务显式失败并给可操作报错，绝不静默换供应商。
_llm_override_resolver: (
    Callable[[str, str], Awaitable[dict[str, str] | None]] | None
) = None


def set_llm_override_resolver(
    fn: Callable[[str, str], Awaitable[dict[str, str] | None]] | None,
) -> None:
    global _llm_override_resolver
    _llm_override_resolver = fn


#: 排空时既没解析出用户配置、容器 env 也没有 Key：与 evolve 端点 412 同一口径。
_QUEUE_LLM_MISSING_MESSAGE = (
    "排队任务启动前重解析 LLM 配置失败：未配置 LLM API Key"
    "（个人中心「其他设置 → AI 服务配置」，或在服务器 .env 配置 "
    "DEEPSEEK_API_KEY / AI_IDE_LLM_API_KEY / OPENAI_API_KEY）。"
    "排队不持久化密钥，无法沿用提交时的配置。"
)


# 进度落库节流：内存心跳每 3s，DB 写 ≥15s 一次（轮询写库会把库压成热点）。
_DB_SYNC_INTERVAL_S = 15.0


def _task_store():
    """任务记录层 store（模块级函数便于测试替换；失败语义见 _db_* 系列）。"""
    from backend.services.engine.alpha_agent.task_store import get_mining_task_store

    return get_mining_task_store()


#: 任务日志根：每任务一个子目录（subprocess_stdout.log + task_state.json +
#: RD-Agent 工作区）。默认 /data——容器重建不丢（T-FM-20）；历史本体另在
#: PG `rd_agent_mining_tasks`，日志只是排障补充。主机直跑等 /data 不可写的
#: 场景用 LOG_TRACE_PATH 显式指到可写目录（旧默认即 /tmp/alpha_agent_logs）。
DEFAULT_LOG_DIR = "/data/alpha_agent_logs"

#: 终态任务日志目录的留存线（天）：engine 启动时由 :func:`gc_task_logs` 执行。
ENV_LOG_RETENTION_DAYS = "LOG_TRACE_RETENTION_DAYS"
DEFAULT_LOG_RETENTION_DAYS = 90

#: 可被 GC 的 task_state.json status 原文；认不出的状态与非终态一律不动。
_GC_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})


def _resolve_log_dir() -> Path:
    return Path(os.getenv("LOG_TRACE_PATH", DEFAULT_LOG_DIR))


def _env_int(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("%s=%r 不是整数，按默认 %d 处理", name, raw, default)
        return default


def gc_task_logs(
    log_dir: Path | None = None, *, retention_days: int | None = None
) -> dict[str, int]:
    """任务日志留存 GC：清理终态且超龄的任务目录，返回 ``{"scanned", "pruned"}``。

    只认「``<log_dir>/<task_id>/task_state.json`` 可解析 + status 是终态 +
    状态文件 mtime 早于留存线」的目录；任何一条不满足就原样保留——认不出的
    东西不动（框架自身往根下写的 ``__session__`` 之类天然免疫）。历史本体在
    PG ``rd_agent_mining_tasks``，清掉的是排障日志，清后「查看日志」读不到
    属预期留存语义。``retention_days`` 缺省读 env，0 = 下次启动清空终态。
    """
    root = Path(log_dir) if log_dir is not None else _resolve_log_dir()
    if retention_days is None:
        retention_days = _env_int(ENV_LOG_RETENTION_DAYS, DEFAULT_LOG_RETENTION_DAYS)
    cutoff = time.time() - max(0, int(retention_days)) * 86400
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return {"scanned": 0, "pruned": 0}
    scanned = 0
    pruned = 0
    for entry in entries:
        if not entry.is_dir():
            continue
        scanned += 1
        state_file = entry / "task_state.json"
        try:
            with open(state_file) as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        if str(data.get("status", "")) not in _GC_TERMINAL_STATUSES:
            continue
        try:
            if state_file.stat().st_mtime > cutoff:
                continue
        except OSError:
            continue
        shutil.rmtree(entry, ignore_errors=True)
        pruned += 1
    return {"scanned": scanned, "pruned": pruned}


@dataclass(frozen=True)
class QueueReceipt:
    """一次提交的落点：已启动 or 已排队（带本用户队列内的位次）。"""

    task_id: str
    status: str  # "running"（已派发子进程）| "queued"
    queue_position: int | None = None


@dataclass
class EvolutionTask:
    task_id: str
    user_id: str
    tenant_id: str = "default"
    market: str = "a_share"
    data_source: str = ""
    universe: str = "csi300"
    direction: str = ""
    #: 方向模式（T-MV-02）：'selected'/'random'=类别选择路径；''=模式未参与
    #: （自由文本/卡片派发）→ 落库为 NULL，历史页如实呈现
    direction_mode: str = ""
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
    # 上次进度落库时刻（epoch 秒）。3s 内存心跳不变，DB 写节流见 _DB_SYNC_INTERVAL_S。
    _last_db_sync: float = 0.0


class AlphaAgentLauncher:
    """Launches factor evolution tasks via the RD-Agent runner."""

    _RD_AGENT_RUNNER_SCRIPT = str(
        Path(__file__).resolve().parent.parent.parent.parent.parent / "scripts" / "alpha_agent" / "run_rd_agent.py"
    )

    def __init__(self) -> None:
        self._tasks: dict[str, EvolutionTask] = {}
        self._log_dir = _resolve_log_dir()
        self._log_dir.mkdir(parents=True, exist_ok=True)
        # 排空重入闸：并发 drain（收尾触发 vs 清扫器）只允许一个在跑
        self._draining = False
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
        direction_mode: str | None = None,
        data_source: str | None = None,
        source: str = "text",
        doc_id: str | None = None,
        llm_overrides: dict[str, str] | None = None,
        tenant_id: str = "default",
    ) -> str:
        """Start a factor evolution task. Returns task_id.

        source/doc_id: 输入来源（text=文字指令，doc=文档解析链），落任务记录行。
        direction_mode: 方向如何被选中（'selected'/'random'）；None=模式未参与，
        落库为 NULL——调用方只在类别选择真正发生时传值（T-MV-02）。
        llm_overrides: 用户级 LLM 环境变量覆盖（如个人中心配置的 API Key），
        优先于容器全局 env 注入子进程。

        **永不排队**：并发上限的 429 背压契约在路由层（前端「原文上屏」）；
        需要「满了自动排队」的批量派发路径走 :meth:`start_or_queue`。
        """
        from backend.services.engine.alpha_agent.hw_lock import assert_factor_mining_hardware

        assert_factor_mining_hardware()

        task_id = uuid.uuid4().hex[:16]
        task = EvolutionTask(
            task_id=task_id, user_id=user_id, tenant_id=tenant_id or "default",
            market=market, universe=universe, loop_n=loop_n,
            data_source=data_source or "", direction=direction or "",
            direction_mode=direction_mode or "",
        )
        self._tasks[task_id] = task

        # 记录层落行（失败只告警）：历史页第一秒就要能看见这个任务
        await self._db_create(task, source=source, doc_id=doc_id)

        self._launch(
            task,
            loop_n=loop_n,
            seed=seed,
            provider_uri=provider_uri,
            direction=direction or "",
            llm_overrides=llm_overrides,
        )
        return task_id

    async def start_or_queue(
        self,
        user_id: str,
        *,
        market: str = "a_share",
        universe: str = "csi300",
        loop_n: int = 5,
        seed: str | None = None,
        direction: str | None = None,
        direction_mode: str | None = None,
        data_source: str | None = None,
        source: str = "text",
        doc_id: str | None = None,
        llm_overrides: dict[str, str] | None = None,
        tenant_id: str = "default",
    ) -> QueueReceipt:
        """批量派发入口：有名额立即启动，满则有序排队（最旧优先）。

        与 :meth:`start_evolution`（evolve 端点 429 背压）不同，这里**不拒
        正常提交**——只有排队深度上限（``ALPHA_AGENT_MAX_QUEUED_*``）才抛
        :class:`QueueFullError`，供批量端点逐卡片回可操作错误。

        并发安全的关键：名额判定与占位（PENDING 入内存表）是同一次同步执行，
        中间没有任何 await——并发提交看到的是同一份计数，不会双开超额。
        """
        from backend.services.engine.alpha_agent.hw_lock import assert_factor_mining_hardware

        assert_factor_mining_hardware()

        counts = self.count_running()
        max_per_user, max_global = self.running_capacity()
        has_slot = (
            counts["by_user"].get(user_id, 0) < max_per_user
            and counts["global"] < max_global
        )
        if not has_slot:
            self._assert_queue_room(user_id)

        task_id = uuid.uuid4().hex[:16]
        task = EvolutionTask(
            task_id=task_id, user_id=user_id, tenant_id=tenant_id or "default",
            market=market, universe=universe, loop_n=loop_n,
            data_source=data_source or "", direction=direction or "",
            direction_mode=direction_mode or "",
            status=TaskStatus.PENDING if has_slot else TaskStatus.QUEUED,
        )
        self._tasks[task_id] = task
        await self._db_create(task, source=source, doc_id=doc_id)

        if not has_slot:
            self._persist_task(task)
            return QueueReceipt(task_id, "queued", self._queue_position(task))

        self._launch(
            task,
            loop_n=loop_n,
            seed=seed,
            direction=direction or "",
            llm_overrides=llm_overrides,
        )
        return QueueReceipt(task_id, "running", None)

    async def drain_queue(self) -> int:
        """把排队任务按「最旧优先」补进空闲名额，返回本次启动数。

        触发点：引擎启动 + 每个任务收尾（成功/失败/取消）+ 60s 清扫器。槽位在
        第一个 await **之前**同步预留（QUEUED→PENDING），所以并发 drain
        （``_draining`` 重入闸）与窗口期的新提交（count_running）看到同一份占位。

        **绝不向外抛异常**：单个任务失败（解析不到 LLM 配置 / 启动炸）都落它
        自己的任务行后继续下一个——排水不能拖垮触发它的收尾路径。排空按注册的
        解析器重解析 LLM 配置（队列不持久化密钥）；解析器未注册（主机直跑/
        单测）时 overrides=None，容器 env 兜底。
        """
        if self._draining:
            return 0
        self._draining = True
        started = 0
        try:
            while True:
                counts = self.count_running()
                max_per_user, max_global = self.running_capacity()
                if counts["global"] >= max_global:
                    break
                candidate = next(
                    (
                        t
                        for t in sorted(
                            (
                                t
                                for t in self._tasks.values()
                                if t.status == TaskStatus.QUEUED
                            ),
                            key=lambda t: t.created_at,
                        )
                        if counts["by_user"].get(t.user_id, 0) < max_per_user
                    ),
                    None,
                )
                if candidate is None:
                    break

                # 同步预留：任何 await 之前先把名额占住
                candidate.status = TaskStatus.PENDING

                overrides: dict[str, str] | None = None
                if _llm_override_resolver is not None:
                    overrides = await self._resolve_drain_overrides(candidate)
                    if overrides is None:
                        candidate.status = TaskStatus.FAILED
                        candidate.error_message = _QUEUE_LLM_MISSING_MESSAGE
                        self._persist_task(candidate)
                        await self._db_finish(candidate)
                        continue

                try:
                    self._launch(
                        candidate,
                        loop_n=candidate.loop_n,
                        seed=None,
                        direction=candidate.direction,
                        llm_overrides=overrides,
                    )
                except Exception as e:
                    candidate.status = TaskStatus.FAILED
                    candidate.error_message = f"排队任务启动失败: {e}"
                    logger.exception("queued task %s launch failed", candidate.task_id)
                    self._persist_task(candidate)
                    await self._db_finish(candidate)
                    continue
                started += 1
        finally:
            self._draining = False
        return started

    async def _resolve_drain_overrides(
        self, task: EvolutionTask
    ) -> dict[str, str] | None:
        """排空时的 LLM 覆盖重解析；None = 解析失败（调用方落任务失败）。"""
        assert _llm_override_resolver is not None
        try:
            return await _llm_override_resolver(task.user_id, task.tenant_id)
        except Exception as e:
            logger.warning(
                "queue drain llm re-resolve failed for %s: %s", task.task_id, e
            )
            return None

    async def _drain_after_finish(self) -> None:
        """收尾/取消后的补位排水；排水自身吞异常——绝不改写调用方任务终态。"""
        try:
            await self.drain_queue()
        except Exception as e:
            logger.warning("mining queue drain failed: %s", e)

    def _assert_queue_room(self, user_id: str) -> None:
        q_per_user, q_global = self.queue_capacity()
        queued = [t for t in self._tasks.values() if t.status == TaskStatus.QUEUED]
        user_queued = sum(1 for t in queued if t.user_id == user_id)
        if user_queued >= q_per_user or len(queued) >= q_global:
            raise QueueFullError(
                f"排队已满（您已排队 {user_queued}/{q_per_user}，"
                f"全平台排队 {len(queued)}/{q_global}），"
                "请等待当前任务完成或先取消已排队的任务。"
            )

    def _queue_position(self, task: EvolutionTask) -> int | None:
        """本用户队列内位次（1-based，最旧=1）；不在队列里返回 None。"""
        queued = sorted(
            (
                t
                for t in self._tasks.values()
                if t.status == TaskStatus.QUEUED and t.user_id == task.user_id
            ),
            key=lambda t: t.created_at,
        )
        for idx, t in enumerate(queued, start=1):
            if t.task_id == task.task_id:
                return idx
        return None

    def running_capacity(self) -> tuple[int, int]:
        """(每用户上限, 全平台上限)。路由 429 判定与排队判断共用同一读取点。"""
        return (
            _env_int(ENV_MAX_RUNNING_PER_USER, DEFAULT_MAX_RUNNING_PER_USER),
            _env_int(ENV_MAX_RUNNING_GLOBAL, DEFAULT_MAX_RUNNING_GLOBAL),
        )

    def queue_capacity(self) -> tuple[int, int]:
        """(每用户排队深度, 全平台排队深度)。0 = 不许排队（满了直接拒）。"""
        return (
            _env_int(ENV_MAX_QUEUED_PER_USER, DEFAULT_MAX_QUEUED_PER_USER),
            _env_int(ENV_MAX_QUEUED_GLOBAL, DEFAULT_MAX_QUEUED_GLOBAL),
        )

    def _launch(
        self,
        task: EvolutionTask,
        *,
        loop_n: int,
        seed: str | None = None,
        provider_uri: str | None = None,
        direction: str = "",
        llm_overrides: dict[str, str] | None = None,
    ) -> None:
        """解析 provider_uri 并调度 :meth:`_run_evolution`（不等待子进程）。"""
        # Determine provider URI from market adapter if not specified
        if not provider_uri:
            try:
                from backend.services.engine.rd_agent.market_adapters import get_adapter
                adapter = get_adapter(task.market)
                provider_uri = adapter.get_qlib_provider_uri()
            except Exception:
                provider_uri = os.getenv("QLIB_PROVIDER_URI", "/data/qlib/cn_data")

        # Override provider URI based on data_source
        if task.data_source:
            ds = task.data_source.lower().strip()
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
                direction=direction,
                llm_overrides=llm_overrides,
            )
        )

    async def get_task_status(self, task_id: str) -> dict[str, Any] | None:
        task = self._tasks.get(task_id)
        if not task:
            return None
        return {
            "task_id": task.task_id,
            "user_id": task.user_id,
            # 前端全局进度面板据此排序并显示「开始于」；此前这个字段只在磁盘
            # task_state.json 里有，状态接口不返回，前端只能拿不到就填 now()。
            "created_at": _created_at_iso(task.created_at),
            "status": task.status.value,
            "progress": task.progress,
            "phase": task.phase,
            "progress_pct": task.progress_pct,
            "current_loop": task.current_loop,
            "loop_n": task.loop_n,
            "market": task.market,
            "universe": task.universe,
            "data_source": task.data_source,
            "direction": task.direction,
            "error_message": task.error_message,
            "result": task.result,
            "timeline": task.timeline,
            "token_usage": task.token_usage,
            # 排队位次（本用户队列内，1-based）；非排队状态恒为 None。
            # 前端「排队中（第 N 位）」直接渲染，不用自己数队列。
            "queue_position": (
                self._queue_position(task)
                if task.status == TaskStatus.QUEUED
                else None
            ),
        }

    async def cancel_task(self, task_id: str) -> bool:
        task = self._tasks.get(task_id)
        if not task:
            return False
        if task.status not in (TaskStatus.RUNNING, TaskStatus.PENDING, TaskStatus.QUEUED):
            return False
        task._cancel_requested = True
        # 排队任务还没有进程，杀掉这一步天然跳过
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
        await self._db_cancel(task)
        # 取消运行中的任务腾出名额：排队任务立即补位（排水自身吞异常）
        await self._drain_after_finish()
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

    # ------------------------------------------------------------------
    # 任务记录层（rd_agent_mining_tasks）——任何失败只告警，绝不拦挖掘主链
    # ------------------------------------------------------------------

    async def _db_create(
        self, task: EvolutionTask, *, source: str = "text", doc_id: str | None = None
    ) -> None:
        try:
            await _task_store().create_task(
                task_id=task.task_id,
                user_id=task.user_id,
                # 建行状态=建任务时点的状态（pending 立即跑 / queued 已排队）；
                # store 侧只收这两种初始态，running 只能由心跳/对账到来
                status=task.status.value,
                market=task.market,
                universe=task.universe,
                data_source=task.data_source,
                direction=task.direction,
                # 模式未参与（''）→ NULL：历史页不把自由文本伪记成「类别选定」
                direction_mode=task.direction_mode or None,
                loop_n=task.loop_n,
                source=source,
                doc_id=doc_id,
            )
        except Exception as e:
            logger.warning("mining task row create failed for %s: %s", task.task_id, e)

    async def _db_sync_progress(self, task: EvolutionTask) -> None:
        try:
            await _task_store().update_progress(
                task.task_id,
                status=task.status.value,
                progress_pct=task.progress_pct,
                current_loop=task.current_loop,
            )
        except Exception as e:
            logger.warning(
                "mining task progress sync failed for %s: %s", task.task_id, e
            )

    async def _db_sync_progress_throttled(self, task: EvolutionTask) -> None:
        """内存心跳每 3s 照旧；落到 DB 的进度 ≥_DB_SYNC_INTERVAL_S 一次。"""
        if time.time() - task._last_db_sync < _DB_SYNC_INTERVAL_S:
            return
        task._last_db_sync = time.time()
        await self._db_sync_progress(task)

    async def _db_finish(self, task: EvolutionTask) -> None:
        """终态收尾。取消路径已由 :meth:`_db_cancel` 落 ``cancelled``，此处必须让路。"""
        if task._cancel_requested:
            return
        try:
            store = _task_store()
            factor_count = await store.count_factors(task.task_id)
            await store.mark_terminal(
                task.task_id,
                status=task.status.value,
                error=task.error_message,
                factor_count=factor_count,
            )
        except Exception as e:
            logger.warning(
                "mining task terminal write failed for %s: %s", task.task_id, e
            )

    async def _db_cancel(self, task: EvolutionTask) -> None:
        try:
            await _task_store().mark_terminal(
                task.task_id, status="cancelled", error="Cancelled by user"
            )
        except Exception as e:
            logger.warning(
                "mining task cancel write failed for %s: %s", task.task_id, e
            )

    def _persist_task(self, task: EvolutionTask) -> None:
        """Save task state to disk so it survives restarts.

        纯尽力而为：排队/取消等提交路径都直接调它，任何失败（含路径计算）
        只告警绝不外抛——持久化失败不能把提交本身打挂。
        """
        try:
            state_file = self._log_dir / task.task_id / "task_state.json"
            state_file.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "task_id": task.task_id,
                "user_id": task.user_id,
                # 租户进盘：重启后幸存的任务在排空重解析 LLM 配置时要按
                # (user_id, tenant_id) 打 profile 网关（密钥本体绝不落盘）
                "tenant_id": task.tenant_id,
                "market": task.market,
                "data_source": task.data_source,
                "universe": task.universe,
                "direction": task.direction,
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
                        tenant_id=data.get("tenant_id", "default") or "default",
                        market=data.get("market", "a_share"),
                        data_source=data.get("data_source", ""),
                        universe=data.get("universe", "csi300"),
                        direction=data.get("direction", ""),
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
                    # Running tasks at startup are likely orphaned；
                    # queued 不是孤儿（还没有子进程）——原样保留，启动排水接续
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
        task._last_db_sync = time.time()
        await self._db_sync_progress(task)

        task_log_dir = self._log_dir / task.task_id
        task_log_dir.mkdir(parents=True, exist_ok=True)

        # 因子池检索注入（单通道：只写提示词文件，零运行时副作用——绝不碰
        # base_factors.json，否则 LLM 会把历史摘要当可用基础特征送进因子代码
        # 运行时，理由见 pool_service 模块 docstring）。任何失败都空注入，
        # 绝不拦住一次挖矿。
        from backend.services.engine.mining_plugins import pool_service

        injection = await pool_service.prepare_injection(
            user_id=task.user_id,
            market=task.market,
            universe=task.universe,
            task_id=task.task_id,
            log_dir=task_log_dir,
        )

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

        # CoSTEER 经验记忆（知识库）：跨任务累积「因子代码怎么写才过评测」的经验。
        # 读写路径、绝对路径约束与 filelock 的理由见 rd_agent/kb_env.py。
        from backend.services.engine.rd_agent.kb_env import knowledge_base_env

        env = {
            **os.environ,
            "PYTHONPATH": os.getenv("PYTHONPATH") or "/app",
            "LOG_TRACE_PATH": str(task_log_dir),
            "QLIB_PROVIDER_URI": provider_uri,
            "QLIB_FACTOR_UNIVERSE": task.universe,
            "REASONING_MODEL": chat_model,
            "CHAT_STREAM": "false",
            **knowledge_base_env(),
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
        if injection.path is not None:
            # rd_loop_wrapper._build_prompt_suffix 读这个文件追加「历史挖掘记忆」段
            env["QMF_POOL_CONTEXT_PATH"] = str(injection.path)

        # 补齐 RD-Agent litellm 后端需要的 LITELLM_ 前缀变量（deepseek 优先）
        if llm_overrides:
            # 显式写入 LITELLM_ 覆盖，绕开 build_llm_env 里 os.getenv 的 mock 占位符链
            for _k in ("LITELLM_OPENAI_API_KEY", "LITELLM_OPENAI_API_BASE"):
                if llm_overrides.get(_k):
                    env[_k] = llm_overrides[_k]

        from backend.services.engine.rd_agent.llm_env import (
            build_llm_env,
            embedding_overrides,
        )

        # 用户级向量检索配置（个人中心「向量检索」）——与 chat 是**独立通道**，
        # 必须单独透传：漏掉这一步，界面显示「已保存」而挖掘始终用容器级 .env。
        # 键清单与理由见 llm_env.embedding_overrides。
        env.update(embedding_overrides(llm_overrides))

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

            # spawn 成功才记检索疲劳（提示词已随子进程启动送达）；
            # mark_retrieved 自身吞异常，失败最多少计一次，绝不影响挖掘
            if injection.factor_ids:
                await pool_service.mark_retrieved(injection.factor_ids)

            while process.poll() is None:
                if task._cancel_requested:
                    try:
                        pgid = os.getpgid(process.pid)
                        os.killpg(pgid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    break
                self._update_progress(task, task_log_dir)
                await self._db_sync_progress_throttled(task)
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
            await self._db_finish(task)

        except Exception as e:
            task.status = TaskStatus.FAILED
            task.error_message = str(e)
            logger.exception("Factor mining exception for task %s", task.task_id)
            self._persist_task(task)
            await self._db_finish(task)
        finally:
            # 名额释放后立即补位排队任务（成功/失败/取消任何收尾路径都算）。
            # 排水自身吞异常——它绝不能改写本任务的终态。
            await self._drain_after_finish()

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
