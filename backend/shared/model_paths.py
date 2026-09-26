"""模型目录的唯一解析入口。

背景：`os.getenv("MODELS_PRODUCTION", "/app/models/production")` 原先散落在 7 处
（注册表、交易预检、回放信号生成、回放路由、脚本推理器、推理路由），改路径要逐个
搜改；更麻烦的是兜底目录的默认值在两处口径不一致：

- `model_registry` / `router_service`：`MODELS_FALLBACK_PRODUCTION` 未配置 → 空串（= 无兜底）
- `script_runner`：未配置 → 回落生产目录

本模块把默认值收到一处，并把上面这个差异变成显式参数，避免"看代码才知道"。
"""

from __future__ import annotations

import os

# 容器内生产模型根目录的默认值（docker-compose 通过 ./models 卷挂载到这里）
DEFAULT_MODELS_PRODUCTION = "/app/models/production"


def models_production_dir() -> str:
    """系统/生产模型根目录，由 MODELS_PRODUCTION 覆盖。"""
    return os.getenv("MODELS_PRODUCTION", DEFAULT_MODELS_PRODUCTION)


def models_fallback_production_dir(*, default_to_production: bool = False) -> str:
    """兜底模型根目录，由 MODELS_FALLBACK_PRODUCTION 覆盖。

    未配置时默认返回空串（= 无兜底），与 model_registry / router_service 的既有语义一致。
    需要"未配置则回落生产目录"的调用方（script_runner）显式传
    ``default_to_production=True``。
    """
    raw = os.getenv("MODELS_FALLBACK_PRODUCTION", "")
    if raw:
        return raw
    return models_production_dir() if default_to_production else ""
