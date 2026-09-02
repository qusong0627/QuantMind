"""TTL 缓存 + 手动清除。跨市场复用（对齐 A 股 quantdb_feed._cached 的口径）。"""

from __future__ import annotations

import functools
import threading
import time
from typing import Any
from collections.abc import Callable

_QUERY_TTL: float = 300.0  # 实时聚合结果默认 5 分钟缓存
_cache_lock = threading.Lock()
_cache: dict[tuple[str, ...], tuple[float, Any]] = {}


def cached(key: str, loader: Callable[[], Any], ttl: float = _QUERY_TTL) -> Any:
    """带 TTL 的查询缓存：同一 key 在 ttl 秒内直接返回，过期重新加载。

    线程安全的存储，但 loader 本身应自带幂等保护（调用方保证只读）。
    """
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    value = loader()
    with _cache_lock:
        if len(_cache) > 256:  # 防止长期运行的进程里 key 无限膨胀
            _cache.clear()
        _cache[key] = (time.monotonic(), value)
    return value


def clear_cache() -> None:
    """清空全部缓存（用于手动刷新触发）。"""
    with _cache_lock:
        _cache.clear()


def cache_stats() -> dict[str, Any]:
    """缓存统计（键数 / 最新生成时间），供 /status 或诊断端点使用。"""
    with _cache_lock:
        return {"keys": len(_cache), "ttl_seconds": _QUERY_TTL}


def lru_cache(maxsize: int = 8) -> Callable[[Callable], Callable]:
    """便捷装饰器：把函数结果包进带 TTL 的缓存桶（key = 函数名）。"""

    def decorator(func):
        def wrapper(*args, **_kwargs):
            fq = f"{func.__module__}.{func.__name__}:{args}"
            return cached(fq, lambda: func(*args, **_kwargs))

        return wrapper

    return decorator


class FakeTTL:
    """测试用：无缓存（每次必过时）。"""

    @staticmethod
    def cached(key, loader, ttl=0.0):
        return loader()
