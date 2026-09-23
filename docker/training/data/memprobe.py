"""训练进程内存探针：在关键阶段打印 RSS，供 OOM(ExitCode 137) 事后定位。

为什么需要：训练容器被杀是 **SIGKILL**，stdout 缓冲里的日志一并丢失——2026-09-23
387 特征那轮 OOM，落库日志停在「Direct QuantDB read」一行，完全看不出死在哪一步
（读取 / 池过滤 / 切分 / 喂模型矩阵）。本模块只读 /proc/self/status，不产生任何
行为变化，开销可忽略。

用法：
    from data.memprobe import log_rss
    log_rss("read_range 返回")            # 单点
    with rss_stage("读全量因子"):          # 进入/退出各打一次，异常也打
        df = reader.read_range(...)

分段命名建议带上行数与列数（`log_rss` 的 detail 参数），便于把峰值换算成
「每列每行字节数」核对内存模型：峰值 ≈ 特征数 × 行数 × 4B × 活帧份数。
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager

logger = logging.getLogger("quantmind.memprobe")

_GB = 1024 ** 3

# clear_refs 重置过高水位后，被重置掉的那部分峰值（见 reset_peak）。
_HWM_FLOOR = 0.0


def _vmhwm_gb() -> float:
    """内核当前的 VmHWM（可能已被 reset_peak 清零）。"""
    try:
        with open("/proc/self/status", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("VmHWM:"):
                    return int(line.split()[1]) * 1024 / _GB
    except Exception:  # noqa: BLE001
        pass
    return 0.0


def rss_gb() -> float:
    """当前进程 RSS（GB）。读不到时返回 0.0，绝不因探针本身抛错。"""
    try:
        with open("/proc/self/status", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024 / _GB
    except Exception:  # noqa: BLE001 — 探针不得影响训练
        pass
    return 0.0


def peak_gb() -> float:
    """进程历史峰值 RSS（VmHWM，GB）——比瞬时值更能抓到 OOM 前的顶点。

    `reset_peak()` 之后内核计数会归零，故与重置前记录的高水位取大值，
    保证全程峰值仍单调可读。
    """
    return max(_vmhwm_gb(), _HWM_FLOOR)


def reset_peak() -> float:
    """把内核的 VmHWM 归零到当前 RSS，返回归零前的值（GB）。

    **为什么需要**：VmHWM 进程内单调，不归零就只能读到「相对历史最高点还差多少」，
    逐步归因会把因果搞反——2026-09-23 那轮就是把读取段的 47.4G 继承值记到了
    预处理行上。归零后每一步的 `peak_gb() - rss_before` 才是**该步自己的峰值**。

    重置不影响 cgroup 的 `memory.peak`（docker stats / memwatch 读的是那个），
    因此容器级监控不受影响；`peak_gb()` 也已把旧高水位记进 `_HWM_FLOOR` 兜底。
    """
    global _HWM_FLOOR
    before = _vmhwm_gb()
    if before > _HWM_FLOOR:
        _HWM_FLOOR = before
    try:
        with open("/proc/self/clear_refs", "w", encoding="ascii") as fh:
            fh.write("5")  # CLEAR_REFS_MM_HIWATER_RSS
    except Exception:  # noqa: BLE001 — 探针不得影响训练
        pass
    return before


def log_rss(tag: str, detail: str = "", *, level: int = logging.INFO) -> float:
    """打印一行 `[mem] <tag> rss=..G peak=..G <detail>`，返回当前 RSS。"""
    cur = rss_gb()
    logger.log(level, "[mem] %s rss=%.1fG peak=%.1fG %s", tag, cur, peak_gb(), detail)
    return cur


@contextmanager
def rss_stage(tag: str, detail: str = ""):
    """阶段计时 + 进出各打一次内存（异常路径也打，便于定位 OOM 前最后阶段）。"""
    t0 = time.time()
    log_rss(f"{tag} 开始", detail)
    try:
        yield
    finally:
        log_rss(f"{tag} 结束", f"耗时 {time.time() - t0:.1f}s {detail}")


__all__ = ["log_rss", "peak_gb", "reset_peak", "rss_gb", "rss_stage"]
