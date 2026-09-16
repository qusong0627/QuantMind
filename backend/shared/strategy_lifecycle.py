"""策略生命周期状态机（T-P3-01）——**唯一实现**，纯函数。

设计口径（`docs/统一交易栈_设计方案.md` §4.2 Strategy Spec）：
    DRAFT → VERIFIED → SIM → LIVE        （ARCHIVED 软删，可恢复）
- **SIM 入口须 VERIFIED**：策略进模拟盘前必须通过回测验证（与 qlib_app
  activate 端点现有语义一致）；
- **LIVE 入口须 SIM**：晋级实盘必须有模拟证据（门槛细则见 T-P3-05 晋级门槛总表）；
- **运行中（SIM/LIVE）参数锁**：改参数必须显式升版本（expected_version），
  防止"改线上策略参数无留痕"。

存量词表归一（strategies.status 现存 89 行）：
    ACTIVE / REPOSITORY / REPO → VERIFIED；LIVE_TRADING / TRADING → LIVE。
未知值保守回落 DRAFT（不炸存量写入，写入侧另行告警）。

消费方：`shared/strategy_storage.py`（转换校验/参数锁）、
`live_trading` 启动/停止链路（门禁 + 状态回写）。
"""

from __future__ import annotations

from typing import Any

STATUS_DRAFT = "DRAFT"
STATUS_VERIFIED = "VERIFIED"
STATUS_SIM = "SIM"
STATUS_LIVE = "LIVE"
STATUS_ARCHIVED = "ARCHIVED"

ALL_STATUSES = (
    STATUS_DRAFT,
    STATUS_VERIFIED,
    STATUS_SIM,
    STATUS_LIVE,
    STATUS_ARCHIVED,
)

# 运行中状态（参数锁 / 删除守卫 / 活跃语义共用）
RUNNING_STATUSES = frozenset({STATUS_SIM, STATUS_LIVE})

_LEGACY_STATUS_MAP: dict[str, str] = {
    "DRAFT": STATUS_DRAFT,
    "D": STATUS_DRAFT,
    "ACTIVE": STATUS_VERIFIED,
    "REPOSITORY": STATUS_VERIFIED,
    "REPO": STATUS_VERIFIED,
    "VERIFIED": STATUS_VERIFIED,
    "SIM": STATUS_SIM,
    "SIMULATION": STATUS_SIM,
    "LIVE": STATUS_LIVE,
    "LIVE_TRADING": STATUS_LIVE,
    "TRADING": STATUS_LIVE,
    "ARCHIVED": STATUS_ARCHIVED,
    "ARCHIVE": STATUS_ARCHIVED,
}

# 合法迁移表（同状态=幂等 no-op，另行放行）
_TRANSITIONS: dict[str, frozenset[str]] = {
    STATUS_DRAFT: frozenset({STATUS_VERIFIED, STATUS_ARCHIVED}),
    STATUS_VERIFIED: frozenset({STATUS_DRAFT, STATUS_SIM, STATUS_ARCHIVED}),
    STATUS_SIM: frozenset({STATUS_VERIFIED, STATUS_LIVE, STATUS_ARCHIVED}),
    STATUS_LIVE: frozenset({STATUS_VERIFIED, STATUS_ARCHIVED}),
    STATUS_ARCHIVED: frozenset({STATUS_VERIFIED}),
}

# 各模式启动允许的当前状态（同状态=重复启动幂等）
_START_ALLOWED: dict[str, frozenset[str]] = {
    "SIMULATION": frozenset({STATUS_VERIFIED, STATUS_SIM}),
    "REAL": frozenset({STATUS_SIM, STATUS_LIVE}),
}


class IllegalTransitionError(ValueError):
    """非法状态迁移（含跨级晋级/回退），消息含 from/to 供上层直出。"""


class StrategyLockedError(ValueError):
    """运行中（SIM/LIVE）策略的参数锁：修改内容必须显式升版本。"""


class VersionConflictError(ValueError):
    """expected_version 与当前版本不一致（乐观并发冲突）。"""


def normalize_status(raw: Any) -> str:
    """任意存量/新旧词表 → 规范状态；None/空/未知 → DRAFT（保守）。"""
    text = str(raw or "").strip().upper()
    if not text:
        return STATUS_DRAFT
    return _LEGACY_STATUS_MAP.get(text, STATUS_DRAFT)


def can_transition(current: Any, target: Any) -> bool:
    """迁移合法性（纯判定）；同状态视为幂等合法。"""
    cur = normalize_status(current)
    tgt = normalize_status(target)
    if cur == tgt:
        return True
    return tgt in _TRANSITIONS.get(cur, frozenset())


def assert_transition(current: Any, target: Any) -> None:
    """非法迁移抛 ``IllegalTransitionError``（消息带 from/to）。"""
    if not can_transition(current, target):
        raise IllegalTransitionError(
            f"非法策略状态迁移: {normalize_status(current)} → {normalize_status(target)}"
            f"（允许: {sorted(_TRANSITIONS.get(normalize_status(current), set()))}）"
        )


def is_running(status: Any) -> bool:
    """运行中状态（SIM/LIVE）——参数锁/删除守卫共用口径。"""
    return normalize_status(status) in RUNNING_STATUSES


def requires_version_bump(status: Any) -> bool:
    """运行中策略改参数必须显式升版本（参数锁）。"""
    return is_running(status)


def can_start(current: Any, mode: Any) -> tuple[bool, str]:
    """启动门禁（纯函数）→ (是否允许, 拒绝原因)。

    SIMULATION：须 VERIFIED（或 SIM 重复启动）；
    REAL：须 SIM（或 LIVE 重复启动）——晋级实盘必须有模拟证据。
    """
    cur = normalize_status(current)
    mode_key = str(mode or "").strip().upper()
    allowed = _START_ALLOWED.get(mode_key)
    if allowed is None:
        return False, f"不支持的模式: {mode}"
    if cur in allowed:
        return True, ""
    if mode_key == "SIMULATION":
        if cur == STATUS_DRAFT:
            return False, "策略未通过验证（DRAFT）：先完成回测验证后再启动模拟"
        if cur == STATUS_LIVE:
            return False, "策略正在实盘运行（LIVE）：请先停止实盘再进模拟"
        return False, f"当前状态 {cur} 不允许启动模拟"
    # REAL
    if cur == STATUS_VERIFIED:
        return False, "策略尚未模拟验证（VERIFIED）：REAL 须先经模拟盘（SIM）验证"
    if cur == STATUS_DRAFT:
        return False, "策略未通过验证（DRAFT）：先完成回测与模拟验证"
    return False, f"当前状态 {cur} 不允许启动实盘"
