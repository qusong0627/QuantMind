"""通用小工具（P1 由 train.py 拆出，逐行搬运）。

硬件探测与 JSON 清洗：无训练依赖，主流程与诊断共用。
"""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

logger = logging.getLogger("quantmind.train")


def detect_hardware() -> dict[str, Any]:
    """检测运行环境的硬件配置（CPU、内存、GPU）。"""
    import os
    info: dict[str, Any] = {"cpu_count": os.cpu_count() or 1, "gpu_available": False, "gpu_count": 0, "gpu_name": "", "mem_total_gb": 0.0}
    try:
        import psutil
        info["mem_total_gb"] = round(psutil.virtual_memory().total / (1024 ** 3), 1)
    except ImportError:
        pass
    try:
        import torch
        if torch.cuda.is_available():
            info["gpu_available"] = True
            info["gpu_count"] = torch.cuda.device_count()
            info["gpu_name"] = torch.cuda.get_device_name(0) if info["gpu_count"] > 0 else ""
    except ImportError:
        pass
    logger.info("Hardware: cpu=%d, mem=%.1fGB, gpu=%s(%d), gpu_name=%s",
                info["cpu_count"], info["mem_total_gb"],
                info["gpu_available"], info["gpu_count"], info["gpu_name"])
    return info


def _sanitize_nan_inf(obj):
    """递归清洗为 JSON 可序列化结构：NaN/Inf→None，numpy 标量→原生类型，
    其余不可序列化对象（如误入的模型对象）→None，保证训练不因 metadata 序列化而失败。"""
    import math

    import numpy as np

    if isinstance(obj, dict):
        return {k: _sanitize_nan_inf(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_nan_inf(v) for v in obj]
    if isinstance(obj, (bool, str, int)) or obj is None:
        return obj
    if isinstance(obj, np.generic):
        obj = obj.item()
        if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
            return None
        return obj
    if isinstance(obj, float):
        return None if (math.isnan(obj) or math.isinf(obj)) else obj
    if isinstance(obj, (pd.Timestamp, datetime, Path)):
        return str(obj)
    try:
        import json as _json

        _json.dumps(obj)
        return obj
    except (TypeError, ValueError):
        logger.warning("metadata 中发现不可序列化对象 %s，已置空", type(obj).__name__)
        return None
