"""线程安全 TTL 缓存——移植自隔壁 `backend/api_server.py` L55-97。

背景（原注释照录）：`/overview` 无缓存全量重算（3 市场 × agent × 净值/汇总），
Live 页轮询下曾两次把 api 线程池打满死锁。热接口（overview 30s / prices 10s）
加线程安全 TTL 限频：同签名在 TTL 内直接返回缓存值。

key = ``(fn.__module__, fn.__name__, *args, **kwargs)``。注意 FastAPI 场景下
参数经解析后是普通值（str/int），可哈希；`functools.wraps` 必须保留，
否则 FastAPI 看到 ``*args/**kwargs`` 会把它们当查询参数。
"""

from __future__ import annotations

import functools
import threading
import time
from typing import Any, Callable

_ttl_lock = threading.Lock()
_ttl_cache: dict[tuple, tuple[float, Any]] = {}


def ttl_cache(seconds: float) -> Callable:
    def deco(fn: Callable) -> Callable:
        key_base = (fn.__module__, fn.__name__)

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            key = key_base + args + tuple(sorted(kwargs.items()))
            now = time.monotonic()
            with _ttl_lock:
                hit = _ttl_cache.get(key)
                if hit and now - hit[0] < seconds:
                    return hit[1]
            value = fn(*args, **kwargs)
            with _ttl_lock:
                _ttl_cache[key] = (now, value)
            return value

        return wrapper

    return deco


def ttl_invalidate(prefix: tuple) -> None:
    """使 key 以 prefix 开头的缓存项失效（如数据根被修改后）。"""
    with _ttl_lock:
        for key in [k for k in _ttl_cache if k[: len(prefix)] == prefix]:
            del _ttl_cache[key]
