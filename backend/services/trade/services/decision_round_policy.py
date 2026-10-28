"""本轮资金策略（纯函数）：档位 + 显式覆盖 → 单票比例 / 新开仓上限 / 开关。

2026-10-28 用户口径（实盘智能体）：

* 3 个智能体 × ¥20 万 = ¥60 万；
* 单笔上限 ¥20 万（= 单个 agent 的授予额度）；
* 「股票不限」：允许买候选池之外的 A 股；
* 盈利优先（在可控回撤下最大化净值增长，同时保留止损/熔断/ST/T+1 等硬约束）。

为什么单独立文件
----------------
``decision_round_core`` 已顶到 800 行预算（用户规则：单文件 < 800 行），而这三块
配置属于「一轮用什么参数跑」，与「一轮怎么编排」无关。拆出来后：
``core`` 继续当纯契约叶子，本模块只依赖 ``core.tier_numbers`` 与标准库。

纪律：覆盖只在**合法**时生效；非法值回退档位并留一条 note——静默吞掉会让
「配了但没生效」变成永久谜团（与档位层 ``problems`` 同取向）。
"""

from __future__ import annotations

import logging
import math
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

#: 单票比例覆盖（0, 1]：1.0 = 单票可打满该 agent 的剩余额度（¥20 万）。
ENV_PER_STOCK_PCT = "QM_DECISION_PER_STOCK_PCT"
#: 单轮新开仓只数覆盖（≥0；0 = 沿用既有「不另设上限」语义）。
ENV_MAX_NEW_BUYS = "QM_DECISION_MAX_NEW_BUYS"
#: 「股票不限」：允许买入候选池之外的 A 股（代码/名称/黑名单/停牌/涨跌停/额度
#: 仍逐条判）。默认关 = 历史行为不变。
ENV_ALLOW_OUT_OF_POOL = "QM_DECISION_ALLOW_OUT_OF_POOL"
#: 盈利优先提示词段（目标函数与退出条件）。默认关 = 历史提示词逐字节不变。
ENV_PROFIT_FIRST = "QM_DECISION_PROFIT_FIRST"

DEFAULT_POLICY_PER_STOCK_PCT = 0.20
DEFAULT_POLICY_MAX_NEW_BUYS = 3


@dataclass(frozen=True, slots=True)
class CapitalPolicy:
    """本轮生效的资金策略（档位 → 显式覆盖 → 渲染/闸门参数）。"""

    per_stock_pct: float
    max_new_buys: int
    allow_out_of_pool: bool = False
    profit_first: bool = False
    #: 单票比例/新开仓是否被显式覆盖（决定提示词档位块要不要改写；未覆盖时
    #: 原样返回档位，历史提示词逐字节不变）。
    overridden: bool = False
    #: 覆盖被采用/被拒绝的留痕（进 notes，审计里能回答「这轮为什么是这个上限」）。
    notes: tuple[str, ...] = ()


def _policy_float(env: Mapping[str, str], key: str) -> tuple[float | None, str]:
    raw = str(env.get(key, "") or "").strip()
    if not raw:
        return None, ""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None, f"{key}={raw!r} 不是数字：回退档位值"
    if not math.isfinite(value) or value <= 0 or value > 1:
        return None, f"{key}={raw!r} 不是 (0, 1] 的比例：回退档位值"
    return value, ""


def _policy_int(env: Mapping[str, str], key: str) -> tuple[int | None, str]:
    raw = str(env.get(key, "") or "").strip()
    if not raw:
        return None, ""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None, f"{key}={raw!r} 不是整数：回退档位值"
    if value < 0:
        return None, f"{key}={raw!r} 不能为负：回退档位值"
    return value, ""


def _policy_bool(env: Mapping[str, str], key: str) -> bool:
    from backend.shared.env_flags import normalize_env_flag

    return normalize_env_flag(env.get(key), default=False)


def capital_policy(
    tier: Any, *, env: Mapping[str, str] | None = None
) -> CapitalPolicy:
    """档位 + 显式资金策略覆盖 → :class:`CapitalPolicy`（纯函数，不读 IO）。

    ``env`` 缺省时用进程环境（测试应显式传入，避免机器上的 ``runtime.env``
    把断言带跑）。覆盖只在合法时生效；非法值**回退档位**并留一条 note。
    """
    from backend.services.trade.services.decision_round_core import tier_numbers

    tier_pct, tier_buys = tier_numbers(tier)
    src: Mapping[str, str] = os_env() if env is None else env
    notes: list[str] = []

    pct_override, pct_problem = _policy_float(src, ENV_PER_STOCK_PCT)
    if pct_problem:
        notes.append(pct_problem)
    per_stock_pct = (
        pct_override
        if pct_override is not None
        else (tier_pct if tier_pct is not None else DEFAULT_POLICY_PER_STOCK_PCT)
    )
    if pct_override is not None:
        notes.append(f"单票比例覆盖为 {per_stock_pct:.0%}（{ENV_PER_STOCK_PCT}）")

    buys_override, buys_problem = _policy_int(src, ENV_MAX_NEW_BUYS)
    if buys_problem:
        notes.append(buys_problem)
    max_new_buys = (
        buys_override
        if buys_override is not None
        else (tier_buys if tier_buys is not None else DEFAULT_POLICY_MAX_NEW_BUYS)
    )
    if buys_override is not None:
        notes.append(f"单轮新开仓覆盖为 {max_new_buys} 只（{ENV_MAX_NEW_BUYS}）")

    return CapitalPolicy(
        per_stock_pct=per_stock_pct,
        max_new_buys=max_new_buys,
        allow_out_of_pool=_policy_bool(src, ENV_ALLOW_OUT_OF_POOL),
        profit_first=_policy_bool(src, ENV_PROFIT_FIRST),
        overridden=pct_override is not None or buys_override is not None,
        notes=tuple(notes),
    )


def os_env() -> Mapping[str, str]:
    """进程环境（延迟导入 ``os`` 只为把「读环境」集中在这一处）。"""
    import os

    return os.environ


def tier_with_policy(tier: Any, policy: CapitalPolicy) -> Any:
    """生效档位 = 原档位 + 覆盖后的两个旋钮（提示词档位块与闸门同值）。

    生产 ``TierState`` 是 frozen dataclass，用 ``replace``；测试替身可能只是
    带 ``budget/source`` 的普通对象——为它保留一条同形拷贝路径，避免为了测试
    把生产类型改成可变对象。

    未发生覆盖时**原样返回**（历史提示词逐字节不变）。
    """
    if not policy.overridden:
        return tier
    from dataclasses import is_dataclass, replace

    budget = {
        **(getattr(tier, "budget", None) or {}),
        "per_stock_pct": policy.per_stock_pct,
        "max_new_buys": policy.max_new_buys,
    }
    source = f"{getattr(tier, 'source', '')}+policy"
    if is_dataclass(tier) and not isinstance(tier, type):
        return replace(tier, budget=budget, source=source)
    from types import SimpleNamespace

    copy = {
        key: getattr(tier, key)
        for key in ("level", "label", "reasons", "problems", "date", "inputs")
        if hasattr(tier, key)
    }
    return SimpleNamespace(budget=budget, source=source, **copy)


def filter_affordable_pool(
    *,
    pool_rows: Sequence[Any],
    pool_prices: Mapping[str, float | None],
    gate: Any,
    budget: float,
    round_id: str,
) -> tuple[list[Any], list[Any], list[Any]]:
    """候选池按「买得起一手」裁剪；整池被剔光时按规则分流留痕。

    返回 ``(kept_pool_rows, kept_gate_rows, dropped_rows)``。计数必须**按规则
    分流**（缺现价 vs 买不起一手）：一句「全部未过闸」会把排障引向错处。
    """
    from backend.services.trade.services.decision_round_core import (
        gate_row_to_pool_row,
        pool_row_to_gate_row,
    )
    from backend.shared.decision.gates import (
        RULE_POOL_ROW_INVALID,
        RULE_UNAFFORDABLE,
        filter_pool,
    )

    gate_rows = [
        pool_row_to_gate_row(r, pool_prices.get(str(r.code))) for r in pool_rows
    ]
    kept_rows, dropped_rows = filter_pool(gate_rows, gate, budget=budget)
    kept = [gate_row_to_pool_row(row) for row in kept_rows]
    if pool_rows and not kept:
        by_rule = Counter(
            str(getattr(verdict, "rule", "") or "") for _, verdict in dropped_rows
        )
        logger.warning(
            "[DecisionRound] %s 候选池 %d 只全部未过闸：缺可用现价 %d 只（%s）、"
            "买不起一手 %d 只（%s）（单票预算 ¥%.2f）",
            round_id,
            len(pool_rows),
            by_rule.get(RULE_POOL_ROW_INVALID, 0),
            RULE_POOL_ROW_INVALID,
            by_rule.get(RULE_UNAFFORDABLE, 0),
            RULE_UNAFFORDABLE,
            budget,
        )
    return kept, kept_rows, dropped_rows


__all__ = [
    "ENV_ALLOW_OUT_OF_POOL",
    "ENV_MAX_NEW_BUYS",
    "ENV_PER_STOCK_PCT",
    "ENV_PROFIT_FIRST",
    "CapitalPolicy",
    "capital_policy",
    "filter_affordable_pool",
    "tier_with_policy",
]
