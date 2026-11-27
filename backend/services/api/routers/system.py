from fastapi import APIRouter
from config.settings import settings
import asyncio
import json
import os
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from backend.shared.programmatic_trading_disclosure import (
    HFT_ORDERS_PER_DAY,
    HFT_ORDERS_PER_MINUTE,
    HFT_ORDERS_PER_SECOND,
    SOFTWARE_DEVELOPER,
    SOFTWARE_NAME,
    disclosure_lines,
    disclosure_text,
    software_version,
)
from backend.shared.version import get_version_info, get_deploy_truth, check_updates

router = APIRouter(prefix="/api/v1/system", tags=["System"])


@router.get("/version")
async def system_version(force: bool = False):
    """当前运行代码版本与上游更新检查。

    - version/commit/branch：由 deploy/update.sh 写入 version.json（build 时拷入镜像）。
    - update：可选地调用上游平台（默认 gitee）compare API 算出本部署落后提交数。
      容器无外网或未走 update.sh 时省略；force=true 可绕过缓存强制刷新。
      更新检查属增强能力，任何异常都不应影响版本读取接口。
    """
    info = get_version_info()
    try:
        update = await check_updates(force=force)
    except Exception:
        update = None
    return {
        "version": info["version"],
        "edition": settings.edition,
        "commit": info["commit"],
        "branch": info["branch"],
        "update": update,
    }


@router.get("/deploy-truth")
async def deploy_truth():
    """运行处部署真相（T7-3 / H10）：此刻实际在跑的工作树 commit/脏标记。

    与 /version 的分工：/version 读的是部署脚本落盘的**声明**（version.json，
    合规披露的版本号口径也冻结在彼）；本端点走**运行处探针**——容器与服务器
    都是 bind mount 活代码，只有工作树说了算；声明与运行分叉（热修/并行改动）
    也会一并暴露。探针只读；读不到 .git（源码包部署）时如实报 source=unknown。
    """
    return get_deploy_truth()


@router.get("/programmatic-trading-disclosure")
async def programmatic_trading_disclosure():
    """程序化交易报告义务的待填报信息（软件名称/版本号/开发者）。

    只**提供**信息，不代为报告、不做合规判定。用户按券商与交易所要求自行填报；
    口径与阈值出处见 `backend/shared/programmatic_trading_disclosure.py`。
    """
    return {
        "software_name": SOFTWARE_NAME,
        "version": software_version(),
        "developer": SOFTWARE_DEVELOPER,
        "lines": disclosure_lines(),
        "text": disclosure_text(),
        "high_frequency": {
            "orders_per_second": HFT_ORDERS_PER_SECOND,
            "orders_per_minute": HFT_ORDERS_PER_MINUTE,
            "orders_per_day": HFT_ORDERS_PER_DAY,
            "note": (
                "单账户每秒申报+撤单达 300 笔以上、或单日达 20000 笔以上，"
                "可能被认定为高频交易，须额外报告系统服务器所在地、系统测试报告"
                "与故障应急方案。本工具不代为报告。"
            ),
        },
    }


@router.get("/capabilities")
async def get_capabilities():
    """获取当前版本的系统能力与开关"""
    return settings.capabilities


# ---------------------------------------------------------------------------
# 服务器负载（后台任务坞用）
# ---------------------------------------------------------------------------
_LOAD_CACHE: dict[str, object] = {"at": 0.0, "payload": None}
_LOAD_TTL_S = 3.0
_GPU_CACHE: dict[str, object] = {"at": 0.0, "payload": None}
_GPU_TTL_S = 10.0


def _read_gpu_probe() -> dict | None:
    """容器内没有 nvidia-smi 时，读宿主机看门狗写出的 GPU 探针。

    ``scripts/host_watchdog.py`` 每分钟把 nvidia-smi 结果写到
    ``data/system/host_probe.json``（宿主机 → 容器 = /data/system/host_probe.json）。
    超过 180 秒视为过期，返回 None，不让界面显示僵尸数据。
    """
    path = os.environ.get("QM_HOST_PROBE_PATH", "/data/system/host_probe.json")
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        ts = datetime.fromisoformat(str(raw.get("ts", "")))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - ts).total_seconds()
        gpu = raw.get("gpu")
        if age > 180 or not isinstance(gpu, dict):
            return None
        return {
            "index": int(gpu.get("index", 0)),
            "name": str(gpu.get("name", "GPU")),
            "util_pct": float(gpu.get("util_pct", 0)),
            "mem_used_mb": float(gpu.get("mem_used_mb", 0)),
            "mem_total_mb": float(gpu.get("mem_total_mb", 0)),
            "temp_c": float(gpu.get("temp_c", 0)),
        }
    except Exception:  # noqa: BLE001
        return None


def _read_gpu() -> dict | None:
    """nvidia-smi 读 GPU；无卡/无驱动时返回 None（不报错）。"""
    if time.time() - float(_GPU_CACHE["at"]) < _GPU_TTL_S:
        return _GPU_CACHE["payload"]  # type: ignore[return-value]
    payload = None
    try:
        out = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=index,name,utilization.gpu,memory.used,memory.total,temperature.gpu",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if out.returncode == 0 and out.stdout.strip():
            first = out.stdout.strip().splitlines()[0]
            idx, name, util, used, total, temp = [p.strip() for p in first.split(",")]
            payload = {
                "index": int(idx),
                "name": name,
                "util_pct": float(util),
                "mem_used_mb": float(used),
                "mem_total_mb": float(total),
                "temp_c": float(temp),
            }
    except Exception:  # noqa: BLE001
        payload = None
    if payload is None:
        payload = _read_gpu_probe()
    _GPU_CACHE["at"] = time.time()
    _GPU_CACHE["payload"] = payload
    return payload


def _collect_load() -> dict:
    payload: dict = {
        "cpu_pct": None,
        "mem_used_gb": None,
        "mem_total_gb": None,
        "load1": None,
        "disk_used_pct": None,
        "gpu": None,
        "as_of": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    try:
        import psutil

        # interval=None 返回自上次调用以来的均值；首次调用先预热，避免恒为 0.0
        cpu = psutil.cpu_percent(interval=None)
        if cpu == 0.0:
            cpu = psutil.cpu_percent(interval=0.1)
        payload["cpu_pct"] = round(float(cpu), 1)
        vm = psutil.virtual_memory()
        payload["mem_used_gb"] = round(vm.used / 1e9, 1)
        payload["mem_total_gb"] = round(vm.total / 1e9, 1)
    except Exception:  # noqa: BLE001
        pass
    try:
        payload["load1"] = round(os.getloadavg()[0], 2)
    except Exception:  # noqa: BLE001
        pass
    try:
        du = shutil.disk_usage("/")
        payload["disk_used_pct"] = round(du.used / du.total * 100, 1)
    except Exception:  # noqa: BLE001
        pass
    payload["gpu"] = _read_gpu()
    return payload


@router.get("/load")
async def system_load():
    """当前服务器负载（CPU / 内存 / 磁盘 / GPU）。

    后台任务坞每几秒轮询一次，故带 3 秒进程内缓存；GPU 走 nvidia-smi 单独缓存
    10 秒（无显卡、无驱动一律返回 null，不报错、不影响其余字段）。
    """
    now = time.time()
    if now - float(_LOAD_CACHE["at"]) < _LOAD_TTL_S and _LOAD_CACHE["payload"]:
        return _LOAD_CACHE["payload"]
    payload = await asyncio.to_thread(_collect_load)
    _LOAD_CACHE["at"] = time.time()
    _LOAD_CACHE["payload"] = payload
    return payload

