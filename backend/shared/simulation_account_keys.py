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

# 与 backend.shared.admin_identity.ADMIN_USER_ID 保持一致。本模块保持纯标准库，
# 供沙箱子进程 import，禁止反向依赖 admin_identity（会拉 SQLAlchemy）。
CANONICAL_ADMIN_SIM_USER = "10000001"
_ADMIN_SIM_TOKENS = frozenset({"0", "1", "00000001", "10000001", "admin"})


def normalize_tenant(tenant_id: str | None) -> str:
    return (tenant_id or "").strip() or "default"


def normalize_market(market: object) -> str:
    """CN（含 A/A_SHARE/空）归一为 CN，其余大写原样返回。

    Market 枚举按 .value 解包（``str(Market.CN)`` 是 "Market.CN" 而非 "CN"——
    曾致 ledger_account_id 生成 ":MARKET.CN" 后缀的错误 id）。
    """
    raw = getattr(market, "value", market)
    market_upper = str(raw or "CN").upper().strip()
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


def is_admin_sim_user(user_id: object) -> bool:
    """OSS 管理员模拟账户族：10000001 及历史 admin / 00000001 / 1 / 0。"""
    raw = str(user_id or "").strip()
    if raw in _ADMIN_SIM_TOKENS:
        return True
    if raw.isdigit() and str(int(raw)) in {"0", "1", "10000001"}:
        return True
    return False


def canonical_sim_user_suffix(user_id: object) -> str:
    """模拟账户 Redis 规范后缀。管理员族一律写 10000001。"""
    if is_admin_sim_user(user_id):
        return CANONICAL_ADMIN_SIM_USER
    return str(user_id or "").strip() or "0"


def _user_id_aliases(user_id: object) -> list[str]:
    """同一模拟账户可能出现的 user 后缀。

    规范 ID 是 ``10000001``（8 位且不以 0 开头，int 后不变）。历史键还有
    ``00000001``（int 成 1）、``admin`` 非数字落到 ``0``。这些必须互相能读到，
    否则仪表盘全 0、策略对着空账跑。其它数字用户（42）绝不掺进管理员族。
    """
    raw = str(user_id or "").strip()
    aliases: list[str] = []

    def _add(value: object) -> None:
        text = str(value).strip()
        if text and text not in aliases:
            aliases.append(text)

    _add(raw)
    if is_admin_sim_user(raw):
        for token in (
            CANONICAL_ADMIN_SIM_USER,
            "00000001",
            "1",
            "0",
            "admin",
        ):
            _add(token)
        return aliases

    if raw.isdigit():
        as_int = str(int(raw))
        _add(as_int)
        _add(as_int.zfill(8))
    else:
        _add("0")
    return aliases or ["0"]


def account_lookup_keys(
    tenant_id: str | None, user_id: object, market: str | None = "CN"
) -> list[str]:
    """读取账户时的候选键：调用方原样 + 数字用户的 int / zfill(8) / 管理员历史 0。"""
    seen: set[str] = set()
    keys: list[str] = []
    for candidate in _user_id_aliases(user_id):
        key = account_key(tenant_id, candidate, market)
        if key not in seen:
            seen.add(key)
            keys.append(key)
    return keys


def ledger_user_id_candidates(user_id: object) -> list[str]:
    """PG 台账/资金快照查询用的 user_id 候选。管理员带历史 ``0`` 作最后兜底。"""
    return _user_id_aliases(user_id)


def settings_key(tenant_id: str | None, user_id: object) -> str:
    return f"{SETTINGS_KEY_PREFIX}{normalize_tenant(tenant_id)}:{str(user_id).strip()}"


# ---------------------------------------------------------------------------
# PG 台账账户 id（市场化账户唯一规范）
#
# 与 Redis 账户键同构（CN 无后缀是历史存量，不迁移）：
#     sim:{tenant}:{user}            # CN
#     sim:{tenant}:{user}:{MARKET}   # HK / US / FUTURES / ...
# 账户行 cash/initial_equity 的读写一律经本函数，禁止手写 f-string。
# ---------------------------------------------------------------------------

LEDGER_ACCOUNT_PREFIX = "sim:"


def ledger_account_id(tenant_id: str | None, user_id: object, market: str | None = "CN") -> str:
    """构造 PG 台账账户 id（唯一实现；CN 无后缀→存量行零迁移）。"""
    tenant = normalize_tenant(tenant_id)
    user = str(user_id).strip()
    if normalize_market(market) == "CN":
        return f"{LEDGER_ACCOUNT_PREFIX}{tenant}:{user}"
    return f"{LEDGER_ACCOUNT_PREFIX}{tenant}:{user}:{normalize_market(market)}"


def market_from_ledger_account_id(account_id: str | None) -> str:
    """从台账账户 id 反解市场（无后缀=CN；与 account_key 的键形约定一致）。"""
    parts = str(account_id or "").split(":")
    if len(parts) >= 4 and parts[0] == "sim":
        return normalize_market(parts[3])
    return "CN"


def settings_lookup_keys(tenant_id: str | None, user_id: object) -> list[str]:
    seen: set[str] = set()
    keys: list[str] = []
    for candidate in _user_id_aliases(user_id):
        key = settings_key(tenant_id, candidate)
        if key not in seen:
            seen.add(key)
            keys.append(key)
    return keys


def account_payload_score(data: dict | None) -> tuple[int, float, float]:
    """比较别名账户谁更像真实资金：持仓数 > 总资产 > 现金。"""
    if not data:
        return (-1, -1.0, -1.0)
    positions = data.get("positions") or {}
    count = 0
    if isinstance(positions, dict):
        for pos in positions.values():
            if not isinstance(pos, dict):
                continue
            try:
                if float(pos.get("volume") or 0) > 0:
                    count += 1
            except (TypeError, ValueError):
                continue
    elif isinstance(positions, list):
        count = len(positions)
    try:
        total = float(data.get("total_asset") or 0.0)
    except (TypeError, ValueError):
        total = 0.0
    try:
        cash = float(data.get("cash") or 0.0)
    except (TypeError, ValueError):
        cash = 0.0
    return (count, total, cash)


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
    """运行时 user 身份：管理员族收口 10000001，其它数字补零 8 位，非数字保持原样。"""
    raw = str(raw_user_id or "").strip()
    if not raw:
        return raw
    if is_admin_sim_user(raw):
        return CANONICAL_ADMIN_SIM_USER
    return raw.zfill(8) if raw.isdigit() else raw


def normalize_runtime_tenant(tenant_id: object) -> str:
    return normalize_tenant(tenant_id)


def active_strategy_key(tenant_id: object, user_id: object) -> str:
    """构造 active_strategy 键。禁止各处手写 zfill(8)。"""
    return (
        f"{ACTIVE_STRATEGY_KEY_PREFIX}"
        f"{normalize_runtime_tenant(tenant_id)}:{normalize_runtime_user(user_id)}"
    )


def active_strategy_lookup_keys(tenant_id: object, user_id: object) -> list[str]:
    """读取运行态时的候选键，覆盖管理员历史后缀。"""
    tenant = normalize_runtime_tenant(tenant_id)
    seen: set[str] = set()
    keys: list[str] = []

    def _add(suffix: object) -> None:
        text = str(suffix or "").strip()
        if not text:
            return
        key = f"{ACTIVE_STRATEGY_KEY_PREFIX}{tenant}:{text}"
        if key not in seen:
            seen.add(key)
            keys.append(key)

    _add(normalize_runtime_user(user_id))
    for candidate in _user_id_aliases(user_id):
        _add(candidate)
        if candidate.isdigit():
            _add(str(int(candidate)))
            _add(str(int(candidate)).zfill(8))
    return keys


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
