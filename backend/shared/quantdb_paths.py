"""QuantDB 本地数据目录解析（唯一事实源）。

为什么需要它：docker 栈把数据挂在 ``/data/quantdb`` 并设 ``QM_QUANTDB_DATA_DIR``，
便携包（免 Docker）用 ``$STORAGE_ROOT/quantdb``。凡硬编码绝对路径的模块在便携包上
都会读空 → 静默降级（行业全变「其他」、position_score 失真、行业映射失效）。
需要直接读 QuantDB parquet 的模块都应经此解析，不要再写字面量。

语义与 ``quantdb_hub._resolve_data_dir()`` 逐条等价（该函数已委托到本模块）：
  1. 环境变量 ``QM_QUANTDB_DATA_DIR``（要求目录存在且**非空**）
  2. ``/data/quantdb``（Docker 容器内挂载点）
  3. ``/app/data/quantdb``（Docker 容器内备用）
  4. ``D:/quant_data``（Windows 本地开发常用盘符）
  5. 仓库根 ``data/quantdb``（便携包 / 源码运行）

「非空」很关键：便携包首启会 mkdir 出空 quantdb 目录，空目录必须继续回退，
否则会把「回退 + warning」变成「静默空查询」。

本模块只依赖标准库，供 api / engine / trade 各服务安全 import（勿反向依赖
``backend.services.*``，否则与 quantdb_hub 的委托成环）。
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

QUANTDB_DATA_DIR_ENV = "QM_QUANTDB_DATA_DIR"

# backend/shared/quantdb_paths.py → 仓库根（便携包 $ROOT、Docker /app）
_PROJECT_ROOT = Path(__file__).resolve().parents[2]

# 默认数据目录（容器内绝对路径 / 本地盘符 / 项目根相对）
_DEFAULT_DATA_DIRS = [
    "/data/quantdb",  # Docker 容器内（挂载点）
    "/app/data/quantdb",  # Docker 容器内
    "D:/quant_data",  # Windows 本地开发常用盘符
    str(_PROJECT_ROOT / "data" / "quantdb"),  # 项目根/data/quantdb
]


def _is_usable(path: Path) -> bool:
    try:
        return path.is_dir() and any(path.iterdir())
    except OSError:  # 权限不足等：视作不可用，继续回退
        return False


def resolve_quantdb_dir() -> Path:
    """解析 QuantDB 数据目录。全部候选落空时返回项目根候选，便于报错定位。"""
    env_val = os.getenv(QUANTDB_DATA_DIR_ENV, "").strip()
    if env_val:
        p = Path(env_val)
        if _is_usable(p):
            return p
        logger.warning("%s=%s 不存在或为空，尝试默认路径", QUANTDB_DATA_DIR_ENV, env_val)

    for d in _DEFAULT_DATA_DIRS:
        p = Path(d)
        if _is_usable(p):
            return p

    fallback = _PROJECT_ROOT / "data" / "quantdb"
    if _is_usable(fallback):
        return fallback

    # 最后返回默认路径（让调用方报错更清晰）
    return Path(_DEFAULT_DATA_DIRS[-1])


def resolve_quantdb_subdir(*parts: str) -> Path:
    """QuantDB 数据目录下的子路径（不检查存在性，由调用方决定如何降级）。"""
    return resolve_quantdb_dir().joinpath(*parts)
