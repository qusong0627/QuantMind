"""QuantMind 免 Docker 本地直跑训练编排器
=========================================
便携一键启动包（无 Docker）的本地训练执行器：复用 LocalDockerOrchestrator 的
config 构建 / 日志进度识别 / 回调闭环 / 多周期编排骨架，仅把「docker run 训练
容器 + 轮询容器状态」替换为「同运行时 python 子进程跑 train.py + 轮询退出」。

与 Docker 编排的差异：
- 数据目录全部为本机真实路径（无容器挂载路径换算）；
- 回调地址回落本机 API（http://127.0.0.1:{API_PORT}，QUANTMIND_API_BASE_URL 可覆盖）；
- 无暂停/恢复其它容器的资源保护（本机无容器概念），不做 bootstrap pip（运行时完整）；
- GPU 由 nvidia-smi / torch 直接探测，train.py 内部会自行回落 CPU。
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from collections import deque
from pathlib import Path

import yaml

from backend.services.engine.data_platform.quantdb_factor_reader import normalize_market
from backend.services.engine.training.local_docker_orchestrator import (
    LocalDockerOrchestrator,
    _CALLBACK_CHECK_INTERVAL,
    _CALLBACK_TIMEOUT,
)
from backend.services.engine.training.orchestrator_base import REGISTRY
from backend.services.engine.training.training_log_stream import TrainingRunLogStream
from backend.shared.training_runtime import (
    default_api_base_url,
    find_training_script,
    local_market_data_root,
    local_snapshot_root,
    repo_root_dir,
    training_jobs_dir,
)

logger = logging.getLogger(__name__)


class LocalProcessOrchestrator(LocalDockerOrchestrator):
    """同运行时 python 直跑训练脚本的本地编排器（免 Docker）。"""

    def __init__(self) -> None:
        # 注意：不能调 super().__init__() —— 父类构造里 DockerClient.from_env()
        # 会急切连接 daemon 抓取 server version，免 Docker 环境直接抛错。
        # 直跑模式不需要 docker client，仅复用父类的编排常量与编排骨架。
        self.api_base = default_api_base_url()
        self.internal_secret = (os.getenv("INTERNAL_CALL_SECRET") or "").strip()
        # P0-3: 与父类一致的 fail-closed 语义，缺失时明确报错
        if not self.internal_secret:
            raise RuntimeError(
                "INTERNAL_CALL_SECRET not set; cannot start training orchestrator. "
                "Set it in .env or QUANTMIND_ENV=development for auto-generation."
            )
        self.log_stream = TrainingRunLogStream()

        script = find_training_script()
        if script is None:
            raise RuntimeError(
                "未找到本地训练脚本 train.py（预期在包根目录或 docker/training/ 下），"
                "无法以本机直跑方式执行训练"
            )
        self.train_script = script
        self.work_root = training_jobs_dir()

    # ── config.yaml：把容器挂载路径换算为本机真实路径 ─────────────────────────
    def _build_config_yaml(self, run_id: str, payload: dict) -> dict:
        cfg = super()._build_config_yaml(run_id, payload)

        context = (
            payload.get("context") if isinstance(payload.get("context"), dict) else {}
        )
        market = normalize_market(context.get("market") or "CN")
        factor_source = str(payload.get("factor_source") or "").strip()
        data_root = local_market_data_root(market)

        data = cfg.setdefault("data", {})
        if factor_source:
            # 因子直读模式：QuantDB 目录与 local_dir 都指向本机真实数据根
            data["local_dir"] = str(data_root)
            data["quantdb_dir"] = str(data_root)
        else:
            # 旧式快照模式：命中真实快照目录；缺失时指向数据根，读取期报清晰错误
            snap = local_snapshot_root()
            data["local_dir"] = str(snap if snap is not None else data_root)

        work_dir = training_jobs_dir(run_id)
        cfg.setdefault("output", {})["result_path"] = str(work_dir / "result.json")
        cfg.setdefault("cache", {})["dir"] = str(work_dir / "cache")
        cfg.setdefault("callback", {})["url"] = (
            f"{self.api_base}/api/v1/models/training-runs/{run_id}/complete"
        )
        return cfg

    # ── GPU 探测：仅本机视角，不做 docker 探针容器 ─────────────────────────────
    async def _detect_process_gpu(self) -> bool:
        try:
            proc = await asyncio.create_subprocess_exec(
                "nvidia-smi",
                "-L",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            if await asyncio.wait_for(proc.wait(), timeout=5) == 0:
                return True
        except Exception:  # noqa: BLE001
            pass
        try:
            import torch

            return bool(torch.cuda.is_available())
        except Exception:  # noqa: BLE001
            return False

    # ── 启动训练任务（本机子进程版）────────────────────────────────────────────
    async def launch_training_job(self, run_id: str, payload: dict = None) -> None:
        from backend.services.api.routers.admin.db import TrainingJobRecord
        from backend.shared.database_manager_v2 import get_session

        if payload is None:
            logger.error("[%s] Orchestrator received None payload!", run_id)
            payload = {}

        config = self._build_config_yaml(run_id, payload)
        work_dir = training_jobs_dir(run_id)
        work_dir.mkdir(parents=True, exist_ok=True)
        (work_dir / "cache").mkdir(exist_ok=True)

        async with get_session() as db:
            record = await db.get(TrainingJobRecord, run_id)
            if record:
                record.status = "provisioning"
                record.progress = max(int(record.progress or 0), 5)
                record.logs = (
                    record.logs or ""
                ) + f"Starting local process: {self.train_script}\n"
                user_id = str(record.user_id or "unknown")
                tenant_id = str(record.tenant_id or "default")

                notices = payload.get("system_notices") or []
                for msg in notices:
                    record.logs += f"[NOTICE] {msg}\n"

                await db.commit()
                self.log_stream.append_log(
                    run_id=run_id,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    line=f"[SYSTEM] Starting local train script: {self.train_script}",
                    status="provisioning",
                    progress=5,
                )
                for msg in notices:
                    self.log_stream.append_log(
                        run_id=run_id,
                        tenant_id=tenant_id,
                        user_id=user_id,
                        line=f"[NOTICE] {msg}",
                        status="provisioning",
                        progress=5,
                    )
            else:
                logger.warning(
                    "[%s] Training record not found in launch_training_job", run_id
                )
                user_id = "unknown"
                tenant_id = "default"

        # 写入 config.yaml（与 Docker 编排同构，产物目录为本机工作目录）
        config_path = work_dir / "config.yaml"
        try:
            with open(config_path, "w", encoding="utf-8") as f:
                yaml.dump(config, f, allow_unicode=True, default_flow_style=False)
                f.flush()
                os.fsync(f.fileno())
            logger.info("[%s] Config saved: %s", run_id, config_path)
        except Exception as e:  # noqa: BLE001
            logger.error("[%s] Failed to save config: %s", run_id, e)
            await self._mark_failed(run_id, tenant_id, user_id, f"[ERROR] 写配置失败: {e}")
            return

        # 子进程环境：继承完整环境，覆盖训练关键变量（真实路径，无挂载换算）
        context = (
            payload.get("context") if isinstance(payload.get("context"), dict) else {}
        )
        market = normalize_market(context.get("market") or "CN")
        data_root = local_market_data_root(market)
        snap_root = local_snapshot_root()

        env = dict(os.environ)
        env.update({
            "INTERNAL_CALL_SECRET": self.internal_secret,
            "USE_LOCAL_DATA": "true",
            "TRAINING_LOCAL_DATA_DIR": str(snap_root or data_root),
            "TRAINING_CACHE_DIR": str(work_dir / "cache"),
            "TRAIN_IC_WORKERS": os.getenv("TRAIN_IC_WORKERS", ""),
            "TRAIN_NTHREADS": os.getenv("TRAIN_NTHREADS", ""),
            "QLIB_PROVIDER_URI": os.getenv("QLIB_PROVIDER_URI", ""),
        })
        # train.py 按市场读的数据根环境变量（容器模式是挂载目录名，此处为真实路径）
        _market_env_names = {
            "CN": "QUANTDB_DATA_DIR",
            "HK": "QUANTHK_DATA_DIR",
            "US": "QUANTUS_DATA_DIR",
            "CRYPTO": "QUANTBC_DATA_DIR",
            "FUTURES": "QUANTFUTURES_DATA_DIR",
        }
        env[_market_env_names[market]] = str(data_root)
        # 保证子进程能从仓库根 import backend.*（与 docker 容器 /app 等价）
        python_path = os.pathsep.join(
            p for p in (str(repo_root_dir()), env.get("PYTHONPATH", "")) if p
        )
        env["PYTHONPATH"] = python_path

        # GPU 探测提示（与 Docker 编排同文案，train.py 内部仍会自行回落 CPU）
        gpu_available = await self._detect_process_gpu()
        if not gpu_available:
            self.log_stream.append_log(
                run_id=run_id,
                tenant_id=tenant_id,
                user_id=user_id,
                line="[SYSTEM] 未检测到可用 GPU，改用 CPU 训练（DL 模型耗时显著增加）",
                status="provisioning",
                progress=5,
            )
            logger.info("[%s] GPU not detected, running in CPU mode", run_id)

        # 启动训练子进程（-u 无缓冲，保证日志流实时可读）
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-u",
                str(self.train_script),
                "--config",
                str(config_path),
                cwd=str(self.train_script.parent),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except Exception as e:  # noqa: BLE001
            logger.error("[%s] spawn train.py failed: %s", run_id, e)
            await self._mark_failed(
                run_id, tenant_id, user_id, f"[ERROR] 启动 train.py 失败: {e}"
            )
            return

        async with get_session() as db:
            record = await db.get(TrainingJobRecord, run_id)
            if record:
                record.status = "running"
                record.progress = max(int(record.progress or 0), 12)
                record.instance_id = str(proc.pid)
                record.logs = (
                    record.logs or ""
                ) + f"Process PID: {proc.pid}\n"
                await db.commit()
        self.log_stream.append_log(
            run_id=run_id,
            tenant_id=tenant_id,
            user_id=user_id,
            line=f"[SYSTEM] Process PID: {proc.pid}",
            status="running",
            progress=12,
        )

        try:
            max_time_minutes = max(10, int(payload.get("max_time_minutes") or 120))
        except Exception:  # noqa: BLE001
            max_time_minutes = 120

        REGISTRY.register(
            self._monitor_process(
                run_id,
                proc,
                tenant_id=tenant_id,
                user_id=user_id,
                work_dir=work_dir,
                max_time_minutes=max_time_minutes,
            )
        )

    # ── 轮询训练子进程：增量日志 → 状态落库 → 等待回调 ────────────────────────
    async def _monitor_process(
        self,
        run_id: str,
        proc: asyncio.subprocess.Process,
        *,
        tenant_id: str,
        user_id: str,
        work_dir: Path,
        max_time_minutes: int = 120,
    ) -> None:
        from backend.services.api.routers.admin.db import TrainingJobRecord
        from backend.shared.database_manager_v2 import get_session

        deadline = time.time() + max_time_minutes * 60
        tail: deque[str] = deque(maxlen=120)
        current_progress = 12

        # 增量读取 stdout（含 stderr），实时写日志流并推断进度
        while True:
            remain = deadline - time.time()
            if remain <= 0:
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass
                await asyncio.to_thread(proc.wait)
                tail_text = "\n".join(tail)
                await self._mark_failed(
                    run_id,
                    tenant_id,
                    user_id,
                    f"[TIMEOUT] {max_time_minutes}min limit exceeded\n{tail_text}",
                    progress=100,
                )
                return
            try:
                raw = await asyncio.wait_for(
                    proc.stdout.readline(), timeout=min(5.0, max(0.1, remain))
                )
            except asyncio.TimeoutError:
                continue
            except Exception as read_err:  # noqa: BLE001
                logger.warning("[%s] read train log failed: %s", run_id, read_err)
                break
            if not raw:
                break
            line = raw.decode("utf-8", errors="replace").rstrip()
            if not line:
                continue
            tail.append(line)
            current_progress = self._infer_progress_from_log_line(line, current_progress)
            self.log_stream.append_log(
                run_id=run_id,
                tenant_id=tenant_id,
                user_id=user_id,
                line=line,
                status="running",
                progress=current_progress,
            )

        exit_code = await proc.wait()
        tail_text = "\n".join(tail)

        if exit_code == 0:
            async with get_session() as db:
                r = await db.get(TrainingJobRecord, run_id)
                if r:
                    r.status = "waiting_callback"
                    r.progress = max(int(r.progress or 0), 95)
                    r.logs = (
                        (r.logs or "")
                        + f"[DONE] Process exited 0, waiting callback\n{tail_text}"
                    )
                    await db.commit()
            self.log_stream.append_log(
                run_id=run_id,
                tenant_id=tenant_id,
                user_id=user_id,
                line="[DONE] Process exited 0, waiting callback",
                status="waiting_callback",
                progress=95,
            )
            # 等回调：回调一到即结束（产物同步与模型注册都在回调内完成）
            callback_deadline = time.time() + _CALLBACK_TIMEOUT
            while time.time() < callback_deadline:
                await asyncio.sleep(max(1, _CALLBACK_CHECK_INTERVAL))
                async with get_session(read_only=True) as db:
                    r = await db.get(TrainingJobRecord, run_id)
                    if r and str(r.status or "") in {"completed", "failed"}:
                        return
            await self._mark_failed(
                run_id,
                tenant_id,
                user_id,
                "[TIMEOUT] Callback not received",
                progress=100,
            )
        else:
            await self._mark_failed(
                run_id,
                tenant_id,
                user_id,
                f"[FAILED] ExitCode={exit_code}\n{tail_text}",
                progress=100,
            )
            logger.error("[%s] Training failed, ExitCode=%d", run_id, exit_code)

    async def _mark_failed(
        self,
        run_id: str,
        tenant_id: str,
        user_id: str,
        log_text: str,
        *,
        progress: int = 100,
    ) -> None:
        """统一失败落库（DB + 日志流），多周期父任务会读取该终态。"""
        from backend.services.api.routers.admin.db import TrainingJobRecord
        from backend.shared.database_manager_v2 import get_session

        async with get_session() as db:
            r = await db.get(TrainingJobRecord, run_id)
            if r:
                r.status = "failed"
                r.progress = progress
                r.logs = (r.logs or "") + log_text.rstrip() + "\n"
                await db.commit()
        self.log_stream.append_log(
            run_id=run_id,
            tenant_id=tenant_id,
            user_id=user_id,
            line=log_text.splitlines()[-1] if log_text else "failed",
            status="failed",
            progress=progress,
        )
