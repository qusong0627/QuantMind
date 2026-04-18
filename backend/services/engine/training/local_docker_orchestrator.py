"""
QuantMind 本地 Docker 训练编排器
==================================
使用本机 docker run 异步执行训练任务，无需云 BatchCompute。

流程：
  1. 生成并挂载 config.yaml
  2. docker run -d 启动训练容器（加入 quantmind-network）
  3. 轮询容器状态，写回 DB
  4. 训练容器完成后通过 callback 回写结果
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

import docker
from docker import DockerClient
import yaml

from backend.services.engine.training.training_log_stream import TrainingRunLogStream

logger = logging.getLogger(__name__)

_TRAINING_IMAGE = (os.getenv("TRAINING_IMAGE") or "quantmind-ml-runtime:latest").strip()
_CALLBACK_TIMEOUT = int(os.getenv("TRAINING_CALLBACK_TIMEOUT_SECONDS", "600"))
_POLL_INTERVAL = 10  # 秒
_CALLBACK_CHECK_INTERVAL = int(
    os.getenv("TRAINING_CALLBACK_CHECK_INTERVAL_SECONDS", "2")
)
_DOCKER_NETWORK = os.getenv("TRAINING_DOCKER_NETWORK", "quantmind-network")
_HOST_PROJECT_PATH = Path(
    os.getenv("HOST_PROJECT_PATH")
    or os.getenv("TRAINING_HOST_PROJECT_PATH")
    or "/home/ubuntu/quantmind"
).expanduser()
_LOCAL_DATA_MOUNT_DIR = "/tmp/feature_snapshots"
_LOCAL_DATA_PATH = str(
    Path(
        os.getenv(
            "TRAINING_LOCAL_DATA_PATH",
            str(_HOST_PROJECT_PATH / "db" / "feature_snapshots"),
        )
    )
)
_TRAINING_SCRIPT_HOST_PATH = Path(
    os.getenv(
        "TRAINING_SCRIPT_HOST_PATH",
        str(_HOST_PROJECT_PATH / "docker" / "training" / "train.py"),
    )
).expanduser()


class LocalDockerOrchestrator:
    def __init__(self):
        self.docker = DockerClient.from_env()
        self.api_base = (
            os.getenv("QUANTMIND_API_BASE_URL") or "http://quantmind-api:8000"
        ).strip()
        self.internal_secret = (os.getenv("INTERNAL_CALL_SECRET") or "").strip()
        self.log_stream = TrainingRunLogStream()

    @staticmethod
    def _parse_docker_log_entry(raw_line: str) -> tuple[float, str]:
        """解析 `docker logs --timestamps` 单行，返回 (timestamp, message)。"""
        line = str(raw_line or "").rstrip("\n")
        if not line:
            return 0.0, ""
        if " " not in line:
            return 0.0, line
        ts_part, msg_part = line.split(" ", 1)
        ts_val = 0.0
        try:
            ts_val = datetime.fromisoformat(ts_part.replace("Z", "+00:00")).timestamp()
        except Exception:
            ts_val = 0.0
        return ts_val, msg_part.rstrip("\n")

    @staticmethod
    def _infer_progress_from_log_line(line: str, current: int) -> int:
        text = str(line or "").lower()
        next_progress = int(current)
        if "local data hit:" in text:
            next_progress = max(next_progress, 22)
        if "raw concat size" in text:
            next_progress = max(next_progress, 30)
        if "after date range clip" in text or "data ready:" in text:
            next_progress = max(next_progress, 42)
        if "split mode:" in text or "val_ratio mode:" in text:
            next_progress = max(next_progress, 50)
        if "training finished" in text:
            next_progress = max(next_progress, 86)
        if "result report saved" in text:
            next_progress = max(next_progress, 92)
        return min(99, next_progress)

    # ── 构造 config.yaml 内容 ───────────────────────────────────────────────────
    def _build_config_yaml(self, run_id: str, payload: dict) -> dict:
        if payload is None:
            logger.error(
                "[%s] Payload is None in _build_config_yaml, using absolute defaults",
                run_id,
            )
            payload = {}
        context = (
            payload.get("context") if isinstance(payload.get("context"), dict) else {}
        )

        # 强制使用本地数据，不回落到 COS 下载
        data_source_mode = payload.get("data_source_mode", "LOCAL")

        config: dict[str, Any] = {
            "run_id": run_id,
            "job_name": payload.get("job_name", "unnamed"),
            "data": {
                "train_start": payload.get("train_start", "2022-01-01"),
                "train_end": payload.get("train_end", "2024-12-31"),
                "features": payload.get("features", []),
                "source_mode": data_source_mode,
                "local_dir": _LOCAL_DATA_MOUNT_DIR
                if data_source_mode == "LOCAL"
                else None,
            },
            "model": {
                "type": payload.get("model_type", "lightgbm"),
                "num_boost_round": payload.get("num_boost_round", 1000),
                "early_stopping_rounds": payload.get("early_stopping_rounds", 100),
                "val_ratio": payload.get("val_ratio", 0.15),
                "params": payload.get("lgb_params", {}),
            },
            "label": {
                "target_horizon_days": payload.get("target_horizon_days", 1),
                "target_mode": payload.get("target_mode", "return"),
                "label_formula": payload.get("label_formula", ""),
                "effective_trade_date": payload.get("effective_trade_date", ""),
                "training_window": payload.get("training_window", ""),
            },
            "context": {
                "initial_capital": context.get("initial_capital", 1_000_000),
                "benchmark": context.get("benchmark", "SH000300"),
                "commission_rate": context.get("commission_rate", 0.00025),
                "slippage": context.get("slippage", 0.0005),
                "deal_price": context.get("deal_price", "close"),
            },
            "output": {
                "result_path": "/workspace/result.json",
                "required_artifacts": payload.get(
                    "required_artifacts",
                    ["model.lgb", "pred.pkl", "metadata.json", "result.json"],
                ),
            },
            "callback": {
                "url": f"{self.api_base}/api/v1/models/training-runs/{run_id}/complete",
                "secret": self.internal_secret,
            },
            "cache": {"dir": "/tmp" if data_source_mode == "LOCAL" else None},
        }
        # 显式时间段切分（valid_start/end 优先于 val_ratio）
        split_fields: list[str] = ["valid_start", "valid_end", "test_start", "test_end"]
        if all(payload.get(k) for k in split_fields):
            config["split"] = {
                "train": [payload.get("train_start"), payload.get("train_end")],
                "valid": [payload.get("valid_start"), payload.get("valid_end")],
                "test": [payload.get("test_start"), payload.get("test_end")],
            }
            config["model"]["val_ratio"] = None
        return config

    # ── 启动训练任务 ─────────────────────────────────────────────────────────────
    async def launch_training_job(self, run_id: str, payload: dict = None) -> None:
        from backend.shared.database_manager_v2 import get_session
        from backend.services.api.routers.admin.db import TrainingJobRecord

        if payload is None:
            logger.error("[%s] Orchestrator received None payload!", run_id)
            payload = {}

        config = self._build_config_yaml(run_id, payload)
        async with get_session() as db:
            record = await db.get(TrainingJobRecord, run_id)
            if record:
                record.status = "provisioning"
                record.progress = max(int(record.progress or 0), 5)
                # 增量记录日志，防止覆盖 [SYSTEM] 训练任务已创建
                record.logs = (
                    record.logs or ""
                ) + f"Starting container: {_TRAINING_IMAGE}\n"
                user_id = str(record.user_id or "unknown")
                tenant_id = str(record.tenant_id or "default")

                # 记录系统通知(如日期自动修正)
                notices = payload.get("system_notices") or []
                for msg in notices:
                    record.logs += f"[NOTICE] {msg}\n"

                await db.commit()
                self.log_stream.append_log(
                    run_id=run_id,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    line=f"[SYSTEM] Starting container image: {_TRAINING_IMAGE}",
                    status="provisioning",
                    progress=5,
                )
                # 同时也发到实时日志流
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

        # ── 准备本地模型存储路径 ────────────────────────────────────────────────
        # 必须与 ModelRegistryService.register_model_from_training_run 的路径逻辑一致
        from backend.shared.model_registry import model_registry_service

        model_id = model_registry_service.build_model_id_from_run(run_id)

        user_models_root = Path(model_registry_service.user_models_root)
        internal_models_root = (
            user_models_root
            if user_models_root.is_absolute()
            else Path("/app") / user_models_root
        )
        host_models_root = (
            user_models_root
            if user_models_root.is_absolute()
            else (_HOST_PROJECT_PATH / user_models_root)
        )
        internal_output_dir = internal_models_root / tenant_id / user_id / model_id
        host_output_dir = host_models_root / tenant_id / user_id / model_id

        # 强制创建目录（容器内路径用于回调热路径，宿主机路径用于 docker -v）
        os.makedirs(internal_output_dir, exist_ok=True)
        os.makedirs(host_output_dir, exist_ok=True)
        logger.info(
            "[%s] Local model output directory prepared: %s",
            run_id,
            internal_output_dir,
        )

        # ── 提前将 config.yaml 写入本地目录 ───────────────────────────────────
        local_config_path = internal_output_dir / "config.yaml"
        try:
            with open(local_config_path, "w", encoding="utf-8") as f:
                yaml.dump(config, f, allow_unicode=True, default_flow_style=False)
            logger.info("[%s] Config saved locally: %s", run_id, local_config_path)
        except Exception as e:
            logger.warning("[%s] Failed to save config locally: %s", run_id, e)

        # 始终挂载本地数据目录（宿主机路径，API 容器内 os.path.exists 无法感知）
        volumes: dict[str, dict[str, str]] = {
            str(host_output_dir): {"bind": "/workspace", "mode": "rw"},
            str(_LOCAL_DATA_PATH): {"bind": _LOCAL_DATA_MOUNT_DIR, "mode": "ro"},
        }
        logger.info(
            "[%s] Local data path mounted: %s -> %s",
            run_id,
            _LOCAL_DATA_PATH,
            _LOCAL_DATA_MOUNT_DIR,
        )
        # 始终挂载宿主机 train.py 覆盖镜像内脚本（注意：os.path.exists 在 API 容器内无法感知宿主机路径，固定挂载）
        volumes[str(_TRAINING_SCRIPT_HOST_PATH)] = {
            "bind": "/app/train.py",
            "mode": "ro",
        }
        logger.info(
            "[%s] Local train.py override mounted: %s -> /app/train.py",
            run_id,
            _TRAINING_SCRIPT_HOST_PATH,
        )
        logger.info(
            "[%s] PERSISTENCE Local output mounted: %s -> /workspace",
            run_id,
            host_output_dir,
        )
        logger.info("[%s] Final volumes config: %s", run_id, volumes)

        try:
            container = await asyncio.to_thread(
                self.docker.containers.run,
                _TRAINING_IMAGE,
                command="python /app/train.py --config /workspace/config.yaml",
                environment={
                    "INTERNAL_CALL_SECRET": self.internal_secret,
                    "USE_LOCAL_DATA": "true",
                    "TRAINING_LOCAL_DATA_DIR": _LOCAL_DATA_MOUNT_DIR,
                    "TRAINING_CACHE_DIR": "/tmp",
                },
                volumes=volumes,
                network=_DOCKER_NETWORK,
                detach=True,
                name=f"qm-train-{run_id}",
            )
        except Exception as e:
            from backend.shared.database_manager_v2 import get_session
            from backend.services.api.routers.admin.db import TrainingJobRecord

            logger.error("[%s] docker run failed: %s", run_id, e)
            async with get_session() as db:
                record = await db.get(TrainingJobRecord, run_id)
                if record:
                    record.status = "failed"
                    record.logs = (
                        record.logs or ""
                    ) + f"[ERROR] docker run failed: {e}\n"
                    record.progress = 100
                    await db.commit()
            self.log_stream.append_log(
                run_id=run_id,
                tenant_id=tenant_id,
                user_id=user_id,
                line=f"[ERROR] docker run failed: {e}",
                status="failed",
                progress=100,
            )
            return

        logger.info("[%s] Container started: %s", run_id, container.id[:12])
        async with get_session() as db:
            record = await db.get(TrainingJobRecord, run_id)
            if record:
                record.status = "running"
                record.progress = max(int(record.progress or 0), 12)
                record.instance_id = container.id[:12]
                record.logs = (
                    record.logs or ""
                ) + f"Container ID: {container.id[:12]}\n"
                await db.commit()
        self.log_stream.append_log(
            run_id=run_id,
            tenant_id=tenant_id,
            user_id=user_id,
            line=f"[SYSTEM] Container ID: {container.id[:12]}",
            status="running",
            progress=12,
            container_id=container.id[:12],
        )

        asyncio.create_task(
            self._poll_container(
                run_id, container.id, tenant_id=tenant_id, user_id=user_id
            )
        )

    # ── 轮询容器状态 ─────────────────────────────────────────────────────────────
    async def _poll_container(
        self, run_id: str, container_id: str, *, tenant_id: str, user_id: str
    ) -> None:
        from backend.services.api.routers.admin.db import TrainingJobRecord
        from backend.shared.database_manager_v2 import get_session

        deadline = time.time() + 7200  # 最长 2h
        log_cursor_ts = max(0.0, time.time() - 2)
        last_log_sig = ""
        current_progress = 12

        while time.time() < deadline:
            await asyncio.sleep(_POLL_INTERVAL)
            try:
                c = self.docker.containers.get(container_id)
                c.reload()
                status = c.attrs["State"].get("Status", "")
                exit_code = c.attrs["State"].get("ExitCode", -1)

                # 增量抓取容器日志并写入回测 Redis，供前端轮询时查看真实进度
                try:
                    raw_logs = c.logs(
                        stdout=True,
                        stderr=True,
                        since=max(0, int(log_cursor_ts) - 1),
                        timestamps=True,
                    ).decode("utf-8", errors="replace")
                    if raw_logs:
                        for raw_line in raw_logs.splitlines():
                            ts_val, msg = self._parse_docker_log_entry(raw_line)
                            if not msg:
                                continue
                            sig = f"{ts_val:.6f}:{msg}"
                            if sig == last_log_sig:
                                continue
                            if ts_val > 0:
                                log_cursor_ts = max(log_cursor_ts, ts_val)
                            last_log_sig = sig
                            current_progress = self._infer_progress_from_log_line(
                                msg, current_progress
                            )
                            self.log_stream.append_log(
                                run_id=run_id,
                                tenant_id=tenant_id,
                                user_id=user_id,
                                line=msg,
                                status="running",
                                progress=current_progress,
                                container_id=container_id[:12],
                            )
                except Exception as log_err:
                    logger.debug(
                        "[%s] incremental log fetch failed: %s", run_id, log_err
                    )

                if status in ("running", "created"):
                    continue

                # 容器已结束，获取最后100行日志
                tail_logs = c.logs(tail=100).decode("utf-8", errors="replace")

                if exit_code == 0:
                    async with get_session() as db:
                        r = await db.get(TrainingJobRecord, run_id)
                        if r:
                            r.status = "waiting_callback"
                            r.progress = max(int(r.progress or 0), 95)
                            r.logs = (
                                (r.logs or "")
                                + f"[DONE] Container exited 0, waiting callback\n{tail_logs}"
                            )
                            await db.commit()
                    self.log_stream.append_log(
                        run_id=run_id,
                        tenant_id=tenant_id,
                        user_id=user_id,
                        line="[DONE] Container exited 0, waiting callback",
                        status="waiting_callback",
                        progress=95,
                        container_id=container_id[:12],
                    )
                    # 等 callback；回调一到立即结束等待并清理容器，避免容器长时间停留在 Exited
                    callback_deadline = time.time() + _CALLBACK_TIMEOUT
                    callback_received = False
                    while time.time() < callback_deadline:
                        await asyncio.sleep(max(1, _CALLBACK_CHECK_INTERVAL))
                        async with get_session(read_only=True) as db:
                            r = await db.get(TrainingJobRecord, run_id)
                            if r and str(r.status or "") in {"completed", "failed"}:
                                callback_received = True
                                break
                    if not callback_received:
                        async with get_session() as db:
                            r = await db.get(TrainingJobRecord, run_id)
                            if r and r.status == "waiting_callback":
                                r.status = "failed"
                                r.logs = (
                                    r.logs or ""
                                ) + "[TIMEOUT] Callback not received\n"
                                r.progress = 100
                                await db.commit()
                                self.log_stream.append_log(
                                    run_id=run_id,
                                    tenant_id=tenant_id,
                                    user_id=user_id,
                                    line="[TIMEOUT] Callback not received",
                                    status="failed",
                                    progress=100,
                                    container_id=container_id[:12],
                                )
                else:
                    async with get_session() as db:
                        r = await db.get(TrainingJobRecord, run_id)
                        if r:
                            r.status = "failed"
                            r.logs = (
                                r.logs or ""
                            ) + f"[FAILED] ExitCode={exit_code}\n{tail_logs}"
                            r.progress = 100
                            await db.commit()
                    self.log_stream.append_log(
                        run_id=run_id,
                        tenant_id=tenant_id,
                        user_id=user_id,
                        line=f"[FAILED] ExitCode={exit_code}",
                        status="failed",
                        progress=100,
                        container_id=container_id[:12],
                    )
                    logger.error("[%s] Training failed, ExitCode=%d", run_id, exit_code)

                try:
                    c.remove(force=True, v=True)
                except Exception:
                    pass
                return

            except docker.errors.NotFound:
                async with get_session() as db:
                    r = await db.get(TrainingJobRecord, run_id)
                    if r and r.status not in ("completed", "failed"):
                        r.status = "failed"
                        r.logs = (r.logs or "") + "[ERROR] Container not found\n"
                        r.progress = 100
                        await db.commit()
                        self.log_stream.append_log(
                            run_id=run_id,
                            tenant_id=tenant_id,
                            user_id=user_id,
                            line="[ERROR] Container not found",
                            status="failed",
                            progress=100,
                            container_id=container_id[:12],
                        )
                return
            except Exception as e:
                logger.warning("[%s] poll error: %s", run_id, e)

        # 超出 2h 限制
        async with get_session() as db:
            r = await db.get(TrainingJobRecord, run_id)
            if r and r.status not in ("completed", "failed"):
                r.status = "failed"
                r.logs = (r.logs or "") + "[TIMEOUT] 2h limit exceeded\n"
                r.progress = 100
                await db.commit()
                self.log_stream.append_log(
                    run_id=run_id,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    line="[TIMEOUT] 2h limit exceeded",
                    status="failed",
                    progress=100,
                    container_id=container_id[:12],
                )
