"""2026-10-28 实盘资金策略：3 个智能体 × ¥20 万、单笔上限 ¥20 万。

纯函数层（``decision_round_policy``）的契约：

* 档位给出默认值；显式覆盖只在**合法**时生效；
* 非法覆盖**回退档位**并留 note（不静默、不把 15 当 15%）；
* ``allow_out_of_pool`` / ``profit_first`` 只认 ``"true"``（全项目唯一布尔口径）。
"""

from __future__ import annotations

import pytest

from backend.services.trade.services.decision_round_policy import (
    ENV_ALLOW_OUT_OF_POOL,
    ENV_MAX_NEW_BUYS,
    ENV_PER_STOCK_PCT,
    ENV_PROFIT_FIRST,
    capital_policy,
    filter_affordable_pool,
    tier_with_policy,
)
from backend.shared.decision.context import PoolRow
from backend.shared.decision.gates import BuyGate


class _Tier:
    """最小鸭子类型：``capital_policy`` 只读 ``budget``。"""

    def __init__(self, **budget):
        self.budget = budget


# --------------------------------------------------------------------------
# capital_policy：档位 + 覆盖
# --------------------------------------------------------------------------


def test_defaults_when_no_tier_and_no_env() -> None:
    p = capital_policy(None, env={})
    assert p.per_stock_pct == 0.20
    assert p.max_new_buys == 3
    assert p.allow_out_of_pool is False
    assert p.profit_first is False


def test_tier_values_used_when_no_override() -> None:
    p = capital_policy(_Tier(per_stock_pct=0.15, max_new_buys=2), env={})
    assert p.per_stock_pct == 0.15
    assert p.max_new_buys == 2


def test_explicit_override_wins_and_leaves_a_note() -> None:
    """用户口径：单票 100%（= 剩余额度 ¥20 万）、单轮 9 只。"""
    p = capital_policy(
        _Tier(per_stock_pct=0.10, max_new_buys=1),
        env={ENV_PER_STOCK_PCT: "1.0", ENV_MAX_NEW_BUYS: "9"},
    )
    assert p.per_stock_pct == 1.0
    assert p.max_new_buys == 9
    assert any(ENV_PER_STOCK_PCT in n for n in p.notes)
    assert any(ENV_MAX_NEW_BUYS in n for n in p.notes)


@pytest.mark.parametrize("raw", ["1.5", "0", "-0.2", "abc", "nan"])
def test_invalid_pct_falls_back_to_tier_with_note(raw: str) -> None:
    p = capital_policy(
        _Tier(per_stock_pct=0.10, max_new_buys=1),
        env={ENV_PER_STOCK_PCT: raw},
    )
    assert p.per_stock_pct == 0.10
    assert any(ENV_PER_STOCK_PCT in n for n in p.notes)


@pytest.mark.parametrize("raw", ["-1", "abc", "2.5"])
def test_invalid_buys_falls_back_to_tier_with_note(raw: str) -> None:
    p = capital_policy(
        _Tier(per_stock_pct=0.10, max_new_buys=1),
        env={ENV_MAX_NEW_BUYS: raw},
    )
    assert p.max_new_buys == 1
    assert any(ENV_MAX_NEW_BUYS in n for n in p.notes)


@pytest.mark.parametrize("raw", ["true", " true ", "TRUE"])
def test_flags_are_on_for_the_only_true_token(raw: str) -> None:
    p = capital_policy(
        None,
        env={ENV_ALLOW_OUT_OF_POOL: raw, ENV_PROFIT_FIRST: raw},
    )
    assert p.allow_out_of_pool is True
    assert p.profit_first is True


@pytest.mark.parametrize("raw", ["1", "yes", "on", "false", ""])
def test_flags_stay_off_for_everything_else(raw: str) -> None:
    p = capital_policy(
        None,
        env={ENV_ALLOW_OUT_OF_POOL: raw, ENV_PROFIT_FIRST: raw},
    )
    assert p.allow_out_of_pool is False
    assert p.profit_first is False


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


def test_tier_with_policy_writes_the_effective_knobs() -> None:
    """提示词档位块必须与闸门同值，否则模型会按 10%/1 只自我收紧。"""
    from backend.shared.risk.tiers import TierState

    tier = TierState(
        level="defensive",
        label="防守",
        budget={"per_stock_pct": 0.10, "max_new_buys": 1},
        source="producer",
    )
    p = capital_policy(
        tier, env={ENV_PER_STOCK_PCT: "1.0", ENV_MAX_NEW_BUYS: "9"}
    )
    eff = tier_with_policy(tier, p)
    assert eff.budget["per_stock_pct"] == 1.0
    assert eff.budget["max_new_buys"] == 9
    assert eff.source == "producer+policy"
    # 原档位不被就地改写（不可变纪律）
    assert tier.budget["per_stock_pct"] == 0.10


def test_tier_with_policy_is_identity_without_override() -> None:
    """没有显式覆盖时原样返回：历史提示词的档位块逐字节不变。"""
    from backend.shared.risk.tiers import TierState

    tier = TierState(level="calm", label="平静", budget={"per_stock_pct": 0.2})
    assert tier_with_policy(tier, capital_policy(tier, env={})) is tier
