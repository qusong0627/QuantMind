"""Runtime fault triage helpers for QMT Agent."""
from __future__ import annotations

from typing import Any


def classify_runtime_fault(status: dict[str, Any]) -> dict[str, str]:
    runtime_health = str(status.get("runtime_health") or "").strip().lower()
    runtime_state = str(status.get("runtime_state") or "").strip().lower()
    last_error = str(status.get("last_error") or "").strip()
    qmt_connected = bool(status.get("qmt_connected"))
    bridge_connected = bool(status.get("last_bridge_connect_at"))
    heartbeat_age = status.get("heartbeat_age_seconds")
    account_age = status.get("account_age_seconds")
    thread_states = status.get("worker_threads") or {}
    dead_threads = [name for name, alive in dict(thread_states).items() if not bool(alive)]

    layer = "正常"
    reason = "状态正常"
    action = "无需处理"

    if runtime_state == "stopped":
        layer = "本地运行态"
        reason = "Agent 已停止"
        action = "重新启动 Agent"
    elif runtime_health in {"stale", "degraded"}:
        layer = "本地运行态"
        reason = "后台线程或心跳失活"
        if heartbeat_age not in (None, "", 0):
            reason = f"{reason}（心跳 {heartbeat_age}s）"
        if account_age not in (None, "", 0):
            reason = f"{reason}（账户 {account_age}s）"
        if dead_threads:
            reason = f"{reason}（线程: {','.join(dead_threads[:3])}）"
        action = "检查桌面日志与工作线程，必要时重启 Agent"
    elif not bridge_connected and qmt_connected:
        layer = "云端连接"
        reason = "QMT 已连，但 bridge 未建立或已断开"
        action = "检查桥接会话、网络与后端 /ws/bridge"
    elif not qmt_connected:
        layer = "QMT 连接"
        reason = "QMT 本地柜台未连接"
        action = "检查 MiniQMT、xtquant 和资金账号"

    if "heartbeat report failed" in last_error or "account report failed" in last_error:
        layer = "后端上报"
        reason = last_error
        action = "检查后端接口可达性与鉴权"
    elif "session" in last_error.lower() or "bridge websocket" in last_error.lower():
        layer = "云端连接"
        reason = last_error
        action = "检查 bridge session、WS 链路与后端状态"
    elif "qmt" in last_error.lower() and layer == "正常":
        layer = "QMT 连接"
        reason = last_error
        action = "检查 MiniQMT 与本地路径配置"

    return {"layer": layer, "reason": reason, "action": action}
