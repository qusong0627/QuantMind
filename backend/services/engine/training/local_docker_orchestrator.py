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
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

try:
    import docker
    DockerClient = getattr(docker, "DockerClient", None)
except (ImportError, AttributeError):
    docker = None  # type: ignore
    DockerClient = None  # type: ignore

import yaml

from backend.services.engine.training.training_log_stream import TrainingRunLogStream
from backend.services.engine.training.orchestrator_base import TrainingOrchestrator, REGISTRY
from backend.services.api.training_explain import DEFAULT_EXPLAIN_CFG
from backend.services.engine.data_platform.quantdb_factor_reader import (
    MARKET_DATA_DIR_ENV as _MARKET_DATA_DIR_ENV,
    MARKET_DATA_DIR_DEFAULT as _MARKET_DATA_DIR_DEFAULT,
    market_data_dir,
    normalize_market,
)

logger = logging.getLogger(__name__)

_TRAINING_IMAGE = (os.getenv("TRAINING_IMAGE") or "quantmind-trainer:latest").strip()
# 训练容器启动前补齐的依赖（空格分隔的包名）。训练镜像可能落后于仓库依赖
# （如 QuantDB 因子目录读取所需的 duckdb），缺失会在 load_data 时 ImportError 秒挂。
# 已存在的包会被探测跳过，无额外开销；镜像重建后该项自然退化为空操作。
_TRAINING_BOOTSTRAP_PIP = (os.getenv("TRAINING_BOOTSTRAP_PIP") or "duckdb pyqlib").strip()


def _host_mem_limit_gb() -> str | None:
    """训练容器的 mem_limit（GB 字符串），保护宿主机不被训练进程打爆。

    编排器运行在 api 容器内，/proc/meminfo 显示宿主机总内存（cgroup v2 不虚拟化）。
    取宿主机 80%（下限 20GB、上限 64GB）；读不到内存信息时返回 None（不限）。
    限制留 20% 余量给宿主机其它服务（PG/Redis/LLM 容器等），
    训练峰值超限时只牺牲训练容器（OOM → ExitCode 137），不拖垮整机。
    """
    try:
        with open("/proc/meminfo", encoding="utf-8") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    total_gb = int(line.split()[1]) / 1024.0 / 1024.0
                    limit_gb = max(20, min(64, int(total_gb * 0.8)))
                    return f"{limit_gb}g"
    except OSError:
        return None
    return None
_CALLBACK_TIMEOUT = int(os.getenv("TRAINING_CALLBACK_TIMEOUT_SECONDS", "600"))
_POLL_INTERVAL = 10  # 秒
_CALLBACK_CHECK_INTERVAL = int(
    os.getenv("TRAINING_CALLBACK_CHECK_INTERVAL_SECONDS", "2")
)
_DOCKER_NETWORK = os.getenv("TRAINING_DOCKER_NETWORK", "quantmind-network")
# ── 路径配置（Docker-in-Docker 场景）────────────────────────────────────────────
# API 容器通过 /var/run/docker.sock 与宿主机 Docker daemon 通信。
# Docker daemon 需要的 volume 路径是 docker-compose.yml 中 bind mount 的
# 宿主机端路径（即 ./data 展开后的绝对路径）。
#
# 已知映射（来自 docker-compose.yml）：
#   ./data:/data        → 宿主机 <compose_dir>/data  ←→ 容器 /data
#   ./backend:/app/backend  → 宿主机 <compose_dir>/backend  ←→ 容器 /app/backend

_LOCAL_DATA_MOUNT_DIR = "/tmp/feature_snapshots"
_QUANTDB_DATA_MOUNT_DIR = "/tmp/quantdb_data"
# 非 CN 市场的数据目录挂载（与 QUANTDB_DATA_MOUNT_DIR 语义一致）
_MARKET_DATA_MOUNT_DIRS = {
    "CN": _QUANTDB_DATA_MOUNT_DIR,
    "HK": "/tmp/quanthk_data",
    "US": "/tmp/quantus_data",
    "CRYPTO": "/tmp/quantbc_data",
    "FUTURES": "/tmp/quantfutures_data",
}
# 训练容器内环境变量名（train.py 按市场选择数据根目录）
_MARKET_MOUNT_ENV_VARS = {
    "CN": "QUANTDB_DATA_DIR",
    "HK": "QUANTHK_DATA_DIR",
    "US": "QUANTUS_DATA_DIR",
    "CRYPTO": "QUANTBC_DATA_DIR",
    "FUTURES": "QUANTFUTURES_DATA_DIR",
}

# ── 训练资源保护：训练期间临时停止其它容器，把内存腾给训练任务 ───────────────────
# 通过 TRAINING_PAUSE_OTHERS=false 可关闭该行为
_PAUSE_OTHERS_ENABLED = os.getenv("TRAINING_PAUSE_OTHERS", "true").strip().lower() not in {
    "0", "false", "no", "off",
}
# 受保护的容器名前缀：训练期间永远不停。可通过环境变量覆盖（逗号分隔）。
# 默认保护 quantmind 全家桶 + 训练容器自身 + 通用基础依赖。
_DEFAULT_PROTECTED_PREFIXES = ("quantmind", "qm-train-")
_PROTECTED_PREFIXES: tuple[str, ...] = tuple(
    p.strip()
    for p in (
        os.getenv("TRAINING_PROTECTED_NAME_PREFIXES")
        or ",".join(_DEFAULT_PROTECTED_PREFIXES)
    ).split(",")
    if p.strip()
)

# 宿主机 compose 工作目录
_raw = (os.getenv("HOST_PROJECT_PATH") or "").strip()
if _raw and _raw != ".":
    _HOST_PROJECT_PATH = Path(_raw).resolve()
else:
    # 退化为当前工作目录（容器内通常为 /app）
    _HOST_PROJECT_PATH = Path.cwd().resolve()

# 数据目录：feature_snapshots 在 /app/db/feature_snapshots（来自 ./db:/app/db 挂载）
# Docker volume host path 需要宿主机绝对路径
if Path("/app/db/feature_snapshots").exists():
    _LOCAL_DATA_PATH = str(_HOST_PROJECT_PATH / "db" / "feature_snapshots")
elif Path("/data/feature_snapshots").exists():
    _LOCAL_DATA_PATH = str(_HOST_PROJECT_PATH / "data" / "feature_snapshots")
else:
    _LOCAL_DATA_PATH = str(_HOST_PROJECT_PATH / "db" / "feature_snapshots")

# QuantDB 数据目录：train.py 需要读取 instrument_detail.parquet（行业编码）等全量数据
# 注意 QM_QUANTDB_DATA_DIR 是【容器内】路径（如 /data/quantdb，来自 ./data:/data 挂载），
# 而 Docker volume 的 source 必须是【宿主机】绝对路径，不能直接使用该值。
# 这里把容器内路径换算回宿主机路径，语义与 _LOCAL_DATA_PATH 保持一致。
_qdb_dir = os.getenv("QM_QUANTDB_DATA_DIR", "").strip() or "/data/quantdb"
_qdb_path = Path(_qdb_dir)
if _qdb_path.is_relative_to("/data"):
    # /data/quantdb → <host_project>/data/quantdb
    _QUANTDB_DATA_HOST_PATH = str(
        _HOST_PROJECT_PATH / "data" / _qdb_path.relative_to("/data")
    )
elif _qdb_path.is_relative_to("/app"):
    # /app/data/quantdb → <host_project>/data/quantdb
    _QUANTDB_DATA_HOST_PATH = str(
        _HOST_PROJECT_PATH / _qdb_path.relative_to("/app")
    )
elif _qdb_path.is_absolute():
    _QUANTDB_DATA_HOST_PATH = str(_qdb_path)
else:
    _QUANTDB_DATA_HOST_PATH = str(_HOST_PROJECT_PATH / _qdb_path)


def _market_host_path(container_dir: str) -> str:
    """把容器内数据目录（/data/quanthk 等）换算为宿主机绝对路径。"""
    p = Path(container_dir)
    if p.is_relative_to("/data"):
        return str(_HOST_PROJECT_PATH / "data" / p.relative_to("/data"))
    if p.is_relative_to("/app"):
        return str(_HOST_PROJECT_PATH / p.relative_to("/app"))
    if p.is_absolute():
        return str(p)
    return str(_HOST_PROJECT_PATH / p)


def _market_data_mount(market: str) -> tuple[str, str]:
    """返回 (宿主机数据目录, 训练容器挂载目录)；CN 复用既有 QuantDB 常量。"""
    market = normalize_market(market)
    if market == "CN":
        return _QUANTDB_DATA_HOST_PATH, _QUANTDB_DATA_MOUNT_DIR
    container_dir = (
        os.getenv(_MARKET_DATA_DIR_ENV[market], "").strip()
        or _MARKET_DATA_DIR_DEFAULT[market]
    )
    return _market_host_path(container_dir), _MARKET_DATA_MOUNT_DIRS[market]

# 训练脚本：./docker/training/train.py 挂载到容器内 /app/docker/training/
_TRAINING_SCRIPT_HOST_PATH = str(_HOST_PROJECT_PATH / "docker" / "training" / "train.py")
# 预处理纯函数集：train.py 顶层 `from preprocessing import ...`，需与 train.py 一并挂载
_PREPROCESSING_HOST_PATH = str(_HOST_PROJECT_PATH / "docker" / "training" / "preprocessing.py")
# 多核因子筛选：train.py 顶层 `from parallel_utils import ...`，需与 train.py 一并挂载
_PARALLEL_UTILS_HOST_PATH = str(_HOST_PROJECT_PATH / "docker" / "training" / "parallel_utils.py")

def _validate_config_dict(run_id: str, config: dict) -> dict:
    """B1 schema 门：config.yaml 经 TrainingConfig 校验后返回契约字典。

    - 校验通过：返回 dump_contract_dict（parsed 与输入相等；key 顺序/整数浮点写法
      可能不同，消费者一律 yaml.safe_load，不影响）。
    - 校验失败：记 warning 并回退原手拼 dict（fail-open，行为不变；B2 再收紧）。
    """
    try:
        from backend.shared.training.schemas import TrainingConfig, dump_contract_dict

        return dump_contract_dict(TrainingConfig.from_dict(config))
    except Exception as exc:
        logger.warning("[%s] TrainingConfig validation failed, fallback legacy dict: %s", run_id, exc)
        return config


class LocalDockerOrchestrator(TrainingOrchestrator):
    def __init__(self):
        self.docker = DockerClient.from_env()
        self.api_base = (
            os.getenv("QUANTMIND_API_BASE_URL") or "http://quantmind-api:8000"
        ).strip()
        self.internal_secret = (os.getenv("INTERNAL_CALL_SECRET") or "").strip()
        # P0-3: 强制 fail-closed。secret 缺失直接抛错，不再用空 secret 走 fail-open。
        if not self.internal_secret:
            raise RuntimeError(
                "INTERNAL_CALL_SECRET not set; cannot start training orchestrator. "
                "Set it in .env or QUANTMIND_ENV=development for auto-generation."
            )
        self.log_stream = TrainingRunLogStream()

    # ── GPU 自动检测 ─────────────────────────────────────────────────────────────
    async def _detect_gpu_available(self) -> bool:
        """探测训练容器能否拿到 GPU，无 GPU 时回退 CPU 训练。

        三层探测：
        1. API 容器内 nvidia-smi / torch（裸机直挂或容器直通 GPU 场景）
        2. Docker-in-Docker：用训练镜像起一次性探针容器实测 --gpus all
           （api 容器本身无 nvidia-smi，但宿主 daemon 可能已配 nvidia runtime）
        探针带 60s TTL 缓存，与就绪面板共享，训练启动不会反复起容器。
        """
        from backend.services.engine.training.node_manager import NodeStatus

        try:
            proc = await asyncio.create_subprocess_exec(
                "nvidia-smi", "-L",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            if await asyncio.wait_for(proc.wait(), timeout=5) == 0:
                return True
        except Exception:
            pass
        try:
            import torch
            if torch.cuda.is_available():
                return True
        except Exception:
            pass
        gpus = await NodeStatus._probe_gpu_via_docker(self.docker, _TRAINING_IMAGE)
        return bool(gpus)

    # ── 训练期间资源保护 ──────────────────────────────────────────────────────────
    @staticmethod
    def _is_protected(name: str) -> bool:
        n = (name or "").lstrip("/")
        return any(n.startswith(p) for p in _PROTECTED_PREFIXES)

    def _pause_others(
        self,
        work_dir: Path,
        run_id: str,
        pause_others: bool | None = None,
    ) -> list[str]:
        """停止所有非保护的运行中容器，把名字写到 work_dir/.paused_containers.json。

        返回被停止的容器名列表。失败时记录 warning 但不抛出，保证训练能继续。

        pause_others 参数（前端训练开关透传）：
        - None → 用环境变量 TRAINING_PAUSE_OTHERS 默认值（启动时决定）
        - True/False → 显式覆盖环境变量（用户在前端选了是否停其他容器）
        """
        if pause_others is None:
            enabled = _PAUSE_OTHERS_ENABLED
        else:
            enabled = pause_others
        if not enabled:
            logger.info("[%s] pause-others disabled (env=%s, req=%s), skip", run_id, _PAUSE_OTHERS_ENABLED, pause_others)
            return []

        paused: list[str] = []
        try:
            containers = self.docker.containers.list(filters={"status": "running"})
        except Exception as exc:
            logger.warning("[%s] list running containers failed: %s", run_id, exc)
            return []

        for c in containers:
            name = c.name or ""
            if self._is_protected(name):
                continue
            try:
                # 用 stop 而不是 pause：pause 仍然占内存，stop 才能释放
                c.stop(timeout=20)
                paused.append(name)
                logger.info("[%s] paused container: %s", run_id, name)
            except Exception as exc:
                logger.warning(
                    "[%s] stop container %s failed: %s", run_id, name, exc
                )

        # 落盘，宿主重启 / 进程崩溃后也能从 work_dir 恢复
        try:
            state_path = Path(work_dir) / ".paused_containers.json"
            state_path.write_text(
                json.dumps(
                    {
                        "run_id": run_id,
                        "paused_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        "containers": paused,
                        "protected_prefixes": list(_PROTECTED_PREFIXES),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.warning("[%s] write paused-state file failed: %s", run_id, exc)

        if paused:
            logger.info(
                "[%s] paused %d containers to free memory for training: %s",
                run_id,
                len(paused),
                paused,
            )
        return paused

    def _resume_others(self, work_dir: Path, run_id: str) -> list[str]:
        """恢复 _pause_others 停止的容器。幂等：状态文件不存在时直接返回。"""
        state_path = Path(work_dir) / ".paused_containers.json"
        if not state_path.exists():
            return []

        try:
            data = json.loads(state_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("[%s] read paused-state file failed: %s", run_id, exc)
            return []

        names: list[str] = list(data.get("containers") or [])
        resumed: list[str] = []
        for name in names:
            try:
                c = self.docker.containers.get(name)
                if c.status != "running":
                    c.start()
                resumed.append(name)
                logger.info("[%s] resumed container: %s", run_id, name)
            except docker.errors.NotFound:
                logger.warning(
                    "[%s] cannot resume %s: container no longer exists", run_id, name
                )
            except Exception as exc:
                logger.warning("[%s] start container %s failed: %s", run_id, name, exc)

        # 标记为已处理：保留文件但加 resumed_at，便于事后排查
        try:
            data["resumed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            data["resumed"] = resumed
            state_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception:
            pass

        if resumed:
            logger.info(
                "[%s] resumed %d containers after training: %s",
                run_id,
                len(resumed),
                resumed,
            )
        return resumed

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
    def _filter_features_by_parquet(
        run_id: str, requested_features: list[str]
    ) -> tuple[list[str], list[str]]:
        """检查请求的特征是否存在于 parquet 中，返回 (valid, missing)。

        不做 return_Nd → mom_ret_Nd 别名回退：features_daily.return_Nd 是未来
        N 日收益，用作特征会泄漏标签。mom_ret_Nd 必须由 l1_factors 提供。
        """
        try:
            import pyarrow.parquet as pq
            from pathlib import Path

            parquet_dir = Path(_LOCAL_DATA_MOUNT_DIR)
            if not parquet_dir.exists():
                logger.warning("[%s] Parquet dir not found: %s", run_id, parquet_dir)
                return requested_features, []

            # 训练实际读 core parquet（存在时），否则读 A 股逐年文件。
            # 用 core schema 或 2023-2026 逐年 schema 的并集做校验，
            # 而不是 sorted(glob)[-1]（字母序最后一个是 model_features_us，
            # 无 L2/行业列，会把 A 股特征误报 missing）。
            parquet_cols: set[str] = set()
            core = parquet_dir / "model_features_core.parquet"
            if core.exists():
                parquet_cols |= set(pq.ParquetFile(core).schema_arrow.names)
            else:
                for p in sorted(parquet_dir.glob("model_features_20*.parquet")):
                    parquet_cols |= set(pq.ParquetFile(p).schema_arrow.names)
            if not parquet_cols:
                logger.warning("[%s] No parquet files in %s", run_id, parquet_dir)
                return requested_features, []

            valid = [f for f in requested_features if f in parquet_cols]
            missing = [f for f in requested_features if f not in parquet_cols]
            return valid, missing
        except Exception as exc:
            logger.warning("[%s] Feature filter failed: %s", run_id, exc)
            return requested_features, []

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
        # LightGBM / XGBoost / CatBoost early stopping patterns
        if "did not meet early stopping" in text or "early stopping, best iteration" in text:
            next_progress = max(next_progress, 70)
        if "early stopping" in text and ("round" in text or "iteration" in text):
            next_progress = max(next_progress, 70)
        if "training finished" in text:
            next_progress = max(next_progress, 80)
        # Multi-model model save patterns
        if "model saved" in text or "model.lgb" in text or "model.xgb" in text or "model.cbm" in text:
            next_progress = max(next_progress, 85)
        if "predictions saved" in text or "pred.parquet" in text or "pred.pkl" in text:
            next_progress = max(next_progress, 90)
        if "result.json" in text or "result report saved" in text:
            next_progress = max(next_progress, 95)
        if "metadata.json saved" in text or "inference.py" in text:
            next_progress = max(next_progress, 98)
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
        factor_source = str(payload.get("factor_source") or "").strip()

        # 直读模式按选定的单一 QuantDB 因子源过滤；旧模型仍走快照兼容路径。
        requested_features = payload.get("features", [])
        market = normalize_market(context.get("market") or "CN")
        _, market_mount_dir = _market_data_mount(market)
        if factor_source:
            try:
                from backend.services.engine.data_platform.quantdb_factor_reader import QuantDBFactorReader

                source_start = payload.get("train_start") or "2023-01-11"
                source_end = payload.get("test_end") or payload.get("valid_end") or payload.get("train_end") or ""
                # 容器内数据根目录（api 容器 /data 挂载可见），与训练容器
                # 挂载的 market_mount_dir 同源。
                source_status = QuantDBFactorReader(market=market).assert_ready(
                    factor_source,
                    start=str(source_start) or None,
                    end=str(source_end) or None,
                )
                available = set(source_status.columns)
                field_sources = dict(payload.get("factor_field_sources") or {})
                valid_features = [
                    feature for feature in requested_features
                    if field_sources.get(feature, feature) in available
                ]
                missing_features = [
                    feature for feature in requested_features
                    if field_sources.get(feature, feature) not in available
                ]
            except Exception as exc:
                raise RuntimeError(f"QuantDB factor source {factor_source} is not ready: {exc}") from exc
        else:
            valid_features, missing_features = self._filter_features_by_parquet(
                run_id, requested_features
            )
        if missing_features:
            logger.warning(
                "[%s] %d/%d requested features not in parquet, filtered out: %s...",
                run_id,
                len(missing_features),
                len(requested_features),
                missing_features[:10],
            )
        # 将过滤结果存到 payload 中，供后续返回给前端
        payload["_valid_features"] = valid_features
        payload["_missing_features"] = missing_features

        config: dict[str, Any] = {
            "run_id": run_id,
            "job_name": payload.get("job_name", "unnamed"),
            "data": {
                "train_start": payload.get("train_start", "2022-01-01"),
                "train_end": payload.get("train_end", "2024-12-31"),
                "features": valid_features,
                "source_mode": data_source_mode,
                "local_dir": market_mount_dir if factor_source else (
                    _LOCAL_DATA_MOUNT_DIR if data_source_mode == "LOCAL" else None
                ),
                "factor_source": factor_source or None,
                "factor_catalog_version": str(payload.get("factor_catalog_version") or "") or None,
                "factor_schema_hash": source_status.schema_hash if factor_source else None,
                "factor_field_sources": dict(payload.get("factor_field_sources") or {}),
                "factor_catalog_published_at": str(payload.get("factor_catalog_published_at") or "") or None,
                "factor_coverage": dict(payload.get("factor_coverage") or {}),
                "quantdb_dir": market_mount_dir if factor_source else None,
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
                "xgb_params": {
                    k: v
                    for k, v in payload.get("xgb_params", {}).items()
                    # LightGBM max_depth=-1 convention is invalid for XGBoost; drop it
                    if not (k == "max_depth" and isinstance(v, (int, float)) and v < 0)
                },
                "catboost_params": payload.get("catboost_params", {}),
                "dl_params": payload.get("dl_params", {}),
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

        # WFA 稳定性诊断配置（可选，透传给训练脚本）
        if payload.get("wfa") and isinstance(payload.get("wfa"), dict):
            config["wfa"] = payload["wfa"]

        # 训练时长预算（分钟），透传给训练脚本供阶段级超时检查
        try:
            config["max_time_minutes"] = max(10, int(payload.get("max_time_minutes") or 120))
        except Exception:
            config["max_time_minutes"] = 120

        # 特征准入自动化：默认启用 IC/ICIR 因子筛选（剔除无信号特征），
        # 前端/请求显式指定 factor_selection 时以显式配置为准。
        fs_cfg = payload.get("factor_selection")
        if isinstance(fs_cfg, dict):
            config["factor_selection"] = fs_cfg
        elif str(payload.get("auto_feature_filter", "true")).lower() in ("1", "true", "yes", "on"):
            config["factor_selection"] = {
                "method": "ic_icir",
                "n_top": 80,
                "ic_threshold": 0.01,
                "icir_threshold": 0.15,
                "correlation_threshold": 0.9,
            }

        # 特征截面预处理配置（P1）：默认关闭，兼容旧模型；显式开启后
        # train.py 对特征做 per-(trade_date,feature) 中位数填充+缩尾+Z-score。
        pp_cfg = payload.get("preprocessing")
        if isinstance(pp_cfg, dict):
            config["preprocessing"] = pp_cfg
        elif str(payload.get("enable_cross_sectional_prep", "false")).lower() in ("1", "true", "yes", "on"):
            config["preprocessing"] = {"enabled": True, "winsor": True}
        # B1：过 TrainingConfig 校验门（通过则返回 model_dump，parsed 等价；失败回退原 dict）。
        return _validate_config_dict(run_id, config)

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

        # ── 准备训练工作目录 ────────────────────────────────────────────────────
        # 使用 /data/training_jobs/{run_id} 作为训练容器的工作目录。
        # /data 是 docker-compose 中 ./data:/data 的挂载点，
        # API 容器写入的文件对宿主机和训练容器都可见。
        # 这避免了 _HOST_PROJECT_PATH 在容器内外指向不同文件系统的问题。
        from backend.shared.model_registry import model_registry_service

        model_id = model_registry_service.build_model_id_from_run(run_id)

        # API 容器内的模型注册路径（用于回调后注册模型）
        user_models_root = Path(model_registry_service.user_models_root)
        internal_models_root = (
            user_models_root
            if user_models_root.is_absolute()
            else Path("/app") / user_models_root
        )
        internal_output_dir = internal_models_root / tenant_id / user_id / model_id

        # 训练容器工作目录：使用 /data 挂载点下的路径
        # API 容器内路径：/data/training_jobs/{run_id}（通过 ./data:/data 挂载）
        # 宿主机路径：/opt/quantmind/data/training_jobs/{run_id}（Docker daemon 需要）
        container_work_dir = Path("/data") / "training_jobs" / run_id

        _compose_dir = _HOST_PROJECT_PATH if _HOST_PROJECT_PATH.is_absolute() else Path.cwd()
        host_output_dir = _compose_dir / "data" / "training_jobs" / run_id

        # 强制创建目录（使用容器内路径，确保 API 容器可写入）
        os.makedirs(internal_output_dir, exist_ok=True)
        os.makedirs(container_work_dir, exist_ok=True)
        logger.info(
            "[%s] Training work directory prepared: %s (host mount: %s)",
            run_id,
            container_work_dir,
            host_output_dir,
        )
        logger.info(
            "[%s] Model registry path prepared: %s",
            run_id,
            internal_output_dir,
        )

        # ── 提前将 config.yaml 写入训练工作目录 ─────────────────────────────
        # 写入 container_work_dir（容器内 /data/training_jobs/{run_id}/），
        # 该目录通过 bind mount 与宿主机 /opt/quantmind/data/training_jobs/{run_id}/ 同步，
        # 会被 Docker 挂载为训练容器的 /workspace
        config_path = container_work_dir / "config.yaml"
        try:
            with open(config_path, "w", encoding="utf-8") as f:
                yaml.dump(config, f, allow_unicode=True, default_flow_style=False)
                f.flush()
                os.fsync(f.fileno())
            logger.info("[%s] Config saved: %s", run_id, config_path)
            # Verify the file is visible (bind mount propagation)
            if not config_path.exists():
                raise RuntimeError(f"Config file not visible after write: {config_path}")
        except Exception as e:
            logger.error("[%s] Failed to save config: %s", run_id, e)
            raise

        # 始终挂载本地数据目录（宿主机路径，API 容器内 os.path.exists 无法感知）
        volumes: dict[str, dict[str, str]] = {
            str(host_output_dir): {"bind": "/workspace", "mode": "rw"},
        }
        if not config.get("data", {}).get("factor_source"):
            volumes[str(_LOCAL_DATA_PATH)] = {"bind": _LOCAL_DATA_MOUNT_DIR, "mode": "ro"}
        # 挂载 QuantDB 全量数据（6大类：kline/base_sector/financial/bond_etf/technical_derived/ml_datasets）
        # 存在性检查必须针对【容器内】可见的 _qdb_dir，而非宿主机路径
        # （API 容器内 os.path.exists 无法感知宿主机路径，与 _LOCAL_DATA_PATH 同理）
        if Path(_qdb_dir).exists():
            volumes[_QUANTDB_DATA_HOST_PATH] = {
                "bind": _QUANTDB_DATA_MOUNT_DIR,
                "mode": "ro",
            }
            logger.info(
                "[%s] QuantDB data mounted: %s (host) -> %s",
                run_id,
                _QUANTDB_DATA_HOST_PATH,
                _QUANTDB_DATA_MOUNT_DIR,
            )
        else:
            logger.warning(
                "[%s] QuantDB data dir not visible at %s; skipping mount "
                "(industry code ind_code_l1 will be unavailable)",
                run_id,
                _qdb_dir,
            )
        # 非 CN 市场：挂载该市场数据根目录（quanthk/quantus/…），训练直读
        # 6_ml_datasets 因子源；CN 复用上方 QuantDB 挂载。
        _train_market = normalize_market(
            str((payload.get("context") or {}).get("market") or "CN")
        )
        if _train_market != "CN":
            _mk_host, _mk_mount = _market_data_mount(_train_market)
            if Path(market_data_dir(_train_market)).exists():
                volumes[_mk_host] = {"bind": _mk_mount, "mode": "ro"}
                logger.info(
                    "[%s] %s data mounted: %s (host) -> %s",
                    run_id, _train_market, _mk_host, _mk_mount,
                )
            else:
                logger.warning(
                    "[%s] %s data dir not visible at %s; factor source will be unavailable",
                    run_id, _train_market, market_data_dir(_train_market),
                )
        logger.info(
            "[%s] Training workspace mounted: %s (host) -> /workspace (container writes to %s)",
            run_id,
            host_output_dir,
            container_work_dir,
        )
        if not config.get("data", {}).get("factor_source"):
            logger.info("[%s] Local data path mounted: %s -> %s", run_id, _LOCAL_DATA_PATH, _LOCAL_DATA_MOUNT_DIR)
        # 始终挂载宿主机 train.py 覆盖镜像内脚本（注意：os.path.exists 在 API 容器内无法感知宿主机路径，固定挂载）
        volumes[str(_TRAINING_SCRIPT_HOST_PATH)] = {
            "bind": "/app/train.py",
            "mode": "ro",
        }
        # preprocessing.py 与 train.py 同目录导入（`from preprocessing import ...`）。
        # 与 train.py 一样无条件挂载：API 容器内 os.path.exists 无法感知宿主机路径，
        # 条件判断会导致宿主机文件存在但容器内看不到 → 挂载被跳过 → ImportError。
        volumes[str(_PREPROCESSING_HOST_PATH)] = {
            "bind": "/app/preprocessing.py",
            "mode": "ro",
        }
        # parallel_utils.py 与 train.py 同目录导入（`from parallel_utils import ...`），
        # 多核因子筛选；与 train.py 一样无条件挂载（路径可见性限制同上）。
        volumes[str(_PARALLEL_UTILS_HOST_PATH)] = {
            "bind": "/app/parallel_utils.py",
            "mode": "ro",
        }
        # backend 代码同步挂载：训练镜像内 bake 的 backend 落后于仓库时会缺新模块
        # （如 quantdb_factor_reader → 训练容器 load_data ImportError 秒挂）。
        # 与 train.py 挂载同理；/app/backend 是 compose bind mount，API 容器内
        # 可直接感知其存在（宿主机路径由 HOST_PROJECT_PATH 换算）。
        if Path("/app/backend").exists():
            volumes[str(_HOST_PROJECT_PATH / "backend")] = {
                "bind": "/app/backend",
                "mode": "ro",
            }
            logger.info(
                "[%s] Backend code mounted: %s -> /app/backend",
                run_id,
                _HOST_PROJECT_PATH / "backend",
            )
        else:
            logger.warning(
                "[%s] /app/backend is not a bind mount; training container will use "
                "baked-in backend code (may be stale)",
                run_id,
            )
        logger.info(
            "[%s] Local train.py override mounted: %s -> /app/train.py",
            run_id,
            _TRAINING_SCRIPT_HOST_PATH,
        )
        logger.info(
            "[%s] PERSISTENCE Local output mounted: %s (host) -> /workspace (container: %s)",
            run_id,
            host_output_dir,
            container_work_dir,
        )
        logger.info("[%s] Final volumes config: %s", run_id, volumes)

        # 启动训练容器之前：停掉其它非保护容器，把内存腾出来给训练
        # 用 to_thread 包装：避免 docker.stop（含 SIGTERM 等待）阻塞主 event loop
        # pause_others 支持请求级覆盖：payload 里带 pause_others（前端开关）时优先
        try:
            req_pause = payload.get("pause_others") if isinstance(payload, dict) else None
            if req_pause is None and isinstance(payload, dict):
                ctx = payload.get("context")
                if isinstance(ctx, dict):
                    req_pause = ctx.get("pause_others")
            paused = await asyncio.to_thread(
                self._pause_others, container_work_dir, run_id, req_pause
            )
            if paused:
                self.log_stream.append_log(
                    run_id=run_id,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    line=f"[SYSTEM] Paused {len(paused)} containers to free memory: "
                    + ", ".join(paused),
                    status="provisioning",
                    progress=10,
                )
        except Exception as pause_err:
            logger.warning("[%s] pause others failed (continuing): %s", run_id, pause_err)

        try:
            # P0-2 孤儿容器策略 A：launch 入口先停 + 删除同名旧容器
            # 场景：API 重启 → recover 重新调度 launch → 旧容器可能仍 Running/Exited
            # - running: stop(timeout=10) 优雅停 → remove
            # - exited: 直接 remove
            # - NotFound: 正常，继续
            container_name = f"qm-train-{run_id}"
            try:
                existing = await asyncio.to_thread(
                    self.docker.containers.get, container_name
                )
                if existing.status == "running":
                    logger.warning(
                        "[%s] orphan container %s still running, stopping first",
                        run_id, container_name,
                    )
                    await asyncio.to_thread(existing.stop, timeout=10)
                await asyncio.to_thread(existing.remove)
                logger.info(
                    "[%s] removed orphan container %s (status=%s)",
                    run_id, container_name, existing.status,
                )
            except Exception as get_exc:
                # NotFound 走这里（容器不存在，正常）；其它异常 warn 但不阻塞
                if "NotFound" in type(get_exc).__name__ or "not found" in str(get_exc).lower():
                    pass  # 正常：没有旧容器
                else:
                    logger.warning(
                        "[%s] check orphan container %s failed (continuing): %s",
                        run_id, container_name, get_exc,
                    )

            # GPU 自动选择：有 GPU 请求全部设备，无 GPU 回退 CPU 训练
            # （train.py 内部按 torch.cuda.is_available() 自行回退 CPU 路径）
            gpu_available = await self._detect_gpu_available()
            device_requests = (
                [docker.types.DeviceRequest(count=-1, capabilities=[["gpu"]])]
                if gpu_available
                else None
            )
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
            # 启动前探测并补齐缺失依赖：镜像 bake 的依赖落后于仓库时自动 pip 补齐，
            # 避免 train.py 一进 load_data 就 ImportError。包已存在则直接跳过。
            bootstrap_cmds = [
                f"python -c 'import importlib,sys; importlib.import_module(sys.argv[1])' {pkg} 2>/dev/null || "
                f"python -m pip install -q --disable-pip-version-check {pkg} || exit 1"
                for pkg in _TRAINING_BOOTSTRAP_PIP.split()
            ]
            bootstrap_cmd = " && ".join(bootstrap_cmds) if bootstrap_cmds else "true"
            container = await asyncio.to_thread(
                self.docker.containers.run,
                _TRAINING_IMAGE,
                # 显式覆盖镜像 ENTRYPOINT（train.py）：bootstrap 需要真正的 shell 环境，
                # 否则 sh -c 会被 train.py 当作 CLI 参数忽略（旧镜像无 ENTRYPOINT 也兼容）。
                entrypoint=["sh", "-c"],
                command=[
                    f"{bootstrap_cmd} && exec python /app/train.py --config /workspace/config.yaml",
                ],
                environment={
                    "INTERNAL_CALL_SECRET": self.internal_secret,
                    "USE_LOCAL_DATA": "true",
                    "TRAINING_LOCAL_DATA_DIR": _LOCAL_DATA_MOUNT_DIR,
                    "TRAINING_CACHE_DIR": "/tmp",
                    "QLIB_PROVIDER_URI": os.getenv("QLIB_PROVIDER_URI", ""),
                    # 市场数据根目录（train.py 按 context.market 选择读哪个 env）
                    _MARKET_MOUNT_ENV_VARS[_train_market]: _market_data_mount(_train_market)[1],
                    # 透传 IC 并行度覆盖（不设置时 parallel_utils 按剩余内存预算收缩）
                    "TRAIN_IC_WORKERS": os.getenv("TRAIN_IC_WORKERS", ""),
                    # 透传树模型线程数覆盖（不设置时 train.py 默认 -1 用满所有核心）。
                    # 宿主环境可设 TRAIN_NTHREADS=4 限流，避免训练抢破产线/行情等其它服务。
                    "TRAIN_NTHREADS": os.getenv("TRAIN_NTHREADS", ""),
                    # B2 A/B 对齐窗口：TRAINING_OLD_DISPATCH=1 时训练容器走旧 if/elif 分派。
                    "TRAINING_OLD_DISPATCH": os.getenv("TRAINING_OLD_DISPATCH", ""),
                },
                volumes=volumes,
                network=_DOCKER_NETWORK,
                detach=True,
                name=container_name,
                device_requests=device_requests,
                mem_limit=_host_mem_limit_gb(),
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
            # 启动失败，立刻恢复被暂停的容器
            try:
                await asyncio.to_thread(
                    self._resume_others, container_work_dir, run_id
                )
            except Exception as resume_err:
                logger.warning(
                    "[%s] resume others after docker run failure failed: %s",
                    run_id,
                    resume_err,
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

        # 训练时长预算在 launch 作用域计算后透传给轮询循环。
        # （_poll_container 作用域内无 payload，此前直接引用触发 NameError
        # 被 except 兜底吞掉，用户选 12 小时也会在 120 分钟被杀）
        try:
            max_time_minutes = max(10, int(payload.get("max_time_minutes") or 120))
        except Exception:
            max_time_minutes = 120

        REGISTRY.register(
            self._poll_container(
                run_id,
                container.id,
                tenant_id=tenant_id,
                user_id=user_id,
                work_dir=container_work_dir,
                max_time_minutes=max_time_minutes,
            )
        )

    # ── 轮询容器状态 ─────────────────────────────────────────────────────────────
    async def _poll_container(
        self,
        run_id: str,
        container_id: str,
        *,
        tenant_id: str,
        user_id: str,
        work_dir: Path | None = None,
        max_time_minutes: int = 120,
    ) -> None:
        from backend.services.api.routers.admin.db import TrainingJobRecord
        from backend.shared.database_manager_v2 import get_session

        async def _try_resume() -> None:
            if work_dir is None:
                return
            try:
                # docker start 可能阻塞数秒，丢到线程池避免卡住 event loop
                resumed = await asyncio.to_thread(
                    self._resume_others, work_dir, run_id
                )
                if resumed:
                    self.log_stream.append_log(
                        run_id=run_id,
                        tenant_id=tenant_id,
                        user_id=user_id,
                        line=f"[SYSTEM] Resumed {len(resumed)} containers: "
                        + ", ".join(resumed),
                        status=None,
                        progress=None,
                        container_id=container_id[:12],
                    )
            except Exception as exc:
                logger.warning("[%s] resume others failed: %s", run_id, exc)

        # 训练时长预算：由 launch_training_job 透传（默认 120 分钟）
        deadline = time.time() + max_time_minutes * 60
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
                    # 产物同步由 complete_training_run →
                    # register_model_from_training_run._sync_candidate_artifacts 完成
                    # （源目录 /data/training_jobs/{run_id}，含非 CN 市场分段路径），
                    # 此处不重复复制（此前的复制块引用未定义变量恒 NameError 静默失败）。
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
                await _try_resume()
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
                await _try_resume()
                return
            except Exception as e:
                logger.warning("[%s] poll error: %s", run_id, e)

        # 超出时长预算：先强制杀容器再标记失败。
        # 此前只写 DB 不杀容器——训练容器会变成「僵尸」继续吃满 CPU/内存，
        # 直到宿主 OOM 或被别的机制拖垮（实测残留 3h+/19GB 的 GRU 容器）。
        try:
            c = self.docker.containers.get(container_id)
            c.reload()
            if c.attrs["State"].get("Status") in ("running", "created", "paused"):
                logger.warning(
                    "[%s] deadline exceeded, killing container %s",
                    run_id, container_id[:12],
                )
                await asyncio.to_thread(c.kill)
                await asyncio.to_thread(c.remove, {"force": True, "v": True})
        except docker.errors.NotFound:
            pass
        except Exception as kill_err:
            logger.warning("[%s] kill container after timeout failed: %s", run_id, kill_err)

        async with get_session() as db:
            r = await db.get(TrainingJobRecord, run_id)
            if r and r.status not in ("completed", "failed"):
                r.status = "failed"
                r.logs = (r.logs or "") + f"[TIMEOUT] {max_time_minutes}min limit exceeded\n"
                r.progress = 100
                await db.commit()
                self.log_stream.append_log(
                    run_id=run_id,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    line=f"[TIMEOUT] {max_time_minutes}min limit exceeded",
                    status="failed",
                    progress=100,
                    container_id=container_id[:12],
                )
        await _try_resume()


    # ── 多周期训练编排（一次训练产出多周期模型 + 自动融合）───────────────────────
    async def launch_multi_horizon_job(
        self,
        parent_run_id: str,
        child_run_ids: list[str],
        payload: dict,
    ) -> None:
        """串行跑多个周期的训练任务，全部成功后自动创建 ICIR 加权融合模型。

        每个 child 是一个独立单周期训练任务（已有完整 Docker 容器 + 回调闭环）。
        编排器按顺序依次启动，等待每个 child 完成（或失败），再推进下一个。
        全部成功 → 调 register_ensemble_model 生成「多周期融合模型」。
        """
        from backend.shared.database_manager_v2 import get_session
        from backend.services.api.routers.admin.db import TrainingJobRecord
        from backend.shared.model_registry import model_registry_service

        tenant_id = str(payload.get("_tenant_id") or "")
        user_id = str(payload.get("_user_id") or "")
        # 从 parent record 读取归属
        if not tenant_id or not user_id:
            async with get_session() as db:
                parent_rec = await db.get(TrainingJobRecord, parent_run_id)
                if parent_rec:
                    tenant_id = str(parent_rec.tenant_id or "default")
                    user_id = str(parent_rec.user_id or "")
        display_name = str(payload.get("display_name") or "multi_horizon")

        async def _set_parent(status: str, progress: int, log_line: str) -> None:
            async with get_session() as db:
                r = await db.get(TrainingJobRecord, parent_run_id)
                if r:
                    r.status = status
                    r.progress = progress
                    r.logs = (r.logs or "") + log_line
                    await db.commit()
                self.log_stream.append_log(
                    run_id=parent_run_id,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    line=log_line.strip(),
                    status=status,
                    progress=progress,
                )

        try:
            await _set_parent("provisioning", 5, f"[MH] 多周期训练启动，共 {len(child_run_ids)} 个周期\n")

            completed_model_ids: list[str] = []
            completed_run_ids: set[str] = set()
            horizon_labels: list[str] = []
            n_total = len(child_run_ids)

            for idx, child_run_id in enumerate(child_run_ids):
                # 读取 child payload（含固定 target_horizon_days）
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
                await _set_parent(
                    "running",
                    base_progress,
                    f"[MH] ({idx + 1}/{n_total}) 训练 T+{horizon} 模型…\n",
                )

                # 启动 child 训练（内部会启容器 + 等回调 + 注册模型）
                await self.launch_training_job(run_id=child_run_id, payload=child_payload)

                # 等待 child 完成
                # 等待上限跟随 child 的时长预算（+10min 冗余）：
                # 原硬编码 7200s 会在大预算 child（如 12h）超 2h 时被误判超时
                try:
                    child_budget_minutes = max(10, int(child_payload.get("max_time_minutes") or 120))
                except Exception:
                    child_budget_minutes = 120
                child_deadline = time.time() + (child_budget_minutes + 10) * 60
                while time.time() < child_deadline:
                    await asyncio.sleep(_POLL_INTERVAL)
                    async with get_session(read_only=True) as db:
                        r = await db.get(TrainingJobRecord, child_run_id)
                        if r is None:
                            # 回调正在并发更新该行时的瞬时读异常：按未完成继续轮询
                            # （与 _poll_container 回调等待的容忍语义一致）。
                            # 此前直接 break 会在 child 刚完成的提交瞬间把暂时
                            # 读不到的记录误判为超时，导致 multi-horizon 首个
                            # child 完成后必然失败（实测两天两例同款）
                            continue
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

                # 注意用 run_id 判断完成：completed_model_ids 里存的是模型 ID
                # （mdl_cn_...），此前拿 child_run_id 与之比较恒不相等，
                # 导致首个 child 成功后必然误报 "timed out"
                if child_run_id not in completed_run_ids:
                    raise RuntimeError(f"child T+{horizon} timed out waiting for completion")

                await _set_parent(
                    "running",
                    5 + int(((idx + 1) / n_total) * 90),
                    f"[MH] T+{horizon} 模型训练完成（{idx + 1}/{n_total}）\n",
                )

            # ── 全部完成 → 创建融合模型 ──
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
            fusion_model_id = str(fusion.get("model_id") or "")

            await _set_parent(
                "completed",
                100,
                f"[MH] 融合模型已创建: {fusion_model_id}（ICIR 加权，周期: {'+'.join(horizon_labels)}）\n",
            )

            # 把融合模型信息 + 最丰富的一个 child 完整结果写入 parent result，
            # 保证前端 parseTrainingResult 能正常解析（metrics + artifacts 必需）
            async with get_session() as db:
                r = await db.get(TrainingJobRecord, parent_run_id)
                if r:
                    child_results = []
                    for child_run_id in child_run_ids:
                        child_rec = await db.get(TrainingJobRecord, child_run_id)
                        if child_rec and isinstance(child_rec.result, dict):
                            child_results.append(
                                {
                                    "run_id": child_run_id,
                                    "target_horizon_days": int(
                                        (child_rec.request_payload or {}).get("target_horizon_days") or 0
                                    ),
                                    "result": child_rec.result,
                                }
                            )
                    # 选 metrics 最完整的 child 作为展示基底
                    base_result: dict = {}
                    for cr in child_results:
                        m = (cr.get("result") or {}).get("metrics") or {}
                        if m.get("train") and m.get("val") and m.get("test"):
                            base_result = cr["result"]
                            break
                    parent_result = dict(base_result)
                    parent_result["status"] = "completed"
                    parent_result["multi_horizon"] = {
                        "horizons": horizon_labels,
                        "child_run_ids": child_run_ids,
                        "child_model_ids": completed_model_ids,
                        "fusion_model_id": fusion_model_id,
                        "child_results": child_results,
                    }
                    if isinstance(parent_result.get("metadata"), dict):
                        parent_result["metadata"]["multi_horizon"] = {
                            "horizons": horizon_labels,
                            "fusion_model_id": fusion_model_id,
                        }
                    r.result = parent_result
                    await db.commit()
        except Exception as exc:  # noqa: BLE001
            logger.error("[%s] multi-horizon orchestration failed: %s", parent_run_id, exc)
            await _set_parent(
                "failed",
                100,
                f"[MH] 多周期训练失败: {exc}\n",
            )
