"""模拟账户 Redis 键唯一规范（纯标准库，沙箱子进程也可 import）。

键格式（CN 无后缀是历史存量，不迁移）：
    simulation:account:{tenant}:{user}            # CN / A 股
    simulation:account:{tenant}:{user}:{MARKET}   # HK / US / ...
    simulation:settings:{tenant}:{user}

所有读写必须经本模块，禁止各处手写 f-string，避免跨链路 key 漂移
（T+1 解锁扫错账户、资金快照漏市场账户、沙箱读到空账户）。
"""

from __future__ import annotations

ACCOUNT_KEY_PREFIX = "simulation:account:"
SETTINGS_KEY_PREFIX = "simulation:settings:"


def normalize_tenant(tenant_id: str | None) -> str:
    return (tenant_id or "").strip() or "default"


def normalize_market(market: str | None) -> str:
    """CN（含 A/A_SHARE/空）归一为 CN，其余大写原样返回。"""
    market_upper = str(market or "CN").upper().strip()
    if market_upper in {"", "CN", "A", "A_SHARE"}:
        return "CN"
    return market_upper


def account_key(tenant_id: str | None, user_id: object, market: str | None = "CN") -> str:
    """构造模拟账户 Redis 键。user_id 保持调用方原样（不做 zfill/int 改写）。"""
    tenant = normalize_tenant(tenant_id)
    user = str(user_id).strip()
    if normalize_market(market) == "CN":
        return f"{ACCOUNT_KEY_PREFIX}{tenant}:{user}"
    return f"{ACCOUNT_KEY_PREFIX}{tenant}:{user}:{normalize_market(market)}"


def settings_key(tenant_id: str | None, user_id: object) -> str:
    return f"{SETTINGS_KEY_PREFIX}{normalize_tenant(tenant_id)}:{str(user_id).strip()}"


def parse_account_key(key: str) -> tuple[str, str, str] | None:
    """解析账户键 -> (tenant, user原文, market)，CN 无后缀时 market='CN'。

    5 段（带市场后缀）与 4 段（CN）都接受；其余返回 None。
    """
    parts = str(key or "").split(":")
    if len(parts) not in (4, 5):
        return None
    if parts[0] != "simulation" or parts[1] != "account":
        return None
    tenant = parts[2].strip() or "default"
    user = parts[3].strip()
    if not tenant or not user:
        return None
    market = normalize_market(parts[4]) if len(parts) == 5 else "CN"
    return tenant, user, market


# ---------------------------------------------------------------------------
# 运行时身份（沙箱三元组 / active_strategy 键）唯一规范
#
# 历史坑：active 键曾对 user_id 无条件 zfill(8)，非数字用户 admin 被写成
# 000admin；重启恢复/托管调度按键后缀反解析出 000admin 并以此提交沙箱，
# 而启动/状态查询用的是 _normalize_identity 的 admin，导致“重启后沙箱活着
# 但状态查不到”。自此所有 active 相关读写必须经本模块。
# ---------------------------------------------------------------------------

ACTIVE_STRATEGY_KEY_PREFIX = "trade:active_strategy:"


def normalize_runtime_user(raw_user_id: object) -> str:
    """运行时 user 身份：数字补零 8 位，非数字保持原样（与 _normalize_identity 同口径）。"""
    raw = str(raw_user_id or "").strip()
    if not raw:
        return raw
    return raw.zfill(8) if raw.isdigit() else raw


def normalize_runtime_tenant(tenant_id: object) -> str:
    return normalize_tenant(tenant_id)


def active_strategy_key(tenant_id: object, user_id: object) -> str:
    """构造 active_strategy 键。禁止各处手写 zfill(8)。"""
    return (
        f"{ACTIVE_STRATEGY_KEY_PREFIX}"
        f"{normalize_runtime_tenant(tenant_id)}:{normalize_runtime_user(user_id)}"
    )


def parse_active_strategy_key(key: str) -> tuple[str, str] | None:
    """解析 active 键 -> (tenant后缀, user后缀)，格式不对返回 None。"""
    parts = str(key or "").split(":")
    if len(parts) < 4:
        return None
    if parts[0] != "trade" or parts[1] != "active_strategy":
        return None
    tenant = parts[2].strip() or "default"
    user = ":".join(parts[3:]).strip()
    if not user:
        return None
    return tenant, user


def resolve_active_identity(
    *,
    tenant_suffix: object,
    user_suffix: object,
    payload: dict | None = None,
) -> tuple[str, str]:
    """由 active 键后缀 + 快照负载解析出运行时身份。

    优先负载内启动时持久化的 runtime_tenant_id/runtime_user_id；
    兼容历史键（非数字用户被 zfill 成 000admin）：8 位全 0 前缀的非数字
    后缀脱掉前导 0。数字用户保持 zfill(8)。
    """
    if isinstance(payload, dict):
        t = str(payload.get("runtime_tenant_id") or "").strip()
        u = str(payload.get("runtime_user_id") or "").strip()
        if t and u:
            return normalize_runtime_tenant(t), normalize_runtime_user(u)
    u = str(user_suffix or "").strip()
    if u and not u.isdigit() and len(u) == 8 and u.startswith("0"):
        unpadded = u.lstrip("0")
        if unpadded:
            u = unpadded
    return normalize_runtime_tenant(tenant_suffix), normalize_runtime_user(u)
