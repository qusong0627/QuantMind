"""训练执行运行时解析：Docker 容器训练 vs 本机进程直跑（便携包免 Docker）。

模型训练历史上只支持两种编排方式：本地 Docker（LocalDockerOrchestrator，
Docker-in-Docker 起 TRAINING_IMAGE 容器）与 AutoDL 远程 SSH。便携一键启动包
（无 Docker）缺少本地执行路径，训练页「本地 Docker」节点永远离线：
  Error while fetching server API version: (2, 'CreateFile', ...)

本模块统一三处消费方的"执行环境"判定与目录约定，保证决策一致：
  1. node_manager.collect_local —— 节点就绪探测（状态面板/训练页）；
  2. orchestrator_base.get_orchestrator —— 提交训练时选编排器；
  3. model_registry._sync_candidate_artifacts —— 产物同步源目录。

规则：
- TRAINING_EXECUTOR=auto|docker|process 显式覆盖（auto 为默认）；
- auto：Docker daemon + 训练镜像就绪且 /data 可写（compose 部署语义）→
  docker；其余环境（便携包裸机、容器内 /data 不可写等）→ 本机具备训练脚本
  + 依赖即 process 直跑（Docker 编排硬编码 /data、quantmind-network）；
- 任务目录：TRAINING_JOBS_DIR 显式覆盖；容器部署沿用 /data/training_jobs
  （compose STORAGE_ROOT=/data），本机部署沿用 {STORAGE_ROOT}/training_jobs。

本模块不依赖 backend 其它包，供 backend/shared 与 services 各层导入。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

# 与 quantdb_factor_reader 的 MARKET_DATA_DIR_DEFAULT 对齐的数据根目录名，
# 仅用于本机进程直跑时定位数据（容器模式由编排器挂载决定，不在此解析）。
_MARKET_DIR_NAMES = {
    "CN": "quantdb",
    "HK": "quanthk",
    "US": "quantus",
    "CRYPTO": "quantbc",
    "FUTURES": "quantfutures",
}

# 训练脚本候选：便携包把 docker/training/train.py 复制到包根目录，
# 仓库 / 容器内则保留原 docker/training/ 相对布局。
_SCRIPT_REL_CANDIDATES = (
    "train.py",
    "docker/training/train.py",
)

# 直跑训练依赖探测（importlib find_spec，不真正导入，毫秒级）
_PROCESS_REQUIRED_TOP_PKGS = ("lightgbm", "torch", "pyarrow", "pandas", "yaml")


def training_image_name() -> str:
    """训练容器镜像名（与 local_docker_orchestrator 的 _TRAINING_IMAGE 同源）。"""
    return (os.getenv("TRAINING_IMAGE") or "quantmind-trainer:latest").strip()


def repo_root_dir() -> Path:
    """仓库根目录：backend/shared/training_runtime.py → 向上 2 层。"""
    return Path(__file__).resolve().parents[2]


def running_inside_container() -> bool:
    """是否运行在 Docker 容器内（用于保持容器版目录语义不变）。"""
    if os.path.exists("/.dockerenv"):
        return True
    try:
        cgroup = Path("/proc/1/cgroup").read_text(errors="replace")
        return "docker" in cgroup or "containerd" in cgroup
    except OSError:
        return False


def _probe_docker(timeout: float = 3.0) -> tuple[bool, bool, str | None]:
    """探测 Docker daemon 可用性与训练镜像是否已安装。

    返回 (daemon 可达, 训练镜像已安装, 错误信息)。镜像缺失不算错误——
    调用方据此决定回退本机直跑还是给出"需安装镜像"提示。
    """
    image_present = False
    try:
        import docker as docker_sdk

        client = docker_sdk.DockerClient.from_env(timeout=timeout)
        client.ping()
        try:
            client.images.get(training_image_name())
            image_present = True
        except Exception:  # noqa: BLE001 - 镜像缺失属正常状态分支
            image_present = False
        return True, image_present, None
    except Exception as exc:  # noqa: BLE001 - 任何连接失败都视为不可达
        return False, False, str(exc)


def find_training_script(root: Path | None = None) -> Path | None:
    """在本机/包根目录下寻找训练脚本 train.py，找不到返回 None。"""
    root = Path(root) if root is not None else repo_root_dir()
    for rel in _SCRIPT_REL_CANDIDATES:
        candidate = root / rel
        if candidate.is_file():
            return candidate.resolve()
    return None


def runtime_has_training_deps() -> tuple[bool, list[str]]:
    """当前 python 运行时是否具备直跑训练所需的关键依赖。

    返回 (是否齐全, 缺失包名列表)。用 find_spec 探测，避免重导入开销。
    """
    import importlib.util

    missing = [
        pkg for pkg in _PROCESS_REQUIRED_TOP_PKGS if importlib.util.find_spec(pkg) is None
    ]
    return (not missing, missing)


def resolve_training_executor(*, include_docker_error: bool = False) -> dict[str, Any]:
    """解析训练执行环境，返回状态字典。

    返回值关键字段：
    - executor: "docker" | "process" —— 最终生效的执行方式
    - force: bool —— 是否来自 TRAINING_EXECUTOR 显式覆盖
    - docker_available / image_present / docker_error：daemon 与训练镜像探测
    - script: 直跑脚本路径或 None；missing：直跑缺失的依赖包列表

    auto 规则：
    1. Docker daemon + 训练镜像就绪且 /data 挂载可写（compose 部署）→ docker
       （容器语义：任务目录 /data/training_jobs 与 quantmind-network 齐备）；
    2. 其它情形（便携包等裸机、或容器内 /data 不可写）→ 本机具备训练脚本 +
       运行时依赖即 process 直跑——LocalDockerOrchestrator 硬编码容器路径
       /data 与容器网络，不满足语义时必然 PermissionError/网络缺失；
    3. 两者都不可用 → 仍标记 docker，readiness 层给出可操作的提示文案；
    裸机要强制容器训练须显式 TRAINING_EXECUTOR=docker（自行准备数据目录
    与网络，一般仅测试用途）。
    """
    forced = (os.getenv("TRAINING_EXECUTOR") or "").strip().lower()
    if forced not in ("", "auto", "docker", "process"):
        forced = ""

    result: dict[str, Any] = {
        "executor": "process",
        "force": False,
        "docker_available": False,
        "image_present": False,
        "training_image": training_image_name(),
        "docker_error": None,
        "script": None,
        "missing": [],
    }

    # 显式覆盖：不再做 daemon/镜像探测（docker 分支编排器构造时自会校验），
    # 但直跑条件（脚本/依赖）仍如实填充，供节点就绪面板展示
    if forced in ("docker", "process"):
        result["force"] = True
        result["executor"] = forced
        result["docker_available"] = forced == "docker"
        result["image_present"] = forced == "docker"
        if forced == "process":
            _fill_process_checks(result)
        return result

    docker_ok, image_present, err = _probe_docker()
    result["docker_available"] = docker_ok
    result["image_present"] = image_present
    result["docker_error"] = None if docker_ok else (err or "Docker daemon 未运行")

    # 容器编排依赖 compose 语义：/data 挂载点可写（任务目录 /data/training_jobs
    # 由编排器创建）与 quantmind-network。仅在满足时选择 Docker；便携包等裸机
    # （或容器内 /data 不可写/不存在）即使宿主 Docker 可用也不选容器编排，
    # 避免 /data/training_jobs 创建 PermissionError 卡死训练。
    if docker_ok and image_present and _docker_compose_semantics_ready():
        result["executor"] = "docker"
        return result

    # Docker 不可用 / 无 compose 语义：本机具备直跑条件（脚本 + 依赖）即回落直跑
    _fill_process_checks(result)
    if result.get("script") is not None and not result.get("missing"):
        result["executor"] = "process"
        return result

    # 既无可用 Docker 又无直跑条件：仍标记 docker 以保留旧报错路径
    # （readiness 层会给出可操作的提示文案）
    result["executor"] = "docker"
    if not include_docker_error:
        condition_text = (
            f"（train.py: {'存在' if result.get('script') else '缺失'}；依赖: "
            + ("缺失 " + ", ".join(result.get("missing") or []) if result.get("missing") else "齐全")
            + "）"
        )
        if docker_ok and image_present:
            result["docker_error"] = (
                f"当前环境缺少容器编排所需 /data 挂载（非 compose 部署），无法使用"
                f"容器训练，且本机直跑条件不齐{condition_text}"
            )
        elif docker_ok:
            result["docker_error"] = (
                f"未安装独立训练镜像 {training_image_name()}，且本机直跑条件不齐"
                f"{condition_text}（构建镜像或让运行时具备直跑依赖）"
            )
        else:
            result["docker_error"] = (
                f"Docker daemon 未运行，且本机缺少直跑训练条件{condition_text}"
            )
    return result


def _docker_compose_semantics_ready() -> bool:
    """compose 容器语义是否齐备：/data 挂载存在且对当前用户可写。"""
    data_dir = Path("/data")
    if not data_dir.is_dir():
        return False
    try:
        return os.access("/data", os.W_OK)
    except OSError:
        return False


def _fill_process_checks(result: dict[str, Any]) -> None:
    """填充本机直跑条件：训练脚本路径与缺失依赖（find_spec 探测）。"""
    script = find_training_script()
    deps_ok, missing = runtime_has_training_deps()
    result["script"] = str(script) if script else None
    result["missing"] = missing
    result["deps_ok"] = deps_ok


def training_jobs_root() -> Path:
    """训练任务工作目录根（编排器写盘与注册同步必须同一约定）。"""
    override = (os.getenv("TRAINING_JOBS_DIR") or "").strip()
    if override:
        return Path(override).expanduser().resolve()

    storage = (os.getenv("STORAGE_ROOT") or "").strip()
    # 容器部署（compose STORAGE_ROOT=/data 且 ./data:/data 挂载）沿用
    # 既有 /data/training_jobs 语义，不因 STORAGE_ROOT 配置差异而漂移
    if storage and storage not in ("/data", ""):
        return Path(storage).expanduser().resolve() / "training_jobs"
    return Path("/data") / "training_jobs"


def training_jobs_dir(run_id: str | None = None) -> Path:
    """单个训练任务的目录；run_id 为空时返回根目录。"""
    root = training_jobs_root()
    return root / run_id if run_id else root


def local_market_data_root(market: str) -> Path | None:
    """本机进程直跑时市场数据根目录（无 Docker 挂载语义，读真实路径）。"""
    market = str(market or "CN").upper()
    env_name = f"QM_QUANT{market}_DATA_DIR" if market != "CN" else "QM_QUANTDB_DATA_DIR"
    if market == "HK":
        env_name = "QM_QUANTHK_DATA_DIR"
    elif market == "US":
        env_name = "QM_QUANTUS_DATA_DIR"
    elif market == "CRYPTO":
        env_name = "QM_QUANTBC_DATA_DIR"
    elif market == "FUTURES":
        env_name = "QM_QUANTFUTURES_DATA_DIR"
    env_val = (os.getenv(env_name) or "").strip()
    if env_val:
        return Path(env_val).expanduser()
    # 本机（非容器）开发运行兜底：{STORAGE_ROOT 或 cwd}/data/<market_dir>
    storage = (os.getenv("STORAGE_ROOT") or "").strip()
    base = Path(storage) if storage else Path.cwd()
    for candidate in (
        base / "data" / _MARKET_DIR_NAMES.get(market, "quantdb"),
        base / _MARKET_DIR_NAMES.get(market, "quantdb"),
    ):
        if candidate.is_dir():
            return candidate
    return base / "data" / _MARKET_DIR_NAMES.get(market, "quantdb")


def local_snapshot_root() -> Path | None:
    """本机进程直跑时旧式（非因子目录）特征快照目录，找不到返回 None。"""
    storage = (os.getenv("STORAGE_ROOT") or "").strip()
    candidates: list[Path] = []
    if storage:
        candidates.append(Path(storage) / "feature_snapshots")
    candidates.append(Path.cwd() / "data" / "feature_snapshots")
    candidates.append(Path.cwd() / "db" / "feature_snapshots")
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return None


def default_api_base_url() -> str:
    """训练回调默认地址：进程直跑时回到本机 API 网关。"""
    override = (os.getenv("QUANTMIND_API_BASE_URL") or "").strip()
    if override:
        return override
    port = (os.getenv("API_PORT") or "8000").strip()
    return f"http://127.0.0.1:{port}"
