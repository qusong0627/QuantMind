"""数据源包：统一行情接入。

用法：
    from agent_tools.datasources import get_datasource
    ds = get_datasource("local")
    quote = ds.get_quote("AAPL", "2025-10-30")
"""

from typing import Any, Dict, Optional

from agent_tools.datasources import base, local, tdx  # noqa: F401  (注册副作用)


def get_datasource(name: str, config: Optional[Dict[str, Any]] = None) -> base.DataSource:
    return base.registry.create(name, config)


def available_datasources() -> list[str]:
    return base.registry.available()


def get_default_datasource(backend_config: Optional[Dict[str, Any]] = None) -> base.DataSource:
    """从 backend.yaml 的 datasource.default 创建默认数据源。"""
    name = "local"
    config = None
    if backend_config:
        ds_cfg = backend_config.get("datasource", {})
        name = ds_cfg.get("default", "local")
        config = ds_cfg.get(name, {})
    return get_datasource(name, config)


__all__ = ["get_datasource", "available_datasources", "get_default_datasource"]
