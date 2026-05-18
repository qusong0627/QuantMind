import multiprocessing as mp
import os
import signal
import uuid
from typing import Any

from backend.services.trade.sandbox.worker import sandbox_worker_main
from backend.shared.logging_config import get_logger

logger = get_logger(__name__)


class SandboxPlatformManager:
    """
    轻量级沙箱进程池管理器。
    负责拉起、维护一定数量的 Worker 进程，提供提交运行任务、停止运行任务的接口。
    """

    _instance = None

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, pool_size: int = 10):
        if hasattr(self, "_initialized") and self._initialized:
            return

        self.pool_size = pool_size
        self._workers: dict[int, mp.Process] = {}
        self._task_queues: dict[int, mp.Queue] = {}

        # 记录每个 run_id 由哪个 worker pid 在执行
        self._active_runs: dict[str, int] = {}
        self._initialized = True

    @staticmethod
    def _strategy_key(tenant_id: str, user_id: str, strategy_id: str) -> str:
        return f"{tenant_id}_{user_id}_{strategy_id}"

    def _spawn_worker(self) -> int:
        q = mp.Queue()
        p = mp.Process(target=sandbox_worker_main, args=(q,), daemon=True)
        p.start()
        self._workers[p.pid] = p
        self._task_queues[p.pid] = q
        return p.pid

    def _purge_dead_workers(self) -> None:
        dead_pids = [pid for pid, proc in self._workers.items() if not proc.is_alive()]
        if not dead_pids:
            return

        for pid in dead_pids:
            self._workers.pop(pid, None)
            queue = self._task_queues.pop(pid, None)
            if queue is not None:
                try:
                    queue.close()
                except Exception:
                    pass

        stale_keys = [key for key, pid in self._active_runs.items() if pid in dead_pids]
        for key in stale_keys:
            self._active_runs.pop(key, None)

        logger.warning(
            "Sandbox workers exited unexpectedly: dead_pids=%s stale_strategies=%s",
            dead_pids,
            stale_keys,
        )

    def _ensure_pool_capacity(self) -> None:
        self._purge_dead_workers()
        missing = max(0, self.pool_size - len(self._workers))
        for _ in range(missing):
            self._spawn_worker()

    def _select_idle_worker_pid(self) -> int | None:
        busy_pids = set(self._active_runs.values())
        idle_pids = [
            pid
            for pid, proc in self._workers.items()
            if proc.is_alive() and pid not in busy_pids
        ]
        if not idle_pids:
            return None
        return sorted(idle_pids)[0]

    def start_pool(self):
        """拉起进程池"""
        logger.info(f"Starting Sandbox Worker Pool with {self.pool_size} workers...")
        self._ensure_pool_capacity()
        logger.info(f"Sandbox Worker Pool started. PIDs: {list(self._workers.keys())}")

    def stop_pool(self):
        """关闭所有 worker"""
        logger.info("Stopping Sandbox Worker Pool...")
        for pid, q in self._task_queues.items():
            q.put(None)  # 发送毒药

        for pid, p in self._workers.items():
            p.join(timeout=3)
            if p.is_alive():
                p.terminate()
            q = self._task_queues.get(pid)
            if q is not None:
                try:
                    q.close()
                except Exception:
                    pass

        self._workers.clear()
        self._task_queues.clear()
        self._active_runs.clear()
        logger.info("Sandbox Worker Pool stopped.")

    def submit_strategy(
        self,
        tenant_id: str,
        user_id: str,
        strategy_id: str,
        code_str: str,
        exec_config: dict,
        live_trade_config: dict | None = None,
    ) -> str:
        """分发策略到其中一个空闲的 Worker，返回 run_id"""
        self._ensure_pool_capacity()
        if not self._workers:
            raise RuntimeError("Sandbox Worker Pool is empty.")

        key = self._strategy_key(tenant_id, user_id, strategy_id)
        if self.is_strategy_running(tenant_id, user_id, strategy_id):
            logger.warning(
                "Simulation strategy is already running, restarting it: tenant=%s user=%s strategy=%s",
                tenant_id,
                user_id,
                strategy_id,
            )
            self.stop_strategy(tenant_id, user_id, strategy_id)

        assigned_pid = self._select_idle_worker_pid()
        if assigned_pid is None:
            raise RuntimeError("No idle sandbox worker available.")

        run_id = str(uuid.uuid4())
        task = {
            "tenant_id": tenant_id,
            "user_id": user_id,
            "strategy_id": strategy_id,
            "run_id": run_id,
            "code_str": code_str,
            "exec_config": exec_config,
            "live_trade_config": live_trade_config or {},
        }

        self._task_queues[assigned_pid].put(task)
        self._active_runs[key] = assigned_pid
        logger.info(
            f"Submitted simulation strategy {strategy_id} for user {user_id} to Sandbox Process PID {assigned_pid}"
        )
        return run_id

    def stop_strategy(self, tenant_id: str, user_id: str, strategy_id: str) -> bool:
        """请求停止策略：由于当前一个进程在阻塞循环，停止的最暴力/安全方式是杀掉那个进程然后起一个替补"""
        self._purge_dead_workers()
        key = self._strategy_key(tenant_id, user_id, strategy_id)
        if key not in self._active_runs:
            logger.warning(f"Strategy {strategy_id} is not tracked as running in sandbox.")
            return False

        pid = self._active_runs[key]
        process = self._workers.get(pid)
        if process is None or not process.is_alive():
            self._active_runs.pop(key, None)
            logger.warning(
                "Sandbox strategy mapping found but process already exited: strategy=%s pid=%s",
                strategy_id,
                pid,
            )
            return False

        shared_keys = [k for k, mapped_pid in self._active_runs.items() if mapped_pid == pid]
        if len(shared_keys) > 1:
            # 历史版本可能把多个策略映射到同一 PID；避免一次 stop 误杀其他用户策略。
            self._active_runs.pop(key, None)
            logger.error(
                "Refusing to kill shared sandbox worker pid=%s for strategy=%s; shared_keys=%s",
                pid,
                strategy_id,
                shared_keys,
            )
            return False

        os.kill(pid, signal.SIGTERM)
        process.join(timeout=2)
        if process.is_alive():
            os.kill(pid, signal.SIGKILL)

        self._workers.pop(pid, None)
        queue = self._task_queues.pop(pid, None)
        if queue is not None:
            try:
                queue.close()
            except Exception:
                pass

        for stale_key in [k for k, mapped_pid in self._active_runs.items() if mapped_pid == pid]:
            self._active_runs.pop(stale_key, None)

        new_pid = self._spawn_worker()
        logger.info(
            "Sandbox Worker %s killed for stopping strategy %s. Respawned worker pid=%s.",
            pid,
            strategy_id,
            new_pid,
        )
        return True

    def is_strategy_running(self, tenant_id: str, user_id: str, strategy_id: str) -> bool:
        self._purge_dead_workers()
        key = self._strategy_key(tenant_id, user_id, strategy_id)
        pid = self._active_runs.get(key)
        if pid is None:
            return False
        process: Any = self._workers.get(pid)
        alive = bool(process and process.is_alive())
        if not alive:
            self._active_runs.pop(key, None)
        return alive


sandbox_manager = SandboxPlatformManager()
