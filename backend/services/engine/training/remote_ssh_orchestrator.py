"""AutoDL 远程 GPU 训练编排器。

通过 SSH/rsync/scp 驱动远端 AutoDL 节点执行训练：
  1. rsync 推送特征快照（按训练区间选年）到远端
  2. rsync 推送 config.yaml
  3. ssh 远端 docker run 启动训练容器（复用 train.py）
  4. 轮询远端容器日志，解析进度推送到 Redis（与本地一致）
  5. 训练完成后 scp 拉取模型产物到本地工作目录
  6. 走现有模型注册流程（register_model_from_training_run）

依赖：系统 ssh/scp/rsync 命令行（asyncio.create_subprocess_exec），零额外 Python 依赖。
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import tempfile
from pathlib import Path
from typing import Any

import yaml

from backend.services.engine.training.orchestrator_base import TrainingOrchestrator, REGISTRY
from backend.services.engine.training.training_log_stream import TrainingRunLogStream
from backend.services.api.training_explain import DEFAULT_EXPLAIN_CFG

logger = logging.getLogger(__name__)


def _env_or(key: str, default: str) -> str:
    return (os.getenv(key) or default).strip()


class RemoteSSHOrchestrator(TrainingOrchestrator):
    """AutoDL 远程 GPU 训练编排器。

    配置来源（环境变量）：
      TRAINING_AUTODL_HOST          远端 IP/域名
      TRAINING_AUTODL_SSH_PORT      SSH 端口（默认 22）
      TRAINING_AUTODL_USER          SSH 用户（默认 root）
      TRAINING_AUTODL_SSH_KEY       SSH 私钥路径（可选，默认 ~/.ssh/id_rsa）
      TRAINING_AUTODL_WORK_DIR      远端工作目录（默认 /workspace）
      TRAINING_AUTODL_DOCKER_IMAGE  远端训练镜像（默认 quantmind-oss:latest）
      TRAINING_AUTODL_NODE_NAME     节点标识（默认 autodl-1）
    """

    _POLL_INTERVAL = 10  # 容器状态轮询间隔（秒）
    _LOG_TAIL_LINES = 60

    def __init__(self, node_id: str = "autodl-1", node_config: dict[str, Any] | None = None):
        self.node_id = node_id
        # 优先使用传入的节点配置（多节点 YAML）；否则回退单节点环境变量
        if node_config:
            self.host = str(node_config.get("host") or "")
            self.port = int(node_config.get("port") or 22)
            self.user = str(node_config.get("user") or "root")
            self.ssh_key = str(node_config.get("ssh_key") or "")
            self.ssh_password = str(node_config.get("ssh_password") or "")
            self.work_dir = str(node_config.get("work_dir") or "/workspace")
            self.docker_image = str(node_config.get("docker_image") or "quantmind-oss:latest")
            self.gpus = str(node_config.get("gpus") or "").strip()
            self.quantdb_dir = str(node_config.get("quantdb_dir") or "/data/quantdb")
            # 免 Docker 直跑节点（executor=process）：服务器节点包形态
            # pack_root 包根；runtime_python 内嵌解释器；env_file 装载包环境
            # （QUANTDB_API_KEY / QM_*_DATA_DIR 等，密钥只留在服务器侧）
            self.executor = str(node_config.get("executor") or "docker").lower()
            self.pack_root = str(node_config.get("pack_root") or "").rstrip("/")
            self.runtime_python = str(node_config.get("runtime_python") or "").strip()
            self.env_file = str(node_config.get("env_file") or "train_env.sh").strip()
            self.sync_cmd = str(node_config.get("sync_cmd") or "").strip()
        else:
            self.host = _env_or("TRAINING_AUTODL_HOST", "")
            self.port = int(_env_or("TRAINING_AUTODL_SSH_PORT", "22"))
            self.user = _env_or("TRAINING_AUTODL_USER", "root")
            self.ssh_key = _env_or("TRAINING_AUTODL_SSH_KEY", "")
            self.ssh_password = _env_or("TRAINING_AUTODL_SSH_PASSWORD", "")
            self.work_dir = _env_or("TRAINING_AUTODL_WORK_DIR", "/workspace")
            self.docker_image = _env_or("TRAINING_AUTODL_DOCKER_IMAGE", "quantmind-oss:latest")
            # 远端容器挂载的 GPU（all=全部，0/空=不挂载，1/2=指定数量）
            # AutoDL 节点需安装 nvidia-container-toolkit 才能使用 GPU
            self.gpus = _env_or("TRAINING_AUTODL_GPUS", "").strip()
            self.quantdb_dir = _env_or("TRAINING_AUTODL_QUANTDB_DIR", "/data/quantdb")
            self.executor = _env_or("TRAINING_AUTODL_EXECUTOR", "docker").lower()
            self.pack_root = _env_or("TRAINING_AUTODL_PACK_ROOT", "").rstrip("/")
            self.runtime_python = _env_or("TRAINING_AUTODL_RUNTIME_PYTHON", "")
            self.env_file = _env_or("TRAINING_AUTODL_ENV_FILE", "train_env.sh")
            self.sync_cmd = _env_or("TRAINING_AUTODL_SYNC_CMD", "")
        self.api_base = _env_or("QUANTMIND_API_BASE_URL", "http://quantmind-api:8000")
        # 主节点局域网地址（供远端容器回调）；为空则回退 api_base（可能不可达）
        self.master_host = _env_or("TRAINING_MASTER_HOST", "")
        self.internal_secret = _env_or("INTERNAL_CALL_SECRET", "")
        self.log_stream = TrainingRunLogStream()
        self._tenant_id = _env_or("TRAINING_DEFAULT_TENANT", "default")
        self._user_id = _env_or("TRAINING_DEFAULT_USER", "admin")

        if not self.host:
            raise ValueError(
                f"训练节点 {node_id} 未配置 host（检查 config/training_nodes.yaml 或 TRAINING_AUTODL_HOST）。"
            )
        # P0-3: 强制 fail-closed，secret 缺失直接抛错
        if not self.internal_secret:
            raise RuntimeError(
                "INTERNAL_CALL_SECRET not set; cannot start remote training orchestrator. "
                "Set it in .env or QUANTMIND_ENV=development for auto-generation."
            )

    # ── SSH 基础工具（asyncio subprocess，零额外依赖） ──────────────────────────

    def _auth_prefix(self) -> list[str]:
        """SSH 认证前缀：密码用 sshpass，否则用 key。"""
        if self.ssh_password:
            return ["sshpass", "-p", self.ssh_password]
        return []

    def _ssh_base_args(self) -> list[str]:
        args = self._auth_prefix() + [
            "ssh",
            "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=15",
            "-p", str(self.port),
        ]
        if self.ssh_key:
            args += ["-i", self.ssh_key]
        args.append(f"{self.user}@{self.host}")
        return args

    async def _ssh_exec(self, cmd: str, *, timeout: int = 900) -> tuple[int, str, str]:
        """SSH 执行远端命令。返回 (exit_code, stdout, stderr)。"""
        proc = await asyncio.create_subprocess_exec(
            *self._ssh_base_args(),
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            raise
        return proc.returncode or 0, stdout.decode(errors="replace"), stderr.decode(errors="replace")

    async def _rsync_push(self, local_path: str, remote_dir: str, *, is_dir: bool = False) -> None:
        """rsync 推送本地文件/目录到远端目录。"""
        ssh_opt = f"ssh -o StrictHostKeyChecking=no -p {self.port}"
        if self.ssh_password:
            ssh_opt = f"sshpass -p {self.ssh_password} " + ssh_opt
        elif self.ssh_key:
            ssh_opt += f" -i {self.ssh_key}"
        cmd = [
            "rsync", "-avz", "--partial",
            "-e", ssh_opt,
        ]
        if is_dir:
            cmd += ["--delete"]
        src = local_path.rstrip("/") + ("/" if is_dir else "")
        dst = f"{self.user}@{self.host}:{remote_dir}"
        proc = await asyncio.create_subprocess_exec(
            *cmd, src, dst,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.wait()

    async def _scp_pull(self, remote_file: str, local_dir: Path) -> None:
        """scp 拉取远端单个文件到本地目录（幂等，文件不存在则跳过）。"""
        local_dir.mkdir(parents=True, exist_ok=True)
        cmd = self._auth_prefix() + [
            "scp", "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=15",
            "-P", str(self.port),
        ]
        if self.ssh_key:
            cmd += ["-i", self.ssh_key]
        cmd += [f"{self.user}@{self.host}:{remote_file}", str(local_dir)]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.wait()

    async def _scp_push(self, local_file: str, remote_path: str) -> None:
        """scp 推送本地单个文件到远端指定路径（可指定目标文件名）。"""
        cmd = self._auth_prefix() + [
            "scp", "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=15",
            "-P", str(self.port),
        ]
        if self.ssh_key:
            cmd += ["-i", self.ssh_key]
        cmd += [local_file, f"{self.user}@{self.host}:{remote_path}"]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.wait()


    async def launch_multi_horizon_job(
        self, parent_run_id: str, child_run_ids: list[str], payload: dict | None = None
    ) -> None:
        """远端多周期训练：串行跑各周期子任务（每次远端训练），全部成功后 ICIR 融合。

        每个 child 走自身的 launch_training_job（远端训练 + 产物回传 + 注册），
        全部完成后在本地 register_ensemble_model 创建融合模型（产物已回传，可直接融合）。
        """
        import time

        from backend.shared.database_manager_v2 import get_session
        from backend.services.api.routers.admin.db import TrainingJobRecord
        from backend.shared.model_registry import model_registry_service

        payload = payload or {}
        tenant_id = str(payload.get("_tenant_id") or "")
        user_id = str(payload.get("_user_id") or "")
        if not tenant_id or not user_id:
            try:
                async with get_session() as db:
                    parent_rec = await db.get(TrainingJobRecord, parent_run_id)
                    if parent_rec:
                        tenant_id = str(parent_rec.tenant_id or "default")
                        user_id = str(parent_rec.user_id or "")
            except Exception:
                pass
        self._tenant_id = tenant_id or "default"
        self._user_id = user_id or "admin"
        display_name = str(payload.get("display_name") or "multi_horizon")

        async def _set_parent(status: str, progress: int, log_line: str) -> None:
            try:
                async with get_session() as db:
                    r = await db.get(TrainingJobRecord, parent_run_id)
                    if r:
                        r.status = status
                        r.progress = progress
                        r.logs = (r.logs or "") + log_line
                        await db.commit()
                self._log(parent_run_id, log_line.strip(), status=status, progress=progress)
            except Exception:
                pass

        try:
            await _set_parent("provisioning", 5, f"[MH] 多周期远程训练启动，共 {len(child_run_ids)} 个周期\n")
            completed_model_ids: list[str] = []
            completed_run_ids: set[str] = set()
            horizon_labels: list[str] = []
            n_total = len(child_run_ids)

            for idx, child_run_id in enumerate(child_run_ids):
                async with get_session() as db:
                    child_rec = await db.get(TrainingJobRecord, child_run_id)
                    if child_rec is None:
                        raise RuntimeError(f"child run not found: {child_run_id}")
                    child_payload = (
                        child_rec.request_payload
                        if isinstance(child_rec.request_payload, dict)
                        else {}
                    )
                horizon = int(child_payload.get("target_horizon_days") or 0)
                horizon_labels.append(f"T{horizon}")
                base_progress = 5 + int((idx / n_total) * 90)
                await _set_parent("running", base_progress, f"[MH] ({idx + 1}/{n_total}) 训练 T+{horizon} 模型…\n")

                # 每个 child 走远端训练（内部：推送数据 → 远端 docker run → 拉产物 → 注册）
                await self.launch_training_job(run_id=child_run_id, payload=child_payload)

                child_deadline = time.time() + 7200
                while time.time() < child_deadline:
                    await asyncio.sleep(self._POLL_INTERVAL)
                    async with get_session(read_only=True) as db:
                        r = await db.get(TrainingJobRecord, child_run_id)
                        if r is None:
                            break
                        st = str(r.status or "")
                        if st == "completed":
                            completed_model_ids.append(
                                model_registry_service.build_model_id_from_run(child_run_id)
                            )
                            completed_run_ids.add(child_run_id)
                            break
                        if st == "failed":
                            raise RuntimeError(
                                f"child T+{horizon} training failed: {(r.result or {}).get('error') or (r.logs or '')[-300:]}"
                            )
                # 用 run_id 判断完成（completed_model_ids 存的是模型 ID，见 local 版同款修复）
                if child_run_id not in completed_run_ids:
                    raise RuntimeError(f"child T+{horizon} timed out waiting for completion")
                await _set_parent("running", 5 + int(((idx + 1) / n_total) * 90), f"[MH] T+{horizon} 模型训练完成（{idx + 1}/{n_total}）\n")

            if len(completed_model_ids) < 2:
                raise RuntimeError("multi-horizon requires at least 2 completed models")

            fusion_name = f"{display_name}_MultiHorizon"
            fusion = await model_registry_service.register_ensemble_model(
                tenant_id=tenant_id,
                user_id=user_id,
                source_model_ids=completed_model_ids,
                display_name=fusion_name,
                weight_strategy="icir",
            )
            await _set_parent("completed", 100, f"[MH] 多周期融合模型已创建: {fusion.get('model_id', '')}\n")
        except Exception as exc:  # noqa: BLE001
            logger.error("[%s] 多周期远程训练失败: %s", parent_run_id, exc, exc_info=True)
            await _set_parent("failed", 0, f"[MH] 多周期远程训练失败: {exc}\n")

    async def test_connection(self) -> dict:
        """测试 SSH 连接 + 远端执行环境(docker 或免 Docker runtime)。"""
        results = {}
        if self.executor == "process":
            runtime = self._process_runtime()
            env_sh = self._process_env_file()
            cmd = (
                f"echo OK && {{ [ -f {env_sh} ] && . {env_sh} || true; }} && "
                f"{runtime} --version 2>&1 | head -1 && "
                f"test -f {self.pack_root}/sync_factors.sh && echo sync_factors=OK"
            )
            code, out, err = await self._ssh_exec(cmd)
            results["ssh"] = code == 0 and "OK" in out
            results["docker"] = False
            results["runtime"] = code == 0 and "Python" in (out + err)
            if code == 0:
                results["host"] = self.host
                results["detail"] = (out + err).strip()
            else:
                results["error"] = (err or out).strip()
            return results

        code, out, err = await self._ssh_exec("echo OK && docker --version 2>&1 | head -1")
        results["ssh"] = code == 0 and "OK" in out
        results["docker"] = code == 0 and "Docker" in (out + err)
        if code == 0:
            results["host"] = self.host
            results["detail"] = (out + err).strip()
        else:
            results["error"] = (err or out).strip()
        return results

    # ── 训练编排 ───────────────────────────────────────────────────────────────

    async def launch_training_job(self, run_id: str, payload: dict | None = None) -> None:
        """编排远端训练：推送数据 → 启动容器 → 轮询 → 拉取产物 → 注册。"""
        payload = payload or {}
        # 从 DB 读取 tenant/user（与本地编排一致），供日志写入
        try:
            from backend.shared.database_manager_v2 import get_session
            from backend.services.api.routers.admin.db import TrainingJobRecord

            async with get_session() as _db:
                _record = await _db.get(TrainingJobRecord, run_id)
                if _record:
                    self._tenant_id = str(_record.tenant_id or "default")
                    self._user_id = str(_record.user_id or "admin")
        except Exception:
            pass
        self._log(run_id, "[SYSTEM] 远程训练启动（AutoDL），开始同步数据...", status="provisioning", progress=5)

        try:
            # Direct jobs bind exactly one raw QuantDB source; legacy jobs keep
            # their immutable snapshot mount for historical model compatibility.
            config = self._build_config_yaml(run_id, payload)
            direct_source = str(config["data"].get("factor_source") or "")
            # 远程节点（AutoDL）目前仅支持 A 股 QuantDB 直读：非 CN 市场的
            # 6_ml_datasets 数据不在同步清单内，硬走会把本地 CN 目录误当
            # 目标市场数据源（静默用错数据）。显式拒绝而非兜底。
            market = str((config.get("context") or {}).get("market") or "CN").upper()
            is_process = self.executor == "process"
            # docker 编排的 QuantDB 直读仍仅限 CN(远端同步清单限制);免 Docker 节点包
            # 自带各市场数据根 + sync_factors.sh(服务器侧维护市场因子同步),放行全市场
            if direct_source and market != "CN" and not is_process:
                raise RuntimeError(
                    f"远程节点暂不支持 {market} 市场 QuantDB 直读训练，"
                    "请选择本地 Docker 节点，或取消数据源直读（快照路径）"
                )
            if is_process:
                # 免 Docker：数据与产物走远端真实路径（市场 env 由包内 train_env.sh 提供）
                config["data"]["local_dir"] = f"{self.work_dir}/feature_snapshots"
                if direct_source:
                    config["data"]["local_dir"] = str(self.quantdb_dir or self.work_dir)
                if config.get("output"):
                    config["output"]["result_path"] = f"{self.work_dir}/result.json"
            else:
                config["data"]["local_dir"] = "/tmp/quantdb" if direct_source else "/tmp/feature_snapshots"
                if direct_source:
                    config["data"]["quantdb_dir"] = "/tmp/quantdb"
            config["callback"]["url"] = self._callback_url(run_id)

            # 2. 确保远端工作目录结构
            await self._ssh_exec(
                f"mkdir -p {self.work_dir}/feature_snapshots "
                f"{self.work_dir}/templates {self.work_dir}/modules"
            )

            # 3. 直读源数据同步（免 Docker 节点交给包内 sync_factors.sh <market>，
            #    市场数据根/凭据/同步器全在服务器侧，编排器只传市场名）
            if direct_source and is_process:
                if not self.pack_root:
                    raise RuntimeError(
                        "免 Docker 节点（executor=process）必须配置 pack_root（服务器节点包根目录）"
                    )
                sync_sh = f"{self.pack_root}/sync_factors.sh"
                code, out, err = await self._ssh_exec(
                    f"test -x {sync_sh} && cd {self.pack_root} && bash sync_factors.sh {market}",
                    timeout=1800,
                )
                if code != 0:
                    raise RuntimeError(f"远端因子同步失败(sync_factors.sh): {err or out}")
                self._log(run_id, f"[SYNC] 远端因子源已就绪({market}): {direct_source}", progress=15)
            elif direct_source:
                sync_cmd = _env_or(
                    "TRAINING_AUTODL_QUANTDB_SYNC_CMD",
                    "python /app/backend/scripts/quantdb_daily_sync.py",
                )
                quoted_dir = shlex.quote(self.quantdb_dir)
                code, out, err = await self._ssh_exec(
                    f"mkdir -p {quoted_dir} && QM_QUANTDB_DATA_DIR={quoted_dir} "
                    f"{sync_cmd} --parquet-only --datasets l1_factors,l2_factors,l1_l2_factors",
                    timeout=1800,
                )
                if code != 0:
                    raise RuntimeError(f"AutoDL QuantDB sync failed: {err or out}")
                self._log(run_id, f"[SYNC] QuantDB 因子源已增量同步: {direct_source}", progress=15)
            else:
                feature_files = self._resolve_feature_files(payload)
                if feature_files:
                    self._log(run_id, f"[SYNC] 推送 {len(feature_files)} 个特征快照到 AutoDL...", progress=10)
                    for f in feature_files:
                        if Path(f).exists():
                            await self._rsync_push(f, f"{self.work_dir}/feature_snapshots/")
                    self._log(run_id, "[SYNC] 特征快照同步完成", progress=15)
                else:
                    self._log(run_id, "[SYNC] 未匹配到特征快照文件，跳过", progress=15)

            # 4. 推送 config.yaml + train.py（写临时文件再 scp 到固定名）
            self._log(run_id, "[SYNC] 推送训练配置与训练脚本...", progress=18)
            with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as tf:
                yaml.safe_dump(config, tf, allow_unicode=True)
                config_local = tf.name
            await self._scp_push(config_local, f"{self.work_dir}/config.yaml")
            os.unlink(config_local)

            # 训练脚本 train.py 每次训练都推送最新版并挂载覆盖镜像内置版，
            # 这样更新 train.py 不需要重新打包/上传 AutoDL 镜像。
            train_script = self._resolve_train_script()
            if train_script:
                await self._rsync_push(train_script, f"{self.work_dir}/train.py")
                self._log(run_id, "[SYNC] train.py 已同步（覆盖镜像内置版）")

            # preprocessing.py 与 train.py 同目录顶层 import，需一并推送
            prep_script = self._resolve_preprocessing_script()
            if prep_script:
                await self._rsync_push(prep_script, f"{self.work_dir}/preprocessing.py")
                self._log(run_id, "[SYNC] preprocessing.py 已同步")

            # parallel_utils.py（多核因子筛选）与 train.py 同目录顶层 import，需一并推送
            par_script = self._resolve_parallel_utils_script()
            if par_script:
                await self._rsync_push(par_script, f"{self.work_dir}/parallel_utils.py")
                self._log(run_id, "[SYNC] parallel_utils.py 已同步")
            if direct_source:
                # docker: rsync 后挂载覆盖镜像内置路径；免 Docker：推成 backend 同
                # 相对路径，PYTHONPATH 以 work_dir 优先 → 与主节点同版本(含新数据集自动发现)
                dest_dir = (
                    f"{self.work_dir}/backend/services/engine/data_platform"
                    if is_process
                    else f"{self.work_dir}/modules/"
                )
                for module in (self._resolve_quantdb_factor_reader(), self._resolve_quantdb_hub()):
                    if module:
                        await self._rsync_push(module, dest_dir)
                self._log(run_id, "[SYNC] QuantDB 直读 Reader 已同步")

            # 统一推理模板 inference_parquet.py 也推送并挂载，
            # 保证远端训练产出与本地一致的完整 inference.py（而非简化 fallback）。
            template = self._resolve_inference_template()
            if template:
                await self._scp_push(
                    template, f"{self.work_dir}/templates/inference_parquet.py"
                )
                self._log(run_id, "[SYNC] inference_parquet.py 模板已同步")

            # 5. 远端启动执行（容器 或 免 Docker runtime python）
            label = f"qm-train-{run_id}"
            if is_process:
                code, out, err = await self._launch_process_job(run_id, label, direct_source=direct_source)
            else:
                self._log(run_id, "[SYSTEM] 在 AutoDL 启动训练容器...", progress=20)
                docker_cmd = self._build_docker_run_cmd(label, direct_source=direct_source)
                code, out, err = await self._ssh_exec(docker_cmd, timeout=120)
            if code != 0:
                raise RuntimeError(f"远端训练启动失败: {err or out}")
            self._log(run_id, f"[SYSTEM] 远端训练已启动: {label}", progress=22)

            # 6. 后台轮询训练进度
            REGISTRY.register(
                self._poll_remote(run_id, label) if not is_process
                else self._poll_process(run_id, label)
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("[%s] 远程训练编排失败: %s", run_id, exc, exc_info=True)
            self._log(run_id, f"[ERROR] 远程训练编排失败: {exc}", status="failed", progress=0)

    # ── 免 Docker 执行（executor=process：服务器节点包 runtime python） ──────────

    def _process_runtime(self) -> str:
        """远端 runtime 解释器（默认节点包 layout: {pack_root}/runtime/bin/python3）。"""
        if self.runtime_python:
            return self.runtime_python
        if not self.pack_root:
            raise RuntimeError("executor=process 节点必须配置 pack_root 或 runtime_python")
        return f"{self.pack_root}/runtime/bin/python3"

    def _process_env_file(self) -> str:
        """包内环境文件（train_env.sh，装载 QUANTDB_API_KEY / QM_*_DATA_DIR）。"""
        if self.env_file.startswith("/"):
            return self.env_file
        if not self.pack_root:
            return self.env_file
        return f"{self.pack_root}/{self.env_file}"

    async def _launch_process_job(self, run_id: str, label: str, *, direct_source: str) -> tuple[int, str, str]:
        """在远端以 runtime python 直跑 train.py（setsid 后台 + pid 文件 + 日志文件）。"""
        work = self.work_dir
        runtime = self._process_runtime()
        env_sh = self._process_env_file()
        py_path = f"PYTHONPATH={work}:{self.pack_root}" if self.pack_root else f"PYTHONPATH={work}"
        run_cmd = (
            f"mkdir -p {work} && cd {work} && "
            f"{{ [ -f {env_sh} ] && . {env_sh} || true; }} && "
            f"{py_path} TRAINING_WORKSPACE_DIR={work} "
            f"setsid {runtime} {work}/train.py --config {work}/config.yaml "
            f"> {work}/train.log 2>&1 < /dev/null & echo $! > {work}/train.pid"
        )
        self._log(run_id, "[SYSTEM] 在远端启动 runtime 训练(免 Docker)...", progress=20)
        return await self._ssh_exec(run_cmd, timeout=90)

    async def _poll_process(self, run_id: str, label: str) -> None:
        """轮询远端 runtime 训练(日志文件 + pid 存活)，完成后拉产物/注册。"""
        work = self.work_dir
        seen_lines: set[str] = set()
        progress = 22
        try:
            while True:
                code, out, err = await self._ssh_exec(
                    f"tail -n {self._LOG_TAIL_LINES} {work}/train.log 2>/dev/null",
                    timeout=120,
                )
                for line in (out + err).splitlines():
                    line = line.strip()
                    if not line or line in seen_lines:
                        continue
                    seen_lines.add(line)
                    progress = max(progress, LocalDockerProgress.infer(line, progress))
                    self._log(run_id, line, progress=progress)

                alive_code, alive_out, _ = await self._ssh_exec(
                    f"kill -0 $(cat {work}/train.pid 2>/dev/null) 2>/dev/null && echo A || echo G",
                    timeout=60,
                )
                if "A" in (alive_out or ""):
                    await asyncio.sleep(self._POLL_INTERVAL)
                    continue

                # 进程已结束：以 result.json 判定成败（与容器 exit_code=0 语义一致）
                has_code, has_out, _ = await self._ssh_exec(
                    f"test -f {work}/result.json && echo Y || echo N", timeout=60
                )
                self._log(run_id, "[SYSTEM] 远端训练进程已结束，处理产物...", status="waiting_callback", progress=95)
                await self._pull_artifacts(run_id)
                if "Y" in (has_out or ""):
                    self._log(run_id, "[SYSTEM] 模型产物已回传，等待模型注册...", progress=97)
                    await self._trigger_registration(run_id)
                else:
                    self._log(run_id, "[ERROR] 远端训练未产出 result.json，判定失败", status="failed", progress=0)
                await self._ssh_exec(f"rm -f {work}/train.pid 2>/dev/null || true", timeout=60)
                return
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error("[%s] 远端 runtime 轮询异常: %s", run_id, exc, exc_info=True)
            self._log(run_id, f"[ERROR] 远端 runtime 轮询异常: {exc}", status="failed", progress=progress)

    async def _poll_remote(self, run_id: str, container_name: str) -> None:
        """轮询远端容器日志，解析进度，完成后拉取产物。"""
        seen_lines: set[str] = set()
        progress = 22
        try:
            while True:
                code, out, err = await self._ssh_exec(
                    f"docker logs {container_name} --tail {self._LOG_TAIL_LINES} 2>&1",
                    timeout=120,
                )
                # 进度解析 + 日志去重推送
                for line in (out + err).splitlines():
                    line = line.strip()
                    if not line or line in seen_lines:
                        continue
                    seen_lines.add(line)
                    progress = max(progress, LocalDockerProgress.infer(line, progress))
                    self._log(run_id, line, progress=progress)

                # 检查容器状态
                code2, status_out, _ = await self._ssh_exec(
                    f"docker inspect -f '{{{{.State.Status}}}}' {container_name} 2>/dev/null || echo gone",
                    timeout=60,
                )
                status = (status_out or "").strip()
                if status in ("exited", "dead", "gone"):
                    # 拿退出码
                    code3, exit_out, _ = await self._ssh_exec(
                        f"docker inspect -f '{{{{.State.ExitCode}}}}' {container_name} 2>/dev/null || echo -1",
                        timeout=60,
                    )
                    exit_code = (exit_out or "").strip()
                    await self._handle_container_end(run_id, container_name, exit_code)
                    return

                await asyncio.sleep(self._POLL_INTERVAL)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error("[%s] 远程轮询异常: %s", run_id, exc, exc_info=True)
            self._log(run_id, f"[ERROR] 远程轮询异常: {exc}", status="failed", progress=progress)

    async def _handle_container_end(self, run_id: str, container_name: str, exit_code: str) -> None:
        """容器结束后：拉取产物 → 触发注册 → 清理远端。"""
        try:
            if exit_code == "0":
                self._log(run_id, "[SYSTEM] 训练完成，拉取模型产物...", status="waiting_callback", progress=95)
                await self._pull_artifacts(run_id)
                self._log(run_id, "[SYSTEM] 模型产物已回传，等待模型注册...", progress=97)
                # 清理远端容器
                await self._ssh_exec(f"docker rm -f {container_name} 2>/dev/null || true", timeout=60)
                # 触发本地模型注册（与本地流程一致）
                await self._trigger_registration(run_id)
            else:
                self._log(run_id, f"[ERROR] 训练容器异常退出 (exit={exit_code})", status="failed", progress=0)
                await self._ssh_exec(f"docker rm -f {container_name} 2>/dev/null || true", timeout=60)
        except Exception as exc:  # noqa: BLE001
            logger.error("[%s] 容器结束处理失败: %s", run_id, exc, exc_info=True)
            self._log(run_id, f"[ERROR] 容器结束处理失败: {exc}", status="failed", progress=0)

    async def _pull_artifacts(self, run_id: str) -> None:
        """拉取模型产物到本地工作目录 /data/training_jobs/{run_id}。

        用 rsync 整目录同步，包含全部产物（model.*、metadata、inference.py、
        pred.parquet/pred.pkl、result.json、shap_summary.csv 等）。
        """
        # 本地训练工作目录（与 LocalDockerOrchestrator 一致，注册流程从这里找产物）
        work_dir = Path("/data") / "training_jobs" / run_id
        work_dir.mkdir(parents=True, exist_ok=True)
        # rsync 整目录：远端 {work_dir}/ 同步到本地 work_dir/，含隐藏文件
        cmd = [
            "rsync", "-avz", "--partial",
            "-e", f"ssh -o StrictHostKeyChecking=no -p {self.port}"
            + (f" -i {self.ssh_key}" if self.ssh_key else ""),
        ]
        if self.ssh_password:
            cmd = [
                "rsync", "-avz", "--partial",
                "-e", f"sshpass -p {self.ssh_password} ssh -o StrictHostKeyChecking=no -p {self.port}",
            ]
        cmd += [f"{self.user}@{self.host}:{self.work_dir.rstrip('/')}/", f"{work_dir}/"]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.warning("[%s] rsync 拉取产物失败: %s", run_id, stderr.decode(errors="replace")[:300])
        self._log(run_id, f"[SYNC] 模型产物已拉取到 {work_dir}")

    async def _trigger_registration(self, run_id: str) -> None:
        """读取本地工作目录的 result.json，调用 complete_training_run 触发模型注册。

        复用现有注册流程（_sync_candidate_artifacts 从 /data/training_jobs/{run_id} 找产物），
        与本地训练完成后的回调路径一致。
        """
        import json

        from backend.services.api.routers.admin.admin_training_utils import complete_training_run

        work_dir = Path("/data") / "training_jobs" / run_id
        result = {}
        result_path = work_dir / "result.json"
        if result_path.exists():
            try:
                result = json.loads(result_path.read_text(encoding="utf-8"))
            except Exception as exc:  # noqa: BLE001
                logger.warning("[%s] result.json 解析失败: %s", run_id, exc)

        try:
            await complete_training_run(
                run_id=run_id,
                result=result,
                x_internal_call_secret=self.internal_secret,
            )
            self._log(run_id, "[SYSTEM] 模型注册流程已触发", progress=100)
        except Exception as exc:  # noqa: BLE001
            logger.error("[%s] 模型注册失败: %s", run_id, exc, exc_info=True)
            self._log(run_id, f"[ERROR] 模型注册失败: {exc}", status="failed")

    # ── 配置 / 工具 ─────────────────────────────────────────────────────────────

    def _build_config_yaml(self, run_id: str, payload: dict) -> dict:
        """生成训练配置（与本地 LocalDockerOrchestrator._build_config_yaml 结构一致）。

        简化：从 payload 直接构建最小可用配置，local_dir 由调用方覆盖为远端路径。
        """
        context = payload.get("context", {}) if isinstance(payload.get("context"), dict) else {}
        features = payload.get("features", []) or []

        config: dict[str, Any] = {
            "run_id": run_id,
            "job_name": payload.get("job_name", "unnamed"),
            "data": {
                "train_start": payload.get("train_start", "2022-01-01"),
                "train_end": payload.get("train_end", "2024-12-31"),
                "features": features,
                "source_mode": "LOCAL",
                "local_dir": "/tmp/feature_snapshots",
                "factor_source": str(payload.get("factor_source") or "") or None,
                "factor_catalog_version": str(payload.get("factor_catalog_version") or "") or None,
                "factor_schema_hash": str(payload.get("factor_schema_hash") or "") or None,
                "factor_field_sources": dict(payload.get("factor_field_sources") or {}),
                "factor_catalog_published_at": str(payload.get("factor_catalog_published_at") or "") or None,
                "factor_coverage": dict(payload.get("factor_coverage") or {}),
            },
            "model": {
                "type": payload.get("model_type", "lightgbm"),
                "types": payload.get("model_types"),
                "ensemble": payload.get("ensemble", "none"),
                "prediction_mode": payload.get("prediction_mode", "point"),
                "num_boost_round": payload.get("num_boost_round", 1000),
                "early_stopping_rounds": payload.get("early_stopping_rounds", 100),
                "val_ratio": payload.get("val_ratio", 0.15),
                "params": payload.get("lgb_params", {}),
                "xgb_params": payload.get("xgb_params", {}),
                "catboost_params": payload.get("catboost_params", {}),
                "dl_params": payload.get("dl_params", {}),
            },
            "label": {
                "target_horizon_days": payload.get("target_horizon_days", 1),
                "target_mode": payload.get("target_mode", "return"),
                "label_formula": payload.get("label_formula", ""),
            },
            "context": {
                "initial_capital": context.get("initial_capital", 1_000_000),
                "benchmark": context.get("benchmark", "SH000300"),
                "commission_rate": context.get("commission_rate", 0.00025),
                "slippage": context.get("slippage", 0.0005),
                "deal_price": context.get("deal_price", "close"),
                "market": context.get("market", "CN"),
                "industry_as_feature": context.get("industry_as_feature", False),
            },
            "explain": payload.get("explain", DEFAULT_EXPLAIN_CFG),
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
            "cache": {"dir": "/tmp"},
        }

        split_fields = ["valid_start", "valid_end", "test_start", "test_end"]
        if all(payload.get(k) for k in split_fields):
            config["split"] = {
                "train": [payload.get("train_start"), payload.get("train_end")],
                "valid": [payload.get("valid_start"), payload.get("valid_end")],
                "test": [payload.get("test_start"), payload.get("test_end")],
            }
            config["model"]["val_ratio"] = None

        if payload.get("wfa") and isinstance(payload.get("wfa"), dict):
            config["wfa"] = payload["wfa"]
        try:
            config["max_time_minutes"] = max(10, int(payload.get("max_time_minutes") or 120))
        except Exception:
            config["max_time_minutes"] = 120
        if isinstance(payload.get("factor_selection"), dict):
            config["factor_selection"] = payload["factor_selection"]
        # 特征截面预处理配置：与本地编排器保持一致，远程 GPU 训练同样生效
        pp_cfg = payload.get("preprocessing")
        if isinstance(pp_cfg, dict):
            config["preprocessing"] = pp_cfg
        elif str(payload.get("enable_cross_sectional_prep", "false")).lower() in ("1", "true", "yes", "on"):
            config["preprocessing"] = {"enabled": True, "winsor": True}
        return config

    def _resolve_feature_files(self, payload: dict) -> list[str]:
        """根据训练市场与区间解析需要推送的特征快照文件（容器内路径）。

        市场 → 文件：
          - A股（CN/a_share）: 按年份 model_features_YYYY.parquet
          - 其他市场: 单体 model_features_{market}.parquet
        """
        feature_dir = Path("/app/db/feature_snapshots")
        if not feature_dir.is_dir():
            feature_dir = Path("/data/feature_snapshots")
        if not feature_dir.is_dir():
            logger.warning("特征快照目录不存在: %s", feature_dir)
            return []

        # 解析市场（payload.context.market: CN/HK/US/CRYPTO/FUTURES 或 a_share 等）
        context = payload.get("context", {}) if isinstance(payload.get("context"), dict) else {}
        market_raw = str(context.get("market") or "CN").upper()
        market_key = {
            "CN": "a_share", "A": "a_share", "A_SHARE": "a_share",
            "HK": "hong_kong", "US": "us_stock",
            "CRYPTO": "crypto", "BC": "crypto",
            "FUTURES": "futures",
        }.get(market_raw, "a_share")

        # 非 A 股：单体文件
        if market_key != "a_share":
            from backend.services.api.routers.admin.model_management_utils import (
                _MARKET_SNAPSHOT_PARQUET,
            )

            parquet_name = _MARKET_SNAPSHOT_PARQUET.get(market_key)
            f = feature_dir / parquet_name if parquet_name else None
            if f and f.exists():
                return [str(f)]
            logger.warning("市场 %s 特征快照不存在: %s", market_raw, f)
            return []

        # A 股：按年份文件
        train_start = str(payload.get("train_start") or "2022-01-01")
        train_end = str(payload.get("train_end") or "2024-12-31")
        try:
            start_year = int(train_start[:4]) - 1  # 前一年用于标签
            end_year = int(train_end[:4])
        except (ValueError, TypeError):
            return []
        files = []
        for y in range(max(start_year, 2010), end_year + 1):
            f = feature_dir / f"model_features_{y}.parquet"
            if f.exists():
                files.append(str(f))
        return files

    def _callback_url(self, run_id: str) -> str:
        """构建训练回调 URL。

        优先用主节点局域网地址（TRAINING_MASTER_HOST），使远端容器能直接回调；
        否则回退 api_base（容器内服务名，远端可能不可达，主节点仍会自拉产物兜底）。
        """
        base = f"http://{self.master_host}:8000" if self.master_host else self.api_base
        return f"{base}/api/v1/models/training-runs/{run_id}/complete"

    def _build_docker_run_cmd(self, container_name: str, *, direct_source: str = "") -> str:
        """构造远端 docker run 命令字符串。

        train.py 与 inference 模板已 rsync 到工作目录并挂载覆盖镜像内置版，
        保证 train.py/模板更新不需要重新打包/上传 AutoDL 镜像。

        根据 TRAINING_AUTODL_GPUS 决定是否挂载 GPU：
          - all / 数字 → 加 --gpus（AutoDL 节点需装 nvidia-container-toolkit）
          - 空 / 0     → 不加（纯 CPU 训练）
        """
        gpus_flag = ""
        if self.gpus and self.gpus != "0":
            gpus_flag = f"--gpus \"{self.gpus}\" "
        # Direct jobs never mount feature_snapshots.  The configuration itself
        # remains the source of truth inside train.py.
        data_mount = (
            f"-v {self.quantdb_dir}:/tmp/quantdb:ro "
            if direct_source else
            f"-v {self.work_dir}/feature_snapshots:/tmp/feature_snapshots:ro "
        )
        # 与本地编排器一致：镜像 bake 的依赖可能落后于仓库（如 QuantDB 直读所需
        # 的 duckdb），启动前探测补齐；包已存在时探测跳过、零开销。
        _bootstrap_pkgs = _env_or("TRAINING_BOOTSTRAP_PIP", "duckdb pyqlib").split()
        bootstrap_cmd = " && ".join(
            f"python -c 'import importlib,sys; importlib.import_module(sys.argv[1])' {pkg} 2>/dev/null "
            f"|| python -m pip install -q --disable-pip-version-check {pkg} || exit 1"
            for pkg in _bootstrap_pkgs
        ) if _bootstrap_pkgs else "true"
        return (
            f"docker run -d --name {container_name} "
            f"{gpus_flag}"
            f"-v {self.work_dir}:/workspace "
            f"{data_mount}"
            f"-v {self.work_dir}/train.py:/app/train.py:ro "
            f"-v {self.work_dir}/preprocessing.py:/app/preprocessing.py:ro "
            f"-v {self.work_dir}/parallel_utils.py:/app/parallel_utils.py:ro "
            f"-v {self.work_dir}/templates:/app/backend/services/engine/inference/templates:ro "
            + (f"-v {self.work_dir}/modules/quantdb_factor_reader.py:/app/backend/services/engine/data_platform/quantdb_factor_reader.py:ro " if direct_source else "")
            + (f"-v {self.work_dir}/modules/quantdb_hub.py:/app/backend/services/engine/data_platform/quantdb_hub.py:ro " if direct_source else "")
            + f"--entrypoint sh {self.docker_image} -c \"{bootstrap_cmd} && exec python /app/train.py --config /workspace/config.yaml\""
        )

    def _resolve_train_script(self) -> str | None:
        """定位本地 train.py 训练脚本路径（优先项目目录，回退容器内路径）。"""
        candidates = [
            str(Path(__file__).resolve().parents[3] / "docker" / "training" / "train.py"),
            "/app/docker/training/train.py",
            "/app/train.py",
        ]
        for p in candidates:
            if Path(p).exists():
                return p
        return None

    def _resolve_preprocessing_script(self) -> str | None:
        """定位本地 preprocessing.py（train.py 顶层 import 的纯函数集）。"""
        candidates = [
            str(Path(__file__).resolve().parents[3] / "docker" / "training" / "preprocessing.py"),
            "/app/docker/training/preprocessing.py",
            "/app/preprocessing.py",
        ]
        for p in candidates:
            if Path(p).exists():
                return p
        return None

    def _resolve_quantdb_factor_reader(self) -> str | None:
        candidates = [
            str(Path(__file__).resolve().parents[2] / "data_platform" / "quantdb_factor_reader.py"),
            "/app/backend/services/engine/data_platform/quantdb_factor_reader.py",
        ]
        return next((path for path in candidates if Path(path).is_file()), None)

    def _resolve_quantdb_hub(self) -> str | None:
        candidates = [
            str(Path(__file__).resolve().parents[2] / "data_platform" / "quantdb_hub.py"),
            "/app/backend/services/engine/data_platform/quantdb_hub.py",
        ]
        return next((path for path in candidates if Path(path).is_file()), None)

    def _resolve_parallel_utils_script(self) -> str | None:
        """定位本地 parallel_utils.py（多核因子筛选，train.py 顶层 import）。"""
        candidates = [
            str(Path(__file__).resolve().parents[3] / "docker" / "training" / "parallel_utils.py"),
            "/app/docker/training/parallel_utils.py",
            "/app/parallel_utils.py",
        ]
        for p in candidates:
            if Path(p).exists():
                return p
        return None

    def _resolve_inference_template(self) -> str | None:
        """定位本地统一推理模板 inference_parquet.py。"""
        candidates = [
            str(Path(__file__).resolve().parents[3]
                / "backend" / "services" / "engine" / "inference" / "templates" / "inference_parquet.py"),
            "/app/backend/services/engine/inference/templates/inference_parquet.py",
        ]
        for p in candidates:
            if Path(p).exists():
                return p
        return None

    def _log(self, run_id: str, line: str, *, status: str | None = None, progress: int | None = None) -> None:
        try:
            self.log_stream.append_log(
                run_id=run_id,
                tenant_id=self._tenant_id,
                user_id=self._user_id,
                line=line,
                status=status,
                progress=progress,
            )
        except Exception:  # noqa: BLE001
            logger.warning("append_log failed for %s: %s", run_id, line)


class LocalDockerProgress:
    """复用 LocalDockerOrchestrator 的日志进度解析逻辑。"""

    @staticmethod
    def infer(line: str, current: int) -> int:
        from backend.services.engine.training.local_docker_orchestrator import (
            LocalDockerOrchestrator,
        )

        return LocalDockerOrchestrator._infer_progress_from_log_line(line, current)
