# -*- coding: utf-8 -*-
"""今日行情缓存配置（从 bridge_config.json 的 today_cache.* 读取）。"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}


def _as_bool(value, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    return bool(default)


def _as_float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _as_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _get(config_get, path: str, default=None):
    """兼容 CFG.get(path, default) 与普通 dict。"""
    if callable(config_get):
        try:
            return config_get(path, default)
        except TypeError:
            return config_get(path)
        except Exception:  # noqa: BLE001 - 配置读取失败按默认
            return default
    if isinstance(config_get, dict):
        node = config_get
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node
    return default


@dataclass(frozen=True)
class TodayCacheConfig:
    enabled: bool = True
    dir: str = "../data/cache/qmt"
    snapshot_interval_s: float = 3.0
    chunk: int = 1000
    universe_key: str = ""
    sector_name: str = "沪深A股"
    max_symbols: int = 6000
    max_age_s: float = 5.0
    part_interval_s: float = 300.0
    part_max_rows: int = 200000
    bar1m_retention_days: int = 30
    write_queue_max: int = 200
    compress: str = "zstd"
    spool_fallback: bool = True
    memory_limit_mb: int = 300
    disk_min_free_gb: float = 5.0
    session_only: bool = True
    startup_grace_s: float = 8.0
    max_bars_per_symbol: int = 320
    query_max_symbols: int = 600
    hot_enabled: bool = True
    hot_max_symbols: int = 800
    hot_tick_max_per_symbol: int = 1200
    hot_tick_min_interval_s: float = 1.0
    hot_tick_stale_s: float = 20.0
    hot_query_max_symbols: int = 800
    hot_tick_retention_days: int = 10
    snapshot3s_enabled: bool = True
    snapshot3s_retention_days: int = 5

    @classmethod
    def from_config(cls, config_get, base_dir: str) -> "TodayCacheConfig":
        data = {
            "enabled": _as_bool(_get(config_get, "today_cache.enabled", True), True),
            "dir": str(_get(config_get, "today_cache.dir", "../data/cache/qmt")),
            "snapshot_interval_s": _as_float(
                _get(config_get, "today_cache.snapshot_interval_s", 3.0), 3.0
            ),
            "chunk": _as_int(_get(config_get, "today_cache.chunk", 1000), 1000),
            "universe_key": str(
                _get(config_get, "today_cache.universe_key", "") or ""
            ),
            "sector_name": str(
                _get(config_get, "today_cache.sector_name", "沪深A股") or "沪深A股"
            ),
            "max_symbols": _as_int(
                _get(config_get, "today_cache.max_symbols", 6000), 6000
            ),
            "max_age_s": _as_float(
                _get(config_get, "today_cache.max_age_s", 5.0), 5.0
            ),
            "part_interval_s": _as_float(
                _get(config_get, "today_cache.part_interval_s", 300.0), 300.0
            ),
            "part_max_rows": _as_int(
                _get(config_get, "today_cache.part_max_rows", 200000), 200000
            ),
            "bar1m_retention_days": _as_int(
                _get(config_get, "today_cache.bar1m_retention_days", 30), 30
            ),
            "write_queue_max": _as_int(
                _get(config_get, "today_cache.write_queue_max", 200), 200
            ),
            "compress": str(
                _get(config_get, "today_cache.compress", "zstd") or "zstd"
            ).lower(),
            "spool_fallback": _as_bool(
                _get(config_get, "today_cache.spool_fallback", True), True
            ),
            "memory_limit_mb": _as_int(
                _get(config_get, "today_cache.memory_limit_mb", 300), 300
            ),
            "disk_min_free_gb": _as_float(
                _get(config_get, "today_cache.disk_min_free_gb", 5.0), 5.0
            ),
            "session_only": _as_bool(
                _get(config_get, "today_cache.session_only", True), True
            ),
            "startup_grace_s": _as_float(
                _get(config_get, "today_cache.startup_grace_s", 8.0), 8.0
            ),
            "max_bars_per_symbol": _as_int(
                _get(config_get, "today_cache.max_bars_per_symbol", 320), 320
            ),
            "query_max_symbols": _as_int(
                _get(config_get, "today_cache.query_max_symbols", 600), 600
            ),
            "hot_enabled": _as_bool(
                _get(config_get, "today_cache.hot_enabled", True), True
            ),
            "hot_max_symbols": _as_int(
                _get(config_get, "today_cache.hot_max_symbols", 800), 800
            ),
            "hot_tick_max_per_symbol": _as_int(
                _get(config_get, "today_cache.hot_tick_max_per_symbol", 1200), 1200
            ),
            "hot_tick_min_interval_s": _as_float(
                _get(config_get, "today_cache.hot_tick_min_interval_s", 1.0), 1.0
            ),
            "hot_tick_stale_s": _as_float(
                _get(config_get, "today_cache.hot_tick_stale_s", 20.0), 20.0
            ),
            "hot_query_max_symbols": _as_int(
                _get(config_get, "today_cache.hot_query_max_symbols", 800), 800
            ),
            "hot_tick_retention_days": _as_int(
                _get(config_get, "today_cache.hot_tick_retention_days", 10), 10
            ),
            "snapshot3s_enabled": _as_bool(
                _get(config_get, "today_cache.snapshot3s_enabled", True), True
            ),
            "snapshot3s_retention_days": _as_int(
                _get(config_get, "today_cache.snapshot3s_retention_days", 5), 5
            ),
        }
        data = _clamp(data)
        path = data["dir"]
        if not os.path.isabs(path):
            path = os.path.join(base_dir, path)
        data["dir"] = os.path.abspath(os.path.normpath(path))
        return cls(**data)

    def status(self) -> dict:
        return asdict(self)


def _clamp(data: dict) -> dict:
    """边界校验：坏的配置值收敛到安全范围，不抛异常（桥不能因配置退出）。"""
    data["snapshot_interval_s"] = min(60.0, max(1.0, data["snapshot_interval_s"]))
    data["chunk"] = min(6000, max(100, data["chunk"]))
    data["max_symbols"] = min(10000, max(100, data["max_symbols"]))
    data["max_age_s"] = min(60.0, max(1.0, data["max_age_s"]))
    data["part_interval_s"] = min(3600.0, max(30.0, data["part_interval_s"]))
    data["part_max_rows"] = min(1000000, max(1000, data["part_max_rows"]))
    data["bar1m_retention_days"] = min(365, max(1, data["bar1m_retention_days"]))
    data["write_queue_max"] = min(10000, max(1, data["write_queue_max"]))
    data["memory_limit_mb"] = min(4096, max(64, data["memory_limit_mb"]))
    data["disk_min_free_gb"] = min(100.0, max(0.0, data["disk_min_free_gb"]))
    data["startup_grace_s"] = min(120.0, max(0.0, data["startup_grace_s"]))
    data["max_bars_per_symbol"] = min(600, max(60, data["max_bars_per_symbol"]))
    data["query_max_symbols"] = min(6000, max(1, data["query_max_symbols"]))
    data["hot_max_symbols"] = min(2000, max(1, data["hot_max_symbols"]))
    data["hot_tick_max_per_symbol"] = min(
        10000, max(60, data["hot_tick_max_per_symbol"])
    )
    data["hot_tick_min_interval_s"] = min(
        60.0, max(0.0, data["hot_tick_min_interval_s"])
    )
    data["hot_tick_stale_s"] = min(600.0, max(1.0, data["hot_tick_stale_s"]))
    data["hot_query_max_symbols"] = min(
        2000, max(1, data["hot_query_max_symbols"])
    )
    data["hot_tick_retention_days"] = min(
        60, max(1, data["hot_tick_retention_days"])
    )
    data["snapshot3s_retention_days"] = min(
        30, max(1, data["snapshot3s_retention_days"])
    )
    if data["compress"] not in {"zstd", "snappy", "gzip", "none"}:
        data["compress"] = "zstd"
    return data
