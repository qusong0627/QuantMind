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
import json
import logging
import os
import shlex
import tempfile
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from backend.services.engine.training.orchestrator_base import TrainingOrchestrator, REGISTRY
from backend.services.engine.training.pool_binding import resolve_training_pool
from backend.services.engine.training.training_log_stream import TrainingRunLogStream
from backend.services.api.training_explain import DEFAULT_EXPLAIN_CFG

logger = logging.getLogger(__name__)


def _env_or(key: str, default: str) -> str:
    return (os.getenv(key) or default).strip()


def _derive_absolute_split(payload: dict) -> dict[str, list[str]] | None:
    """兜底（同步）：探针不可用时，用中心 QuantDB 的交易日序列算切分。

    正常路径是 ``window_probe.probe_data_window()`` 异步探针（本地或远程节点），
    本函数只服务于无法 await 的同步构建路径，且窗口取自中心数据 —— 与节点实际
    日期集合可能有偏差，调用方应记录告警。
    """
    from backend.services.engine.training import window_probe as wp

    source = str(payload.get("factor_source") or "").strip()
    if not source or not wp.window_span(payload):
        return None
    context = payload.get("context") if isinstance(payload.get("context"), dict) else {}
    market = str(context.get("market") or "CN").upper()
    try:
        window = wp.probe_local_window(source, market)
    except Exception as exc:  # noqa: BLE001
        logger.warning("derive absolute split failed, fall back to val_ratio: %s", exc)
        return None
    split = wp.build_split_from_window(window, payload)
    if split:
        logger.warning(
            "split derived from CENTER calendar (node probe unavailable): %s", split
        )
    return split


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

    _POLL_INTERVAL = 5  # 远端日志/进程探测间隔（秒）
    _LOG_TAIL_LINES = 120
    _HEARTBEAT_SEC = 20
    _SSH_RETRY_LIMIT = 36  # 5s 间隔约 3 分钟；指数等待时更长

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
            # 执行模式：ssh_docker（默认，走 docker run）或 native_python（免 docker 直跑 train.py）
            self.exec_mode = str(node_config.get("exec_mode") or "ssh_docker").strip()
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
            self.exec_mode = _env_or("TRAINING_AUTODL_EXEC_MODE", "ssh_docker").strip()
        # 远端数据已由魔搭（ModelScope）数据集初始化时，跳过每次训练的增量同步。
        # 节点配置 skip_data_sync: true 或 TRAINING_AUTODL_SKIP_DATA_SYNC=1
        raw_skip = (node_config or {}).get("skip_data_sync")
        if raw_skip is None:
            raw_skip = _env_or("TRAINING_AUTODL_SKIP_DATA_SYNC", "")
        self.skip_data_sync = str(raw_skip).strip().lower() in ("1", "true", "yes", "on")
        # 免 docker 模式：原生 Python 解释器路径（AutoDL 容器为 /root/miniconda3/bin/python）
        self.native_python = _env_or("TRAINING_AUTODL_PYTHON", "/root/miniconda3/bin/python").strip()
        self.api_base = _env_or("QUANTMIND_API_BASE_URL", "http://quantmind-api:8000")
        # 主节点局域网地址（供远端容器回调）；为空则回退 api_base（可能不可达）
        self.master_host = _env_or("TRAINING_MASTER_HOST", "")
        self.internal_secret = _env_or("INTERNAL_CALL_SECRET", "")
        self.log_stream = TrainingRunLogStream()
        # 探针读到的节点数据窗口（window_probe.DataWindow），launch 时填充
        self._window: Any | None = None
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
            "-n",
            "-T",
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
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            raise
        return proc.returncode or 0, stdout.decode(errors="replace"), stderr.decode(errors="replace")

    _SSH_TRANSIENT_MARKERS = (
        "connection refused",
        "connection timed out",
        "connection reset",
        "no route to host",
        "broken pipe",
        "connection closed",
        "kex_exchange_identification",
        "banner exchange",
        "network is unreachable",
        "temporarily unavailable",
    )

    def _ssh_probe_failed(self, code: int, out: str, err: str) -> str:
        """SSH 本身失败时返回原因；探测成功（拿到 ===META===）返回空串。"""
        blob = f"{out}\n{err}"
        if "===META===" in blob:
            return ""
        low = blob.lower()
        if any(m in low for m in self._SSH_TRANSIENT_MARKERS) or "ssh:" in low:
            return (err or out or "ssh failed").strip().splitlines()[-1][:240]
        if code != 0:
            return (err or out or f"ssh exit {code}").strip().splitlines()[-1][:240]
        return "empty probe (no META)"

    async def _ssh_exec_streaming(
        self,
        cmd: str,
        *,
        timeout: int = 900,
        on_line: Callable[[str], None] | None = None,
        heartbeat_sec: float = 20.0,
        heartbeat_fn: Callable[[int], None] | None = None,
    ) -> tuple[int, str, str]:
        """SSH 执行并把 stdout/stderr 逐行回调；静默过久则打心跳。"""
        proc = await asyncio.create_subprocess_exec(
            *self._ssh_base_args(),
            cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []
        last_out = time.monotonic()
        started = last_out

        async def _pump(stream: asyncio.StreamReader | None, buf: list[str]) -> None:
            nonlocal last_out
            if stream is None:
                return
            while True:
                raw = await stream.readline()
                if not raw:
                    break
                text = raw.decode(errors="replace").rstrip("\r\n")
                buf.append(text)
                last_out = time.monotonic()
                shown = text.strip()
                if on_line and shown:
                    on_line(shown[:500])

        async def _heartbeat() -> None:
            while proc.returncode is None:
                await asyncio.sleep(max(5.0, heartbeat_sec))
                if proc.returncode is not None:
                    return
                if heartbeat_fn and (time.monotonic() - last_out) >= heartbeat_sec:
                    heartbeat_fn(int(time.monotonic() - started))

        hb_task = asyncio.create_task(_heartbeat())
        try:
            await asyncio.wait_for(
                asyncio.gather(
                    _pump(proc.stdout, stdout_parts),
                    _pump(proc.stderr, stderr_parts),
                    proc.wait(),
                ),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise
        finally:
            hb_task.cancel()
            try:
                await hb_task
            except (asyncio.CancelledError, Exception):
                pass
        return (
            proc.returncode or 0,
            "\n".join(stdout_parts),
            "\n".join(stderr_parts),
        )

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
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(
                f"rsync push failed ({proc.returncode}): "
                f"{(stderr or stdout).decode(errors='replace')[:500]}"
            )

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
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(
                f"scp push failed ({proc.returncode}): "
                f"{stderr.decode(errors='replace')[:500]}"
            )


    async def test_connection(self) -> dict:
        """测试 SSH 连接；native_python 检查 Python/GPU，ssh_docker 检查 docker。"""
        results = {"host": self.host, "exec_mode": self.exec_mode}
        if self.exec_mode == "native_python":
            python = self.native_python or "/root/miniconda3/bin/python"
            code, out, err = await self._ssh_exec(
                f"echo OK && {python} --version && "
                f"(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1 || echo no-gpu)"
            )
            results["ssh"] = code == 0 and "OK" in out
            results["docker"] = False
            results["native_python"] = code == 0
            results["detail"] = (out + err).strip()
            if code != 0:
                results["error"] = (err or out).strip()
            return results
        code, out, err = await self._ssh_exec("echo OK && docker --version 2>&1 | head -1")
        results["ssh"] = code == 0 and "OK" in out
        results["docker"] = code == 0 and "Docker" in (out + err)
        if code == 0:
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
            # ── 数据探针（时间切分的唯一来源）────────────────────────────────
            # 切分不再依赖后端因子目录的草稿/发布状态：直接探针读取本节点
            # （远程 AutoDL）自己的 QuantDB 窗口与交易日序列，本地/远端各自自洽。
            probe_source = str(payload.get("factor_source") or "").strip()
            _ctx = payload.get("context") if isinstance(payload.get("context"), dict) else {}
            probe_market = str(_ctx.get("market") or "CN").upper()
            if probe_source:
                from backend.services.engine.training import window_probe as wp

                try:
                    self._window = await wp.probe_data_window(
                        self.node_id, probe_source, market=probe_market
                    )
                    self._log(
                        run_id,
                        f"[PROBE] 节点数据窗口 {self._window.min_date}~{self._window.max_date}"
                        f"（{len(self._window.trading_dates)} 个交易日，"
                        f"{len(self._window.columns)} 列）",
                        progress=6,
                    )
                except Exception as exc:  # noqa: BLE001
                    self._window = None
                    logger.warning("[%s] 节点数据探针失败: %s", run_id, exc)
                    self._log(
                        run_id,
                        f"[PROBE] 节点数据探针失败，退回中心日历兜底: {exc}",
                        progress=6,
                    )

            config = self._build_config_yaml(run_id, payload)
            direct_source = str(config["data"].get("factor_source") or "")
            # 远程节点（AutoDL）目前仅支持 A 股 QuantDB 直读：非 CN 市场的
            # 6_ml_datasets 数据不在同步清单内，硬走会把本地 CN 目录误当
            # 目标市场数据源（静默用错数据）。显式拒绝而非兜底。
            market = str((config.get("context") or {}).get("market") or "CN").upper()
            if direct_source and market != "CN":
                raise RuntimeError(
                    f"远程节点暂不支持 {market} 市场 QuantDB 直读训练，"
                    "请选择本地 Docker 节点，或取消数据源直读（快照路径）"
                )
            config["data"]["local_dir"] = "/tmp/quantdb" if direct_source else "/tmp/feature_snapshots"
            if direct_source:
                # docker 模式容器内挂载 quantdb_dir->/tmp/quantdb；native 模式直读 self.quantdb_dir
                config["data"]["quantdb_dir"] = (
                    self.quantdb_dir if self.exec_mode == "native_python" else "/tmp/quantdb"
                )
            config["callback"]["url"] = self._callback_url(run_id)

            # 2. 确保远端工作目录结构
            await self._ssh_exec(
                f"mkdir -p {self.work_dir}/feature_snapshots "
                f"{self.work_dir}/templates {self.work_dir}/modules"
            )

            # 3. Only direct source data is synced on AutoDL.  The node owns
            # /data/quantdb and its SDK state, so no raw parquet is copied from
            # the coordinator.  Operators may override the command for a custom
            # SDK installation with TRAINING_AUTODL_QUANTDB_SYNC_CMD.
            if direct_source:
                if self.exec_mode == "native_python":
                    # 免 docker：同步脚本要 import backend.shared.runtime_secrets，
                    # 必须先推 backend_min，再跑 quantdb_daily_sync.py。
                    await self._deploy_native_backend(run_id)
                    await self._ensure_native_sync_files()
                    sync_python = self.native_python or "/root/miniconda3/bin/python"
                    sync_script = f"{self.work_dir}/modules/quantdb_daily_sync.py"
                    # 注入 QuantDB API key（主容器 env / runtime.env 解析），节点免预配置
                    qdb_key = self._resolve_quantdb_api_key()
                    qdb_key_env = f"QUANTDB_API_KEY={shlex.quote(qdb_key)} " if qdb_key else ""
                    sync_cmd = (
                        f"PYTHONPATH={self.work_dir}:{self.work_dir}/backend_min "
                        f"PYTHONUNBUFFERED=1 {qdb_key_env}{sync_python} -u {sync_script}"
                    )
                    # native 直读裁剪到近 3 年（避免每次全量下载 2016 至今的历史分区）。
                    # 默认近 3 年；TRAINING_AUTODL_QUANTDB_SINCE 可给 "YYYY-MM-DD" 或 "N-year"，
                    # 设 "0"/"none"/"full" 则禁掉 since 走全量。
                    sync_since = _env_or("TRAINING_AUTODL_QUANTDB_SINCE", "3-year")
                    if sync_since and sync_since.lower() not in ("0", "none", "full", "off"):
                        low = sync_since.lower()
                        if low == "3-year" or low == "3-years":
                            from datetime import date, timedelta

                            sync_since = str(date.today() - timedelta(days=365 * 3))
                        elif low.endswith("year") or low.endswith("years"):
                            n = 0
                            try:
                                n = int(low.split("-")[0] or low.split(" ")[0])
                            except ValueError:
                                n = 3
                            from datetime import date, timedelta

                            sync_since = str(date.today() - timedelta(days=365 * n))
                        sync_cmd += f" --since {sync_since}"
                else:
                    sync_cmd = _env_or(
                        "TRAINING_AUTODL_QUANTDB_SYNC_CMD",
                        "python /app/backend/scripts/quantdb_daily_sync.py",
                    )
                quoted_dir = shlex.quote(self.quantdb_dir)
                if self.skip_data_sync:
                    # 远端数据由魔搭（ModelScope）数据集独立初始化，不再增量同步。
                    # 此时中心 pin 的 factor_coverage 就是训练区间的权威边界，
                    # 远端数据覆盖不足会在 loading 阶段 fail fast（见 loading.py）。
                    self._log(
                        run_id,
                        f"[SYNC] 跳过远端数据同步（节点自带数据）: {quoted_dir}",
                        progress=15,
                    )
                else:
                    # 只同步训练实际请求的因子源（factor_source），避免每次把 l2/l1_l2 等
                    # 无关数据集全量拉取（几 GB、拖慢冒烟/训练启动）。
                    sync_datasets = direct_source or "l1_factors"
                    since_note = f"，since={sync_since}" if "sync_since" in locals() and sync_since else ""
                    self._log(
                        run_id,
                        f"[SYNC] 开始增量同步 QuantDB {sync_datasets}{since_note}（过程日志会持续刷新）...",
                        progress=8,
                    )

                    def _on_sync_line(text: str) -> None:
                        self._log(run_id, f"[SYNC] {text}", progress=10)

                    def _on_sync_heartbeat(elapsed: int) -> None:
                        self._log(
                            run_id,
                            f"[SYNC] QuantDB {sync_datasets} 仍在同步{since_note}… 已等待 {elapsed}s",
                            progress=10,
                        )

                    code, out, err = await self._ssh_exec_streaming(
                        f"mkdir -p {quoted_dir} && QM_QUANTDB_DATA_DIR={quoted_dir} "
                        f"{sync_cmd} --parquet-only --datasets {sync_datasets}",
                        timeout=1800,
                        on_line=_on_sync_line,
                        heartbeat_sec=self._HEARTBEAT_SEC,
                        heartbeat_fn=_on_sync_heartbeat,
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
            # model_trainers/ 包（注册表/训练器拆包）与 train.py 同目录顶层 import，需一并推送
            trainers_dir = self._resolve_trainers_dir()
            if trainers_dir:
                await self._rsync_push(trainers_dir, f"{self.work_dir}/model_trainers/", is_dir=True)
                self._log(run_id, "[SYNC] model_trainers/ 已同步")
            # diagnostics/ 包（漂移/SHAP/小工具拆包）与 train.py 同目录顶层 import，需一并推送
            diagnostics_dir = self._resolve_diagnostics_dir()
            if diagnostics_dir:
                await self._rsync_push(diagnostics_dir, f"{self.work_dir}/diagnostics/", is_dir=True)
                self._log(run_id, "[SYNC] diagnostics/ 已同步")
            # data/ 包（数据加载/切分/筛选拆包）与 train.py 同目录顶层 import，需一并推送
            data_dir = self._resolve_data_dir()
            if data_dir:
                await self._rsync_push(data_dir, f"{self.work_dir}/data/", is_dir=True)
                self._log(run_id, "[SYNC] data/ 已同步")
            if direct_source:
                for module in (self._resolve_quantdb_factor_reader(), self._resolve_quantdb_hub()):
                    if module:
                        await self._rsync_push(module, f"{self.work_dir}/modules/")
                self._log(run_id, "[SYNC] QuantDB 直读 Reader 已同步")

            # 统一推理模板 inference_parquet.py 也推送并挂载，
            # 保证远端训练产出与本地一致的完整 inference.py（而非简化 fallback）。
            template = self._resolve_inference_template()
            if template:
                await self._scp_push(
                    template, f"{self.work_dir}/templates/inference_parquet.py"
                )
                self._log(run_id, "[SYNC] inference_parquet.py 模板已同步")

            # 5. 远端启动训练（按执行模式：docker run 或原生 Python 直跑）
            if self.exec_mode == "native_python":
                self._log(run_id, "[SYSTEM] 在 AutoDL 启动原生训练进程（免 Docker）...", progress=20)
                run_key, log_path = await self._launch_native_train(run_id, config)
                self._log(
                    run_id,
                    f"[SYSTEM] 训练进程已启动 (pid={run_key}, log={log_path})",
                    status="running",
                    progress=22,
                )
            else:
                self._log(run_id, "[SYSTEM] 在 AutoDL 启动训练容器...", progress=20)
                container_name = f"qm-train-{run_id}"
                docker_cmd = self._build_docker_run_cmd(container_name, direct_source=direct_source)
                code, out, err = await self._ssh_exec(docker_cmd, timeout=120)
                if code != 0:
                    raise RuntimeError(f"远端 docker run 失败: {err or out}")
                run_key = container_name
                container_id = (out or "").strip()[:12]
                self._log(run_id, f"[SYSTEM] 训练容器已启动: {container_name} ({container_id})", progress=22)

            # 6. 后台轮询训练进度（native 与 docker 共用，内部按 exec_mode 区分日志/状态取法）
            REGISTRY.register(
                self._poll_remote(run_id, run_key),
                run_id=run_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("[%s] 远程训练编排失败: %s", run_id, exc, exc_info=True)
            self._log(
                run_id,
                f"[ERROR] 远程训练编排失败: {str(exc).strip() or type(exc).__name__}",
                status="failed",
                progress=0,
            )

    async def _ensure_native_sync_files(self) -> None:
        """保证免 docker 直读同步脚本就位（quantdb_daily_sync.py + quantdb-sdk）。

        quantdb_daily_sync.py 依赖 quantdb_sdk（脚本 lazy import）与
        backend.shared.runtime_secrets（经 PYTHONPATH=backend_min 解析）。
        只需推单文件 + 确保 sdk 已装节点。
        """
        sync_script = self._resolve_quantdb_sync_script()
        if sync_script:
            await self._scp_push(sync_script, f"{self.work_dir}/modules/quantdb_daily_sync.py")
        # 幂等确保 quantdb-sdk 已装（已装即跳过）
        python = self.native_python or "/root/miniconda3/bin/python"
        probe = (
            f"{python} -c 'import quantdb_sdk, sys; sys.exit(0)' 2>/dev/null "
            f"|| {python} -m pip install -q quantdb-sdk"
        )
        await self._ssh_exec(probe, timeout=600)

    def _resolve_quantdb_sync_script(self) -> str | None:
        """定位本地 quantdb_daily_sync.py 同步脚本路径。"""
        candidates = [
            str(Path(__file__).resolve().parents[4] / "backend" / "scripts" / "quantdb_daily_sync.py"),
            "/app/backend/scripts/quantdb_daily_sync.py",
        ]
        return next((path for path in candidates if Path(path).is_file()), None)

    def _resolve_quantdb_api_key(self) -> str:
        """解析 QuantDB API key（供 native 直读数据同步注入节点）。

        优先真实环境变量 QUANTDB_API_KEY；否则读 config/runtime.env
        （与主容器 daily sync 的 runtime_secrets 一致），都没有时返回空串
        （同步将因缺 key 失败并在日志明示）。
        """
        key = (os.getenv("QUANTDB_API_KEY") or "").strip()
        if key:
            return key
        try:
            from backend.shared.runtime_secrets import get_secret

            return str(get_secret("QUANTDB_API_KEY") or "").strip()
        except Exception:  # noqa: BLE001
            return ""

    async def _launch_native_train(self, run_id: str, config: dict) -> tuple[str, str]:
        """启动免 docker 原生训练进程（AutoDL 容器内 python train.py）。

        返回 (pid, 日志路径)。假定 train.py 及依赖包已 rsync 到 work_dir；
        数据直读所需 backend 子树在 dist 模式下已由 _deploy_native_backend 推送。
        """
        # native 直读需要 loading.py 的 backend...quantdb_factor_reader 可 import
        await self._deploy_native_backend(run_id)

        # PYTHONPATH 需同时在 work_dir（训练包）与 backend_min（backend.* 子树）上
        py_path = f"{self.work_dir}:{self.work_dir}/backend_min"
        python = self.native_python or "/root/miniconda3/bin/python"
        log_path = f"{self.work_dir}/train_{run_id}.log"
        pid_file = f"{self.work_dir}/train_{run_id}.pid"
        exit_mark = f"{self.work_dir}/train_{run_id}.exit"
        # GPU env：native 用 CUDA_VISIBLE_DEVICES；all/default 不设（用全部）
        gpu_env = ""
        if self.gpus and self.gpus not in ("all", "0", ""):
            gpu_env = f"CUDA_VISIBLE_DEVICES={self.gpus} "
        inner = (
            f"PYTHONUNBUFFERED=1 QM_TRAIN_WORKSPACE={shlex.quote(self.work_dir)} "
            f"PYTHONPATH={py_path} {python} -u "
            f"{shlex.quote(self.work_dir)}/train.py "
            f"--config {shlex.quote(self.work_dir)}/config.yaml "
            f">{shlex.quote(log_path)} 2>&1; "
            f"echo $? > {shlex.quote(exit_mark)}"
        )
        cmd = (
            f"cd {shlex.quote(self.work_dir)} && rm -f {shlex.quote(exit_mark)} && "
            f"{gpu_env}setsid bash -c {shlex.quote(inner)} </dev/null >/dev/null 2>&1 & "
            f"echo $! | tee {shlex.quote(pid_file)}"
        )
        try:
            code, out, err = await self._ssh_exec(cmd, timeout=30)
        except asyncio.TimeoutError:
            # 进程可能已 nohup 起来，SSH 会话却没立刻退出；改读 pid 文件。
            code, out, err = await self._ssh_exec(
                f"cat {shlex.quote(pid_file)} 2>/dev/null || true", timeout=15
            )
        if code != 0:
            raise RuntimeError(f"远端原生训练启动失败: {err or out}")
        pid = next((ln.strip() for ln in (out or "").splitlines() if ln.strip().isdigit()), "")
        if not pid:
            raise RuntimeError(f"远端原生训练未返回 pid: {err or out}")
        return pid, log_path

    async def _deploy_native_backend(self, run_id: str, *, log: bool = True) -> None:
        """把免 docker 直读所需的 backend 最小子树 rsync 到远端 {work_dir}/backend_min/。

        仅训练数据路径上硬性 import 的一小撮文件（含 reader/hub 及其 import 链），
        loading.py:149 `from backend.services.engine.data_platform.quantdb_factor_reader import ...`
        依赖它。docker 模式由镜像内置 backend，无需此步。
        """
        # __file__ = <repo>/backend/services/engine/training/remote_ssh_orchestrator.py
        # parents: [0]training [1]engine [2]services [3]backend [4]<repo>
        repo_root = Path(__file__).resolve().parents[4]
        backend_root = repo_root / "backend"
        # 需保留相对 backend/ 的路径供 PYTHONPATH=backend_min 下 import backend.xxx
        # 依赖闭包（递归追踪）：stock_utils + stock_pool(builtins->constants) + runtime_secrets
        req_entries = [
            "shared/stock_utils.py",
            "shared/runtime_secrets.py",
            "shared/training/schemas.py",
            "shared/stock_pool",  # dir: builtins.py + constants.py（相对 import 自包含）
            "services/engine/data_platform/quantdb_factor_reader.py",
            "services/engine/data_platform/quantdb_hub.py",
        ]
        # 复制到本地临时目录再 rsync（保持 backend 包结构）
        import tempfile
        import shutil

        tmpdir = Path(tempfile.mkdtemp(prefix="qm-native-backend-"))
        dest_root = tmpdir / "backend"
        for rel in req_entries:
            src = backend_root / rel
            if src.is_dir():
                dst = dest_root / rel
                shutil.copytree(src, dst, dirs_exist_ok=True)
            elif src.exists():
                dst = dest_root / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
            else:
                logger.warning("native 后端缺文件（跳过）: %s", src)
                continue
        # 包骨架 __init__.py：必须为空内容，避免触发父包（如 data_platform/__init__.py
        # 会 from ...base import ...）去 import 未随子树推送的 sibling 模块。
        for pkg in ["backend", "backend/shared", "backend/shared/training",
                    "backend/shared/stock_pool",
                    "backend/services", "backend/services/engine",
                    "backend/services/engine/data_platform"]:
            init_file = dest_root.parent / pkg / "__init__.py"
            init_file.parent.mkdir(parents=True, exist_ok=True)
            init_file.write_text("", encoding="utf-8")
        try:
            await self._rsync_push(str(dest_root.parent), f"{self.work_dir}/backend_min/", is_dir=True)
            if log:
                self._log(run_id, f"[SYNC] backend 直读子树已同步到 {self.work_dir}/backend_min")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    # ── 数据探针（时间切分的唯一数据来源）─────────────────────────────────────

    async def probe_data_profile(self, source: str, *, market: str = "CN") -> dict[str, Any]:
        """只读探测节点数据画像：交易日序列 / 因子列 / schema_hash / 覆盖区间。

        远端（魔搭数据集）与中心的交易日集合并不一致（存在整段缺失），时间切分
        必须以节点自己的交易日序列为基准，因此要把整份日期列表读回来。本方法
        只做 SSH 读取，**绝不触发数据同步**；缓存由
        ``window_probe.probe_data_window()`` 统一负责。
        """
        await self._deploy_native_backend("probe", log=False)
        python = self.native_python or "/root/miniconda3/bin/python"
        remote_code = "\n".join(
            [
                "import json, sys",
                f'sys.path.insert(0, "{self.work_dir}/backend_min")',
                "from backend.services.engine.data_platform.quantdb_factor_reader "
                "import QuantDBFactorReader",
                f'r = QuantDBFactorReader("{self.quantdb_dir}", market="{market}")',
                f'st = r.describe("{source}")',
                f'dates = r.available_dates("{source}")',
                f'cols = sorted(r.factor_columns("{source}"))',
                'print("QM_PROFILE=" + json.dumps({"ready": bool(st.ready), '
                '"min_date": st.min_date, "max_date": st.max_date, '
                '"schema_hash": st.schema_hash, "columns": cols, '
                '"trading_dates": dates, "reason": st.reason}))',
            ]
        )
        cmd = (
            f"PYTHONUNBUFFERED=1 PYTHONPATH={self.work_dir}:{self.work_dir}/backend_min "
            f"{python} - <<'QM_PROFILE_EOF'\n{remote_code}\nQM_PROFILE_EOF"
        )
        code, out, err = await self._ssh_exec(cmd, timeout=300)
        profile = self._parse_profile(out)
        if profile is None:
            lines = [ln for ln in (err or out).strip().splitlines() if ln.strip()]
            raise RuntimeError(
                f"节点数据探针失败（{self.node_id}/{source}）: "
                f"{lines[-1][:240] if lines else f'ssh exit {code}'}"
            )
        profile.update(
            {
                "node_id": self.node_id,
                "source": source,
                "market": market,
                "probed_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        return profile

    @staticmethod
    def _parse_profile(out: str) -> dict[str, Any] | None:
        for line in (out or "").splitlines():
            if line.startswith("QM_PROFILE="):
                try:
                    data = json.loads(line[len("QM_PROFILE=") :])
                except Exception:  # noqa: BLE001
                    return None
                return data if isinstance(data, dict) else None
        return None

    async def _poll_remote(
        self, run_id: str, container_name: str, *, skip_existing_log: bool = False
    ) -> None:
        """轮询远端训练（容器或原生进程）日志，解析进度，完成后拉取产物。

        native_python 模式下 run_key 为 （pid 或 "native-{run_id}"/日志文件路径），
        通过读写远端日志文件与进程存活探测替代 docker logs / docker inspect。

        ``skip_existing_log=True``：重挂轮询（后端重启对账）时跳过远端已有日志行，
        避免前端日志流里出现重复内容。
        """
        if self.exec_mode == "native_python":
            await self._poll_native_process(run_id, skip_existing_log=skip_existing_log)
            return
        seen_lines: set[str] = set()
        progress = 22
        try:
            while True:
                if self.log_stream.is_cancel_requested(run_id):
                    await self._cancel_remote(run_id, container_name)
                    return
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
                    self._log(run_id, line, status="running", progress=progress)

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

    async def _poll_native_process(
        self, run_id: str, *, skip_existing_log: bool = False
    ) -> None:
        """轮询免 docker 直跑的原生训练进程（一次 SSH 取日志+存活，静默时心跳）。"""
        log_path = f"{self.work_dir}/train_{run_id}.log"
        pid_file = f"{self.work_dir}/train_{run_id}.pid"
        exit_mark = f"{self.work_dir}/train_{run_id}.exit"
        seen_lines: set[str] = set()
        progress = 22
        silent_rounds = 0
        last_n = 0
        ssh_fails = 0
        missing_pid_rounds = 0
        last_pid = ""
        # 重挂轮询时先跳过远端已有日志（首轮只对齐行号，不回放旧内容）
        emit_log = not skip_existing_log
        try:
            while True:
                if self.log_stream.is_cancel_requested(run_id):
                    await self._cancel_remote(run_id, f"native-{run_id}")
                    return
                start = last_n + 1
                probe = (
                    f"n=$(wc -l < {shlex.quote(log_path)} 2>/dev/null || echo 0); "
                    f"echo '===LOG==='; "
                    f"if [ \"$n\" -ge {start} ]; then sed -n '{start},$p' {shlex.quote(log_path)}; fi; "
                    f"echo '===META==='; "
                    f"echo COUNT:$n; "
                    f"pid=$(cat {shlex.quote(pid_file)} 2>/dev/null || true); echo PID:$pid; "
                    f"if [ -n \"$pid\" ] && kill -0 \"$pid\" 2>/dev/null; then echo ALIVE:1; else echo ALIVE:0; fi; "
                    f"echo EXIT:$(cat {shlex.quote(exit_mark)} 2>/dev/null || true)"
                )
                code, out, err = await self._ssh_exec(probe, timeout=120)
                ssh_err = self._ssh_probe_failed(code, out, err)
                if ssh_err:
                    ssh_fails += 1
                    wait = min(60, self._POLL_INTERVAL * min(ssh_fails, 8))
                    self._log(
                        run_id,
                        f"[SYSTEM] AutoDL SSH 暂时不可达，{wait}s 后重试（{ssh_fails}/{self._SSH_RETRY_LIMIT}）：{ssh_err}",
                        status="running",
                        progress=progress,
                    )
                    if ssh_fails >= self._SSH_RETRY_LIMIT:
                        self._log(
                            run_id,
                            f"[ERROR] AutoDL SSH 连续失败 {ssh_fails} 次，停止轮询（训练进程可能仍在节点上）",
                            status="failed",
                            progress=progress,
                        )
                        return
                    await asyncio.sleep(wait)
                    continue
                ssh_fails = 0

                blob = out + err
                log_blob, _, meta_blob = blob.partition("===META===")
                log_blob = log_blob.replace("===LOG===", "")
                got_new = False
                if emit_log:
                    for line in log_blob.splitlines():
                        line = line.strip()
                        if not line or line in seen_lines:
                            continue
                        if line.lower().startswith("ssh:"):
                            continue
                        seen_lines.add(line)
                        got_new = True
                        progress = max(progress, LocalDockerProgress.infer(line, progress))
                        self._log(run_id, line, status="running", progress=progress)
                else:
                    # 重挂首轮：不回放旧日志，仅从本轮对齐行号
                    emit_log = True

                meta = {}
                for line in meta_blob.splitlines():
                    if ":" in line:
                        key, val = line.split(":", 1)
                        meta[key.strip()] = val.strip()
                try:
                    last_n = max(last_n, int(meta.get("COUNT") or last_n))
                except ValueError:
                    pass
                pid = (meta.get("PID") or last_pid or "").strip()
                alive = meta.get("ALIVE") == "1"
                if pid:
                    last_pid = pid
                if pid and not alive:
                    await asyncio.sleep(1)
                    exit_code = meta.get("EXIT") or "1"
                    await self._handle_container_end(run_id, f"native-{run_id}", exit_code)
                    return
                if not pid:
                    missing_pid_rounds += 1
                    if missing_pid_rounds >= 6:
                        await self._handle_container_end(run_id, f"native-{run_id}", "1")
                        return
                    await asyncio.sleep(self._POLL_INTERVAL)
                    continue
                missing_pid_rounds = 0

                if got_new:
                    silent_rounds = 0
                else:
                    silent_rounds += 1
                    if silent_rounds % max(1, int(self._HEARTBEAT_SEC / self._POLL_INTERVAL)) == 0:
                        self._log(
                            run_id,
                            f"[SYSTEM] AutoDL 训练进程运行中（pid={pid}，暂无新日志，可能在读因子或拟合）",
                            status="running",
                            progress=progress,
                        )

                await asyncio.sleep(self._POLL_INTERVAL)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error("[%s] 原生进程轮询异常: %s", run_id, exc, exc_info=True)
            self._log(run_id, f"[ERROR] 原生进程轮询异常: {exc}", status="failed", progress=progress)

    async def _handle_container_end(self, run_id: str, container_name: str, exit_code: str) -> None:
        """训练结束后：拉取产物 → 触发注册 → 清理远端（docker 容器）。"""
        is_native = self.exec_mode == "native_python"
        try:
            if exit_code == "0":
                self._log(run_id, "[SYSTEM] 训练完成，拉取模型产物...", status="waiting_callback", progress=95)
                await self._pull_artifacts(run_id)
                self._log(run_id, "[SYSTEM] 模型产物已回传，等待模型注册...", progress=97)
                if not is_native:
                    # 清理远端容器（原生进程自然退出，无需清理）
                    await self._ssh_exec(f"docker rm -f {container_name} 2>/dev/null || true", timeout=60)
                # 触发本地模型注册（与本地流程一致）
                await self._trigger_registration(run_id)
            else:
                self._log(run_id, f"[ERROR] 训练异常退出 (exit={exit_code})", status="failed", progress=0)
                if not is_native:
                    await self._ssh_exec(f"docker rm -f {container_name} 2>/dev/null || true", timeout=60)
        except Exception as exc:  # noqa: BLE001
            logger.error("[%s] 容器结束处理失败: %s", run_id, exc, exc_info=True)
            self._log(run_id, f"[ERROR] 容器结束处理失败: {exc}", status="failed", progress=0)

    async def _cancel_remote(self, run_id: str, run_key: str) -> None:
        """用户取消：杀远端进程/容器，落 cancelled 状态并清理取消标记。"""
        if self.exec_mode == "native_python":
            pid_file = f"{self.work_dir}/train_{run_id}.pid"
            kill_cmd = (
                f"pid=$(cat {shlex.quote(pid_file)} 2>/dev/null || true); "
                f"if [ -n \"$pid\" ]; then "
                f"kill -TERM -- -\"$pid\" 2>/dev/null || kill -TERM \"$pid\" 2>/dev/null || true; "
                f"sleep 1; "
                f"kill -9 -- -\"$pid\" 2>/dev/null || kill -9 \"$pid\" 2>/dev/null || true; fi"
            )
            await self._ssh_exec(kill_cmd, timeout=30)
        else:
            container_name = run_key or f"qm-train-{run_id}"
            await self._ssh_exec(
                f"docker stop {container_name} 2>/dev/null || true; "
                f"docker rm -f {container_name} 2>/dev/null || true",
                timeout=60,
            )

        from backend.services.api.routers.admin.db import TrainingJobRecord
        from backend.shared.database_manager_v2 import get_session

        async with get_session() as db:
            r = await db.get(TrainingJobRecord, run_id)
            if r and str(r.status or "") not in ("completed", "failed"):
                r.status = "cancelled"
                r.logs = (r.logs or "") + "[SYSTEM] 训练已被用户取消，远端进程已停止\n"
                r.progress = max(int(r.progress or 0), 0)
                await db.commit()
        self._log(run_id, "[SYSTEM] 训练已被用户取消，远端进程已停止", status="cancelled", progress=0)
        self.log_stream.clear_cancel(run_id)

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
                # 节点数据窗口画像（探针读到的真实区间与交易日数），供训练侧
                # 记录与事后核对：切分就是在这份交易日序列上取的边界。
                "node_window": (
                    self._window.to_dict() if self._window is not None else None
                ),
                # 全局股票池（P3）：远端容器无 DB，池成分在本机解析后传入
                **resolve_training_pool(payload),
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
                # native（免 docker）模式下产物写 work_dir 才能被 _pull_artifacts 拉回；
                # docker 模式容器内 /workspace 即挂在 work_dir，等价。
                "workspace": self.work_dir,
                "result_path": f"{self.work_dir}/result.json",
                "required_artifacts": payload.get(
                    "required_artifacts",
                    ["model.lgb", "pred.pkl", "metadata.json", "result.json"],
                ),
            },
            "callback": {
                "url": self._callback_url(run_id),
                "secret": self.internal_secret,
            },
            "cache": {"dir": "/tmp"},
        }

        # 覆盖边界以探针窗口为准（训练数据就在该节点上）：训练侧 loading.py 用它
        # 钳制取数区间，避免用中心日历去要求节点数据（两侧日期集合本不同步）。
        if self._window is not None and self._window.min_date and self._window.max_date:
            config["data"]["factor_coverage"] = {
                "min_date": self._window.min_date,
                "max_date": self._window.max_date,
            }

        split_fields = ["valid_start", "valid_end", "test_start", "test_end"]
        if all(payload.get(k) for k in split_fields):
            config["split"] = {
                "train": [payload.get("train_start"), payload.get("train_end")],
                "valid": [payload.get("valid_start"), payload.get("valid_end")],
                "test": [payload.get("test_start"), payload.get("test_end")],
            }
            config["model"]["val_ratio"] = None
        else:
            # 探针模式：切分点用「本节点自己的交易日序列」按 val_ratio 换算成
            # 绝对日期下发，远端只按日期过滤。远端魔搭数据集的日期集合与中心
            # 不同（实测少 225 个交易日且成段缺失），用中心日历算切分会让节点上
            # 的实际样本占比失真；探针不可用时才退回中心日历兜底。
            from backend.services.engine.training import window_probe as wp

            derived = None
            if self._window is not None:
                derived = wp.build_split_from_window(self._window, payload)
            if not derived:
                derived = _derive_absolute_split(payload)
            if derived:
                config["split"] = derived
                config["model"]["val_ratio"] = None
                logger.info("[remote] absolute split from probed window: %s", derived)

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
        """构建训练回调 URL；拿不到远端可达地址时返回空串（即不发回调）。

        远端节点只能回调「协调机可达地址」，即 ``TRAINING_MASTER_HOST``（局域网 IP 或
        公网域名）。未配置时**不再回退 ``api_base``**——那是容器内服务名
        （如 ``http://quantmind:8000``），在远端节点上必然解析失败，只会让每次训练
        白等 15s 超时，并在训练日志里留下一条误导性的 ``Callback failed``。

        远端训练的完成由主节点轮询负责（拉产物 + 触发注册），回调只是可选冗余；
        拿不到可达地址就不发，交给轮询即可。空值在 train.py 侧是合法输入
        （``if callback_url:`` 直接跳过）。

        注意：本地 docker 训练走 ``LocalDockerOrchestrator`` 自己的 ``api_base``
        （同 compose 网络内可解析），不受此处影响。
        """
        if not self.master_host:
            return ""
        return f"http://{self.master_host}:8000/api/v1/models/training-runs/{run_id}/complete"

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
            f"-v {self.work_dir}/model_trainers:/app/model_trainers:ro "
            f"-v {self.work_dir}/diagnostics:/app/diagnostics:ro "
            f"-v {self.work_dir}/data:/app/data:ro "
            f"-v {self.work_dir}/templates:/app/backend/services/engine/inference/templates:ro "
            + (f"-v {self.work_dir}/modules/quantdb_factor_reader.py:/app/backend/services/engine/data_platform/quantdb_factor_reader.py:ro " if direct_source else "")
            + (f"-v {self.work_dir}/modules/quantdb_hub.py:/app/backend/services/engine/data_platform/quantdb_hub.py:ro " if direct_source else "")
            + f"--entrypoint sh {self.docker_image} -c \"{bootstrap_cmd} && exec python /app/train.py --config /workspace/config.yaml\""
        )

    def _resolve_train_script(self) -> str | None:
        """定位本地 train.py 训练脚本路径（优先项目目录，回退容器内路径）。"""
        candidates = [
            str(Path(__file__).resolve().parents[4] / "docker" / "training" / "train.py"),
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
            str(Path(__file__).resolve().parents[4] / "docker" / "training" / "preprocessing.py"),
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
            str(Path(__file__).resolve().parents[1] / "data_platform" / "quantdb_factor_reader.py"),
            "/app/backend/services/engine/data_platform/quantdb_factor_reader.py",
        ]
        return next((path for path in candidates if Path(path).is_file()), None)

    def _resolve_quantdb_hub(self) -> str | None:
        candidates = [
            str(Path(__file__).resolve().parents[1] / "data_platform" / "quantdb_hub.py"),
            "/app/backend/services/engine/data_platform/quantdb_hub.py",
        ]
        return next((path for path in candidates if Path(path).is_file()), None)

    def _resolve_parallel_utils_script(self) -> str | None:
        """定位本地 parallel_utils.py（多核因子筛选，train.py 顶层 import）。"""
        candidates = [
            str(Path(__file__).resolve().parents[4] / "docker" / "training" / "parallel_utils.py"),
            str(Path(__file__).resolve().parents[3] / "docker" / "training" / "parallel_utils.py"),
            "/app/docker/training/parallel_utils.py",
            "/app/parallel_utils.py",
        ]
        for p in candidates:
            if Path(p).exists():
                return p
        return None

    def _resolve_trainers_dir(self) -> str | None:
        """定位本地 model_trainers/ 包目录（train.py 顶层 import）。"""
        candidates = [
            str(Path(__file__).resolve().parents[4] / "docker" / "training" / "model_trainers"),
            str(Path(__file__).resolve().parents[3] / "docker" / "training" / "model_trainers"),
            "/app/docker/training/model_trainers",
            "/app/model_trainers",
        ]
        for p in candidates:
            if Path(p).is_dir():
                return p
        return None

    def _resolve_diagnostics_dir(self) -> str | None:
        """定位本地 diagnostics/ 包目录（train.py 顶层 import）。"""
        candidates = [
            str(Path(__file__).resolve().parents[4] / "docker" / "training" / "diagnostics"),
            str(Path(__file__).resolve().parents[3] / "docker" / "training" / "diagnostics"),
            "/app/docker/training/diagnostics",
            "/app/diagnostics",
        ]
        for p in candidates:
            if Path(p).is_dir():
                return p
        return None

    def _resolve_data_dir(self) -> str | None:
        """定位本地 data/ 包目录（train.py 顶层 import）。"""
        candidates = [
            str(Path(__file__).resolve().parents[4] / "docker" / "training" / "data"),
            str(Path(__file__).resolve().parents[3] / "docker" / "training" / "data"),
            "/app/docker/training/data",
            "/app/data",
        ]
        for p in candidates:
            if Path(p).is_dir():
                return p
        return None

    def _resolve_inference_template(self) -> str | None:
        """定位本地统一推理模板 inference_parquet.py。"""
        candidates = [
            str(Path(__file__).resolve().parents[4]
                / "backend" / "services" / "engine" / "inference" / "templates" / "inference_parquet.py"),
            "/app/backend/services/engine/inference/templates/inference_parquet.py",
        ]
        for p in candidates:
            if Path(p).exists():
                return p
        return None

    async def _persist_status(
        self,
        run_id: str,
        status: str,
        progress: int | None = None,
        error_line: str = "",
    ) -> None:
        """把 Redis 实时状态同步到 DB，避免切页恢复时永远停在 pending。"""
        from backend.services.api.routers.admin.db import TrainingJobRecord
        from backend.shared.database_manager_v2 import get_session

        try:
            async with get_session() as db:
                record = await db.get(TrainingJobRecord, run_id)
                if not record:
                    return
                if record.status in {"completed", "failed"} and status not in {
                    "completed",
                    "failed",
                }:
                    return
                record.status = status
                if progress is not None:
                    record.progress = max(int(record.progress or 0), int(progress))
                if status == "failed":
                    prev = record.result if isinstance(record.result, dict) else {}
                    record.result = {
                        **prev,
                        "status": "failed",
                        "error": error_line or prev.get("error") or "远程训练失败",
                    }
                await db.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[%s] persist training status failed: %s", run_id, exc)

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
        if not status:
            return
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(
                self._persist_status(
                    run_id,
                    status,
                    progress,
                    error_line=line if status == "failed" else "",
                )
            )
        except RuntimeError:
            logger.warning("[%s] no event loop to persist status=%s", run_id, status)


class LocalDockerProgress:
    """复用 LocalDockerOrchestrator 的日志进度解析逻辑。"""

    @staticmethod
    def infer(line: str, current: int) -> int:
        from backend.services.engine.training.local_docker_orchestrator import (
            LocalDockerOrchestrator,
        )

        return LocalDockerOrchestrator._infer_progress_from_log_line(line, current)
