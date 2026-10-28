"""决策轮的资金策略适配（阶段 1）：档位展示 + 候选池资金裁剪。

资金参数的**唯一出处**是 :mod:`backend.shared.trading_policy`（``TradingPolicy``）。
本模块不再解析任何环境变量，只做两件与「一轮怎么跑」有关的纯适配：

* :func:`tier_with_policy` —— 把 ``AgentPolicy`` 的两个旋钮写回**档位块**，保证
  提示词里的「单票比例/单轮新开仓」与 ``BuyGate`` 实际夹取值同值；策略值与档位
  一致时原样返回（历史提示词逐字节不变）。
* :func:`filter_affordable_pool` —— 候选池按「买得起一手」裁剪；整池被剔光时
  按规则分流留痕（缺现价 vs 买不起一手是两件不同的事）。

2026-10-28 阶段 1 之前的 ``capital_policy``/``QM_DECISION_*`` 环境覆盖已退役：
配置只认 ``config/trading_policy.json``（或 ``QM_TRADING_POLICY_FILE``）。
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

from backend.shared.trading_policy import AgentPolicy

logger = logging.getLogger(__name__)


def tier_with_policy(tier: Any, agent: AgentPolicy) -> Any:
    """生效档位 = 原档位 + 策略里的两个旋钮（提示词档位块与闸门同值）。

    生产 ``TierState`` 是 frozen dataclass，用 ``replace``；测试替身可能只是
    带 ``budget/source`` 的普通对象——保留一条同形拷贝路径。

    策略值与档位**完全一致**时原样返回（历史提示词逐字节不变，金样不破）。
    """
    budget = getattr(tier, "budget", None) or {}
    if (
        budget.get("per_stock_pct") == agent.per_stock_pct
        and budget.get("max_new_buys") == agent.max_new_buys
    ):
        return tier
    from dataclasses import is_dataclass, replace

    new_budget = {
        **budget,
        "per_stock_pct": agent.per_stock_pct,
        "max_new_buys": agent.max_new_buys,
    }
    source = f"{getattr(tier, 'source', '')}+policy"
    if is_dataclass(tier) and not isinstance(tier, type):
        return replace(tier, budget=new_budget, source=source)
    from types import SimpleNamespace

    copy = {
        key: getattr(tier, key)
        for key in ("level", "label", "reasons", "problems", "date", "inputs")
        if hasattr(tier, key)
    }
    return SimpleNamespace(budget=new_budget, source=source, **copy)


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


__all__ = ["filter_affordable_pool", "tier_with_policy"]
