"""Broker 包：统一券商接入层。

用法：
    from agent_tools.brokers import get_broker
    broker = get_broker("sandbox")          # 或 backend.yaml broker.default
    positions = broker.get_positions("gpt-5", "2025-10-30")
"""

from typing import Any, Dict, Optional

from agent_tools.brokers import base, sandbox, tdx_bridge, futu_bridge, tiger_bridge, ibkr_bridge, qmt_bridge  # noqa: F401  (注册副作用)


def get_broker(name: str, config: Optional[Dict[str, Any]] = None) -> base.Broker:
    """按名称创建 broker 实例；unknown 名称抛 BrokerError。"""
    return base.registry.create(name, config)


def available_brokers() -> list[str]:
    return base.registry.available()


def get_default_broker(backend_config: Optional[Dict[str, Any]] = None) -> base.Broker:
    """从 backend.yaml 的 broker.default 创建默认 broker。"""
    name = "sandbox"
    config = None
    if backend_config:
        broker_cfg = backend_config.get("broker", {})
        name = broker_cfg.get("default", "sandbox")
        config = broker_cfg.get(name, {})
    return get_broker(name, config)


__all__ = ["get_broker", "available_brokers", "get_default_broker"]
