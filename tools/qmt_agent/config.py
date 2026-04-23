"""Agent configuration, constants and load helpers."""
from __future__ import annotations

import json
import socket
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

@dataclass
class AgentConfig:
    api_base_url: str
    server_url: str
    access_key: str
    secret_key: str
    account_id: str
    account_type: str = "STOCK"
    tenant_id: str = "default"
    user_id: str = ""
    client_version: str = "1.0.0"
    client_fingerprint: str = ""
    hostname: str = ""
    qmt_path: str = ""
    qmt_bin_path: str = ""
    session_id: int = 0
    renew_before_seconds: int = 300
    heartbeat_interval_seconds: int = 15
    account_report_interval_seconds: int = 30
    reconnect_interval_seconds: int = 5
    ws_ping_interval_seconds: int = 60
    ws_ping_timeout_seconds: int = 20
    enable_short_trading: bool = False
    short_check_cache_ttl_sec: int = 30
    reconcile_lookback_seconds: int = 86400
    reconcile_max_orders: int = 200
    reconcile_max_trades: int = 200
    reconcile_cancel_after_seconds: int = 60
    order_dispatch_queue_size: int = 500
    order_submit_interval_ms: int = 50
    enable_smart_execution: bool = True
    enable_smart_for_market: bool = False
    smart_max_retries: int = 5
    smart_timeout_seconds: int = 8


_INT_CONFIG_DEFAULTS: dict[str, int] = {
    "session_id": 0,
    "renew_before_seconds": 300,
    "heartbeat_interval_seconds": 15,
    "account_report_interval_seconds": 30,
    "reconnect_interval_seconds": 5,
    "ws_ping_interval_seconds": 60,
    "ws_ping_timeout_seconds": 20,
    "short_check_cache_ttl_sec": 30,
    "reconcile_lookback_seconds": 86400,
    "reconcile_max_orders": 200,
    "reconcile_max_trades": 200,
    "reconcile_cancel_after_seconds": 60,
    "order_dispatch_queue_size": 500,
    "order_submit_interval_ms": 50,
    "smart_max_retries": 5,
    "smart_timeout_seconds": 8,
}

_INT_CONFIG_MINIMUMS: dict[str, int] = {
    "session_id": 0,
    "renew_before_seconds": 30,
    "heartbeat_interval_seconds": 10,
    "account_report_interval_seconds": 20,
    "reconnect_interval_seconds": 3,
    "ws_ping_interval_seconds": 20,
    "ws_ping_timeout_seconds": 5,
    "short_check_cache_ttl_sec": 5,
    "reconcile_lookback_seconds": 60,
    "reconcile_max_orders": 1,
    "reconcile_max_trades": 1,
    "reconcile_cancel_after_seconds": 10,
    "order_dispatch_queue_size": 10,
    "order_submit_interval_ms": 0,
    "smart_max_retries": 1,
    "smart_timeout_seconds": 3,
}

_QMT_ORDER_STATUS_MAP: dict[int, str] = {
    48: "PENDING",             # 未报
    49: "SUBMITTED",           # 待报
    50: "SUBMITTED",           # 已报
    51: "CANCEL_PENDING",      # 报撤中 (原归类为 CANCELLED 不够精确)
    52: "PARTIALLY_FILLED",    # 部成待撤
    53: "CANCELLED",           # 已撤
    54: "PARTIALLY_CANCELLED", # 部撤
    55: "REJECTED",            # 废单
    56: "FILLED",              # 已成
    57: "REJECTED",            # 柜台拒单
    58: "FILLED",              # 已成
    255: "REJECTED",           # 未知废单
}


def normalize_agent_config_data(data: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(data or {})
    for key in (
        "api_base_url",
        "server_url",
        "access_key",
        "secret_key",
        "account_id",
        "tenant_id",
        "user_id",
        "client_version",
        "client_fingerprint",
        "hostname",
        "qmt_path",
        "qmt_bin_path",
        "account_type",
    ):
        if key in normalized and normalized[key] is not None:
            normalized[key] = str(normalized[key]).strip()
    normalized["enable_short_trading"] = bool(normalized.get("enable_short_trading", False))
    normalized["enable_smart_execution"] = bool(normalized.get("enable_smart_execution", True))
    normalized["enable_smart_for_market"] = bool(normalized.get("enable_smart_for_market", False))
    normalized["account_type"] = str(normalized.get("account_type") or "STOCK").strip().upper() or "STOCK"
    for key, default in _INT_CONFIG_DEFAULTS.items():
        raw = normalized.get(key, default)
        try:
            value = int(raw)
        except (TypeError, ValueError):
            value = default
        minimum = _INT_CONFIG_MINIMUMS.get(key, 1)
        normalized[key] = max(minimum, value)
    return normalized


def load_config(path: str) -> AgentConfig:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    data.setdefault("hostname", socket.gethostname())
    data.setdefault("client_fingerprint", socket.gethostname())
    data = normalize_agent_config_data(data)
    allowed_keys = {item.name for item in fields(AgentConfig)}
    filtered = {key: value for key, value in data.items() if key in allowed_keys}
    return AgentConfig(**filtered)




def validate_config_dict(data: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for key in ("api_base_url", "server_url", "access_key", "secret_key", "account_id"):
        if not str(data.get(key) or "").strip():
            errors.append(f"{key} 不能为空")
    qmt_path = str(data.get("qmt_path") or "").strip()
    if qmt_path and not Path(qmt_path).exists():
        errors.append("qmt_path 不存在")
    qmt_bin_path = str(data.get("qmt_bin_path") or "").strip()
    if qmt_bin_path and not Path(qmt_bin_path).exists():
        errors.append("qmt_bin_path 不存在")
    for key in (
        "renew_before_seconds",
        "heartbeat_interval_seconds",
        "account_report_interval_seconds",
        "reconnect_interval_seconds",
        "ws_ping_interval_seconds",
        "ws_ping_timeout_seconds",
        "short_check_cache_ttl_sec",
        "reconcile_lookback_seconds",
        "reconcile_max_orders",
        "reconcile_max_trades",
        "reconcile_cancel_after_seconds",
        "order_dispatch_queue_size",
        "order_submit_interval_ms",
        "smart_max_retries",
        "smart_timeout_seconds",
    ):
        val = data.get(key)
        if val is not None:
            try:
                if int(val) <= 0:
                    errors.append(f"{key} 必须大于 0")
            except (TypeError, ValueError):
                errors.append(f"{key} 必须为整数")
    return errors
