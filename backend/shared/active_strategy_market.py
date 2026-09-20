"""活跃策略市场判定（唯一口径，T-RC-15）。

活跃策略键 ``trade:active_strategy:{tenant}:{user}`` **不带市场维度**——一个用户
同时只有一个活跃策略，属于哪个市场只能从载荷里读。此前该判定只写在
``services/api/routers/desk.py``，``/real-trading/status`` 只能自造一套或干脆没有，
前端在港股页签看到 A 股运行态却无从判断。这里上提为共享实现，两处委托调用。

优先级与启动链路一致（``real_trading_lifecycle`` 启动时
``deployment_market = live_config.market or exec_config.market or "CN"``）。
"""

from __future__ import annotations

from typing import Any

from backend.shared.simulation_account_keys import normalize_market

#: 载荷里查找市场字段的顺序（与启动链路 deployment_market 推导一致）
_MARKET_FIELDS = ("live_trade_config", "execution_config")


def resolve_active_strategy_market(payload: dict[str, Any] | None) -> tuple[str, str]:
    """返回 ``(市场, 来源)``。

    来源 ∈ ``live_trade_config`` / ``execution_config`` / ``default``——只返回市场
    会把「载荷根本没写市场、兜底成 CN」伪装成「策略确实是 A 股」，前端无从分辨，
    故必须同时给出出处。
    """
    data = payload if isinstance(payload, dict) else {}
    for field in _MARKET_FIELDS:
        cfg = data.get(field)
        if isinstance(cfg, dict) and str(cfg.get("market") or "").strip():
            return normalize_market(cfg.get("market")), field
    return "CN", "default"


def active_strategy_market(payload: dict[str, Any] | None) -> str:
    """活跃策略所属市场（纯函数）。市场值，不看来源时用这个。"""
    return resolve_active_strategy_market(payload)[0]


def strategy_declared_market(strategy: dict[str, Any] | None) -> str | None:
    """策略自身声明的市场（``strategies.parameters.market``）；未声明返回 None。

    与活跃策略载荷的市场是**两个不同的东西**：前者是策略"生来属于哪个市场"，
    后者是"这次以哪个市场的口径在跑"。两者不一致本身就是一种需要暴露的异常。
    """
    if not isinstance(strategy, dict):
        return None
    params = strategy.get("parameters")
    if not isinstance(params, dict):
        return None
    raw = str(params.get("market") or "").strip()
    if not raw:
        return None
    return normalize_market(raw)


def market_gate(declared: str | None, active: str) -> dict[str, Any] | None:
    """页签市场 vs 运行策略市场的闸门判定；未声明市场时返回 None（不判定）。

    与 desk 的「无声明不判定」纪律一致：调用方没说自己要看哪个市场，就不该
    凭空判定它不匹配。
    """
    if not str(declared or "").strip():
        return None
    want = normalize_market(declared)
    matched = want == active
    return {
        "declared": want,
        "active": active,
        "matched": matched,
        "reason": ""
        if matched
        else f"当前运行策略为 {active} 市场，与所选页签 {want} 不一致，交易按 {active} 口径执行",
    }
