"""因子挖掘硬件门槛：低于 8 核 / 32GB 直接拒绝，避免 RD-Agent 把整机拖死。"""

from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_MIN_CPU_CORES = 8
DEFAULT_MIN_MEMORY_GB = 32.0
_UNLIMITED_CGROUP_MEMORY = 1 << 62


class HardwareLockError(RuntimeError):
    """当前机器不满足因子挖掘最低硬件要求。"""


@dataclass(frozen=True)
class HardwareProbe:
    cpu_cores: int
    memory_bytes: int
    min_cpu_cores: int
    min_memory_gb: float
    ok: bool
    message: str

    @property
    def memory_gb(self) -> float:
        return self.memory_bytes / 1_000_000_000


def _read_text(path: str) -> str | None:
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except Exception:
        return None


def _cgroup_cpu_cores() -> float | None:
    raw = _read_text("/sys/fs/cgroup/cpu.max")
    if raw:
        parts = raw.split()
        if len(parts) >= 2 and parts[0] != "max":
            try:
                return int(parts[0]) / int(parts[1])
            except (TypeError, ValueError, ZeroDivisionError):
                return None
    quota = _read_text("/sys/fs/cgroup/cpu/cpu.cfs_quota_us")
    period = _read_text("/sys/fs/cgroup/cpu/cpu.cfs_period_us")
    if quota and period:
        try:
            q, p = int(quota), int(period)
            if q > 0 and p > 0:
                return q / p
        except (TypeError, ValueError, ZeroDivisionError):
            return None
    return None


def _cgroup_memory_bytes() -> int | None:
    for path in (
        "/sys/fs/cgroup/memory.max",
        "/sys/fs/cgroup/memory/memory.limit_in_bytes",
    ):
        raw = _read_text(path)
        if not raw or raw == "max":
            continue
        try:
            value = int(raw)
        except ValueError:
            continue
        if 0 < value < _UNLIMITED_CGROUP_MEMORY:
            return value
    return None


def _host_cpu_cores() -> int:
    try:
        import psutil

        n = psutil.cpu_count(logical=True)
        if n:
            return int(n)
    except Exception:
        pass
    return int(os.cpu_count() or 1)


def _host_memory_bytes() -> int:
    try:
        import psutil

        return int(psutil.virtual_memory().total)
    except Exception:
        pass
    raw = _read_text("/proc/meminfo")
    if raw:
        for line in raw.splitlines():
            if line.startswith("MemTotal:"):
                try:
                    return int(line.split()[1]) * 1024
                except (IndexError, ValueError):
                    break
    return 0


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _min_cpu_cores() -> int:
    try:
        return max(1, int(os.getenv("ALPHA_AGENT_MIN_CPU_CORES", DEFAULT_MIN_CPU_CORES)))
    except (TypeError, ValueError):
        return DEFAULT_MIN_CPU_CORES


def _min_memory_gb() -> float:
    try:
        return max(1.0, float(os.getenv("ALPHA_AGENT_MIN_MEMORY_GB", DEFAULT_MIN_MEMORY_GB)))
    except (TypeError, ValueError):
        return DEFAULT_MIN_MEMORY_GB


def probe_hardware() -> HardwareProbe:
    """采集可见 CPU/内存（宿主机与 cgroup 限额取更严者）。"""
    cpu = _host_cpu_cores()
    cgroup_cpu = _cgroup_cpu_cores()
    if cgroup_cpu is not None:
        cpu = max(1, min(cpu, math.floor(cgroup_cpu)))

    memory = _host_memory_bytes()
    cgroup_mem = _cgroup_memory_bytes()
    if cgroup_mem is not None:
        memory = min(memory, cgroup_mem) if memory > 0 else cgroup_mem

    min_cpu = _min_cpu_cores()
    min_mem_gb = _min_memory_gb()
    memory_gb = memory / 1_000_000_000
    skipped = _env_flag("ALPHA_AGENT_SKIP_HW_LOCK")
    ok = skipped or (cpu >= min_cpu and memory_gb >= min_mem_gb)
    if skipped:
        message = (
            f"硬件锁已跳过（ALPHA_AGENT_SKIP_HW_LOCK）：当前 {cpu} 核 / {memory_gb:.1f} GB"
        )
    elif ok:
        message = f"硬件满足因子挖掘门槛：{cpu} 核 / {memory_gb:.1f} GB（要求 {min_cpu} 核 / {min_mem_gb:.0f} GB）"
    else:
        message = (
            f"因子挖掘已中止：当前机器 {cpu} 核 / {memory_gb:.1f} GB，"
            f"低于最低门槛 {min_cpu} 核 / {min_mem_gb:.0f} GB。"
            "继续运行会导致整机卡死。"
        )
    return HardwareProbe(
        cpu_cores=cpu,
        memory_bytes=memory,
        min_cpu_cores=min_cpu,
        min_memory_gb=min_mem_gb,
        ok=ok,
        message=message,
    )


def assert_factor_mining_hardware() -> HardwareProbe:
    """不满足门槛则抛 HardwareLockError，满足则返回探测结果。"""
    probe = probe_hardware()
    if probe.ok:
        logger.info("[alpha-agent] %s", probe.message)
        return probe
    logger.error("[alpha-agent] %s", probe.message)
    raise HardwareLockError(probe.message)
