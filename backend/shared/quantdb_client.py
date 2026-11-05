"""QuantDB SDK 客户端工厂（B1）。

统一所有 QuantDB 调用的超时/重试/Key 读取，避免同步脚本与远程 adapter
各用一套配置（同步曾是 (15,300)/3，adapter 用 SDK 默认 (5,60)/2）。

Key 每次调用时从 runtime_secrets 读取：管理台换 Key 后，下一次同步/查询
无需重启进程。客户端不做跨调用缓存，避免旧 Key 的 client 被长期复用。
"""

from __future__ import annotations

import os
from typing import Any, Literal

Purpose = Literal["sync", "adapter", "admin"]

# purpose -> (connect/read timeout, max_retries)
PURPOSE_LIMITS: dict[str, tuple[tuple[int, int], int]] = {
    # 同步要下载大分区（V5 宽表单文件可达数百 MB），读写超时放宽
    "sync": ((15, 300), 3),
    # 管理台/远程 adapter 的在线查询：避免请求挂太久
    "adapter": ((15, 120), 3),
    "admin": ((15, 120), 3),
}


def get_client(purpose: Purpose | str = "sync") -> Any:
    """构造 QuantDBClient；未配置 Key 时抛 RuntimeError。"""
    from quantdb_sdk import QuantDBClient  # type: ignore

    from backend.shared.runtime_secrets import get_secret

    api_key = get_secret("QUANTDB_API_KEY")
    if not api_key:
        raise RuntimeError("QUANTDB_API_KEY 未配置")
    timeout, retries = PURPOSE_LIMITS.get(str(purpose), PURPOSE_LIMITS["admin"])
    kwargs: dict[str, Any] = {
        "api_key": api_key,
        "timeout": timeout,
        "max_retries": retries,
    }
    host = os.getenv("QUANTDB_API_HOST", "").strip()
    if host:
        kwargs["api_host"] = host
    return QuantDBClient(**kwargs)
