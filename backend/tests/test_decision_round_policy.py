"""阶段 1：决策轮资金策略适配（档位块 + 候选池裁剪）。

资金参数的唯一出处是 ``backend.shared.trading_policy``；本文件只测两件适配：

* ``tier_with_policy``：策略两个旋钮写回档位块；值一致时原样返回（金样不破）；
* ``filter_affordable_pool``：买不起一手的票不进模型视野。
"""

from __future__ import annotations

from types import SimpleNamespace

from backend.services.trade.services.decision_round_policy import (
    filter_affordable_pool,
    tier_with_policy,
)
from backend.shared.decision.context import PoolRow
from backend.shared.decision.gates import BuyGate
from backend.shared.risk.tiers import TierState
from backend.shared.trading_policy import AgentPolicy


def _tier(**budget) -> TierState:
    return TierState(
        level="defensive",
        label="防守",
        budget=budget,
        source="producer",
    )


def test_tier_with_policy_is_identity_when_values_match() -> None:
    tier = _tier(per_stock_pct=0.20, max_new_buys=3)
    assert (
        tier_with_policy(tier, AgentPolicy(per_stock_pct=0.20, max_new_buys=3))
        is tier
    )


def test_tier_with_policy_writes_effective_knobs() -> None:
    """提示词档位块必须与闸门同值，否则模型会按 10%/1 只自我收紧。"""
    tier = _tier(per_stock_pct=0.10, max_new_buys=1)
    eff = tier_with_policy(tier, AgentPolicy(per_stock_pct=1.0, max_new_buys=9))
    assert eff.budget["per_stock_pct"] == 1.0
    assert eff.budget["max_new_buys"] == 9
    assert eff.source == "producer+policy"
    # 原档位不被就地改写（不可变纪律）
    assert tier.budget["per_stock_pct"] == 0.10


def test_tier_with_policy_handles_plain_test_doubles() -> None:
    tier = SimpleNamespace(budget={"per_stock_pct": 0.1}, source="doc", level="x")
    eff = tier_with_policy(tier, AgentPolicy(per_stock_pct=1.0, max_new_buys=9))
    assert eff.budget["per_stock_pct"] == 1.0
    assert eff.source == "doc+policy"
    assert eff.level == "x"


# --------------------------------------------------------------------------
# filter_affordable_pool：买不起一手的票不进模型视野
# --------------------------------------------------------------------------


def _gate() -> BuyGate:
    return BuyGate(
        pool_codes=frozenset({"600036.SH", "000001.SZ"}),
        per_stock_pct=1.0,
    )


def test_unaffordable_rows_are_dropped_and_affordable_kept() -> None:
    rows = [
        PoolRow(code="600036.SH", name="招商银行"),
        PoolRow(code="000001.SZ", name="平安银行"),
    ]
    prices = {"600036.SH": 30.0, "000001.SZ": 3000.0}
    kept, kept_rows, dropped = filter_affordable_pool(
        pool_rows=rows,
        pool_prices=prices,
        gate=_gate(),
        budget=5000.0,
        round_id="rnd-test",
    )
    assert [r.code for r in kept] == ["600036.SH"]
    assert len(kept_rows) == 1
    assert [str(r["code"]) for r, _ in dropped] == ["000001.SZ"]


def test_empty_pool_is_not_an_error() -> None:
    kept, kept_rows, dropped = filter_affordable_pool(
        pool_rows=[],
        pool_prices={},
        gate=_gate(),
        budget=5000.0,
        round_id="rnd-test",
    )
    assert kept == [] and kept_rows == [] and dropped == []
