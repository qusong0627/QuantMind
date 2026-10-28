"""实盘智能体交易策略的**单一配置出处**（阶段 1）。

设计：`docs/实盘智能体交易策略重构_设计与分阶段_20261028.md` §2。

为什么要有这个模块
------------------
2026-10-28 的口径（3×¥20 万、单笔 ¥20 万、股票不限、盈利优先）最初是补丁式落地：
额度在 `agent_ledger` 的 env、单票/新开仓在 `runtime.env` 的覆盖、镜像在 Redis
`mirror:config`、池外/盈利开关又各一个 env。同一个参数散在五层，任何一层的漂移
都会让「模型看到的额度」与「闸门执行的额度」不是同一个数（这正是 kill criteria
G1/G3 要消灭的形态）。

本模块把 **agent 级**（额度/单票/新开仓/池外/盈利优先/分数榜条数）与 **账户级**
（镜像五项上限）收敛成一份不可变配置：

* 缺文件 → 内置默认（开源/单测可跑，不报错）；
* 文件存在但 JSON 坏、字段未知、数值非法 → ``TradingPolicyError``（**失败关闭**：
  决策轮 abort，不静默回退到更松/更紧的默认值）；
* per-agent 只写差异，其余继承 ``default``；键按 ``normalize_agent().lower()`` 归一；
* 按 ``(path, mtime_ns, size)`` 做进程内缓存——改文件下一轮生效，不必重启；
* **旧 env 一律不读**（``QM_AGENT_QUOTA`` / ``QM_DECISION_*`` 已退役）；镜像的
  ``MIRROR_MAX_*`` 只在**没有策略文件**时作为开源兜底。

本模块是叶子：只读环境/文件，不连 Redis/DB，不 import services。
"""

from __future__ import annotations

import json
import math
import os
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from backend.shared.order_contract import normalize_agent

#: 策略文件路径覆盖。容器内 ``config/`` 是挂载卷，重建容器不丢。
ENV_POLICY_FILE = "QM_TRADING_POLICY_FILE"

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


class TradingPolicyError(ValueError):
    """策略文件不可用（JSON 坏/字段未知/数值非法）。调用方必须失败关闭。"""


@dataclass(frozen=True, slots=True)
class AgentPolicy:
    """单个智能体的交易策略（¥ 元 / 比例 0–1）。"""

    quota: float = 100_000.0
    per_stock_pct: float = 0.20
    max_new_buys: int = 3
    allow_out_of_pool: bool = False
    profit_first: bool = False
    #: 【模型分数榜】条数；0 = 不渲染（开源/默认路径提示词逐字节不变）。
    score_leaders_limit: int = 0


@dataclass(frozen=True, slots=True)
class MirrorPolicy:
    """真单镜像的五项硬上限（账户级，不按 agent 分）。"""

    max_order_value: float = 10_000.0
    max_daily_value: float = 50_000.0
    max_daily_symbols: int = 5
    max_daily_orders: int = 20
    max_slippage_pct: float = 0.02


@dataclass(frozen=True, slots=True)
class TradingPolicy:
    """一份完整的交易策略（agent 级 + 账户级）。"""

    default: AgentPolicy = field(default_factory=AgentPolicy)
    agents: Mapping[str, AgentPolicy] = field(default_factory=dict)
    mirror: MirrorPolicy = field(default_factory=MirrorPolicy)
    #: ``builtin`` 或策略文件路径（审计/状态键里能看出这轮用的是哪份）。
    source: str = "builtin"
    problems: tuple[str, ...] = ()

    def for_agent(self, agent: str) -> AgentPolicy:
        """agent → 生效策略（未列出的 agent 用 ``default``）。"""
        return self.agents.get(policy_key(agent), self.default)


def policy_key(agent: str) -> str:
    """agent 身份 → 策略键（与账本/名册同一 ``normalize_agent``，再折大小写）。"""
    return normalize_agent(agent).lower()


def policy_path() -> Path:
    """策略文件路径：env 覆盖 → 仓库/容器 ``config/trading_policy.json``。"""
    env = os.getenv(ENV_POLICY_FILE, "").strip()
    if env:
        return Path(env)
    for candidate in (
        _PROJECT_ROOT / "config" / "trading_policy.json",
        Path("/app/config/trading_policy.json"),
    ):
        if candidate.is_file():
            return candidate
    return _PROJECT_ROOT / "config" / "trading_policy.json"


_cache_lock = threading.Lock()
_cache_key: tuple[str, int, int] | None = None
_cache_policy: TradingPolicy | None = None


def clear_cache() -> None:
    """清进程内缓存（测试/运维改文件后立即生效用）。"""
    global _cache_key, _cache_policy
    with _cache_lock:
        _cache_key = None
        _cache_policy = None


def load_trading_policy(path: str | Path | None = None) -> TradingPolicy:
    """读一次策略（缓存按文件 mtime/size）。

    文件不存在 → ``TradingPolicy(source="builtin")``；文件存在但不可信 →
    :class:`TradingPolicyError`（**不**吞成默认值）。
    """
    global _cache_key, _cache_policy
    file_path = Path(path) if path is not None else policy_path()
    try:
        is_file = file_path.is_file()
    except OSError as exc:
        raise TradingPolicyError(f"交易策略文件不可访问: {file_path}: {exc}") from exc
    if not is_file:
        return TradingPolicy(source="builtin")
    try:
        stat = file_path.stat()
    except OSError as exc:
        raise TradingPolicyError(f"交易策略文件不可读: {file_path}: {exc}") from exc
    key = (str(file_path), int(stat.st_mtime_ns), int(stat.st_size))
    with _cache_lock:
        if _cache_key == key and _cache_policy is not None:
            return _cache_policy
    try:
        raw = json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TradingPolicyError(
            f"交易策略文件不可解析: {file_path}: {type(exc).__name__}: {exc}"
        ) from exc
    policy = parse_trading_policy(raw, source=str(file_path))
    with _cache_lock:
        _cache_key = key
        _cache_policy = policy
    return policy


_ALLOWED_TOP = frozenset({"default", "agents", "mirror"})
_AGENT_FIELDS = frozenset(AgentPolicy.__dataclass_fields__)
_MIRROR_FIELDS = frozenset(MirrorPolicy.__dataclass_fields__)


def parse_trading_policy(
    raw: Any, *, source: str = ""
) -> TradingPolicy:
    """原始 JSON 对象 → :class:`TradingPolicy`（**严格**校验，未知即错）。"""
    if not isinstance(raw, Mapping):
        raise TradingPolicyError(
            f"交易策略顶层必须是对象（收到 {type(raw).__name__}）"
        )
    unknown = set(raw) - _ALLOWED_TOP
    if unknown:
        raise TradingPolicyError(f"交易策略含未知顶层字段: {sorted(unknown)}")

    default = _parse_agent(raw.get("default", {}), base=AgentPolicy(), where="default")

    agents_raw = raw.get("agents", {})
    if not isinstance(agents_raw, Mapping):
        raise TradingPolicyError(
            f"agents 必须是对象（收到 {type(agents_raw).__name__}）"
        )
    agents: dict[str, AgentPolicy] = {}
    for name, value in agents_raw.items():
        text = str(name or "").strip()
        if not text:
            raise TradingPolicyError("agents 含空键")
        key = policy_key(text)
        if key in agents:
            raise TradingPolicyError(
                f"agents 键归一后重复: {text!r}（归一为 {key!r}）"
            )
        agents[key] = _parse_agent(
            value, base=default, where=f"agents.{text}"
        )

    mirror = _parse_mirror(raw.get("mirror", {}), base=MirrorPolicy())
    return TradingPolicy(
        default=default,
        agents=agents,
        mirror=mirror,
        source=source or "builtin",
    )


def _parse_agent(raw: Any, *, base: AgentPolicy, where: str) -> AgentPolicy:
    if not isinstance(raw, Mapping):
        raise TradingPolicyError(
            f"{where} 必须是对象（收到 {type(raw).__name__}）"
        )
    unknown = set(raw) - _AGENT_FIELDS
    if unknown:
        raise TradingPolicyError(f"{where} 含未知字段: {sorted(unknown)}")
    values: dict[str, Any] = {}
    for key in _AGENT_FIELDS:
        if key not in raw:
            continue
        val = raw[key]
        if key == "quota":
            values[key] = _positive_float(val, f"{where}.quota")
        elif key == "per_stock_pct":
            values[key] = _pct(val, f"{where}.per_stock_pct")
        elif key == "max_new_buys":
            values[key] = _non_negative_int(val, f"{where}.max_new_buys")
        elif key == "score_leaders_limit":
            values[key] = _non_negative_int(
                val, f"{where}.score_leaders_limit", maximum=100
            )
        elif key in ("allow_out_of_pool", "profit_first"):
            if not isinstance(val, bool):
                raise TradingPolicyError(
                    f"{where}.{key} 必须是布尔值（收到 {type(val).__name__}）"
                )
            values[key] = val
    return replace(base, **values)


def _parse_mirror(raw: Any, *, base: MirrorPolicy) -> MirrorPolicy:
    if not isinstance(raw, Mapping):
        raise TradingPolicyError(f"mirror 必须是对象（收到 {type(raw).__name__}）")
    unknown = set(raw) - _MIRROR_FIELDS
    if unknown:
        raise TradingPolicyError(f"mirror 含未知字段: {sorted(unknown)}")
    values: dict[str, Any] = {}
    for key in _MIRROR_FIELDS:
        if key not in raw:
            continue
        val = raw[key]
        if key in ("max_order_value", "max_daily_value"):
            values[key] = _positive_float(val, f"mirror.{key}")
        elif key in ("max_daily_symbols", "max_daily_orders"):
            values[key] = _non_negative_int(val, f"mirror.{key}")
        elif key == "max_slippage_pct":
            pct = _float(
                val, f"mirror.{key}"
            )
            if pct < 0 or pct > 1:
                raise TradingPolicyError(
                    f"mirror.{key} 必须在 [0, 1]（收到 {val!r}）"
                )
            values[key] = pct
    return replace(base, **values)


def _float(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TradingPolicyError(
            f"{where} 必须是数字（收到 {type(value).__name__}）"
        )
    out = float(value)
    if not math.isfinite(out):
        raise TradingPolicyError(f"{where} 必须是有限数（收到 {value!r}）")
    return out


def _positive_float(value: Any, where: str) -> float:
    out = _float(value, where)
    if out <= 0:
        raise TradingPolicyError(f"{where} 必须 > 0（收到 {value!r}）")
    return out


def _pct(value: Any, where: str) -> float:
    out = _float(value, where)
    if out <= 0 or out > 1:
        raise TradingPolicyError(f"{where} 必须在 (0, 1]（收到 {value!r}）")
    return out


def _non_negative_int(value: Any, where: str, *, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TradingPolicyError(
            f"{where} 必须是整数（收到 {type(value).__name__}）"
        )
    if isinstance(value, float) and not value.is_integer():
        raise TradingPolicyError(f"{where} 必须是整数（收到 {value!r}）")
    out = int(value)
    if out < 0:
        raise TradingPolicyError(f"{where} 必须 ≥ 0（收到 {value!r}）")
    if maximum is not None and out > maximum:
        raise TradingPolicyError(f"{where} 必须 ≤ {maximum}（收到 {value!r}）")
    return out


__all__ = [
    "ENV_POLICY_FILE",
    "AgentPolicy",
    "MirrorPolicy",
    "TradingPolicy",
    "TradingPolicyError",
    "clear_cache",
    "load_trading_policy",
    "parse_trading_policy",
    "policy_key",
    "policy_path",
]
