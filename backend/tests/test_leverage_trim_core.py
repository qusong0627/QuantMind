"""减仓执行器纯函数层（``leverage_trim_core``）的契约测试（P2.6）。

这些用例是**档位文档 → 减仓计划**这段共识的可执行版本。最要紧的三条：

1. 缺 ``leverage_max``/``leverage_trim_to`` 的档位（``FALLBACK_LIMITS`` 的形态）
   **不动手**，且是 ``idle`` 不是 ``blocked``——那是档位层的有意姿态而非故障；
2. 超限却一条腿都排不出来 = ``blocked``（该动手却动不了），原因必须落到 ``skipped``；
3. 权益/敞口读不出 = ``blocked`` 且**不下单**——不拿数据故障当卖出信号。
"""

from __future__ import annotations

import pytest

from backend.services.trade.services import leverage_trim_core as core
from backend.services.trade.services.leverage_trim_core import (
    ACTION_BLOCKED,
    ACTION_IDLE,
    ACTION_TRIM,
    LIMITS_ABSENT,
    LIMITS_DIRTY,
    LIMITS_OK,
    MAX_LEGS_PER_ROUND,
    TRIM_TO_MIN_RATIO,
    LegInput,
    TrimLimits,
    aggregate_value,
    limits_from_budget,
    plan_trim,
    trim_client_order_id,
)
from backend.shared.risk.tiers import FALLBACK_LIMITS, LEVELS

# ── 测试用常量（避免魔法数字散落）────────────────────────────────────
_EQUITY = 1_000_000.0
_DEFENSIVE = TrimLimits(1.0, 1.0, "defensive", "producer")
_CAUTION = TrimLimits(1.2, 1.15, "caution", "producer")
_MAIN = "600036.SH"  # 主板：整手 100
_STAR = "688111.SH"  # 科创板：最小申报 200
_ST = "600519.SH"


def _leg(symbol: str = _MAIN, **kwargs) -> LegInput:
    """一腿的正常默认值：有量、有价、可卖、未跌停。逐个用例只覆盖它关心的字段。"""
    base = {
        "volume": 1000.0,
        "available": 1000.0,
        "price": 100.0,
        "day_chg_ratio": 0.01,
        "limit_threshold_ratio": 0.10,
    }
    base.update(kwargs)
    return LegInput(symbol=symbol, **base)


# ── limits_from_budget：档位预算 → 减仓参数 ─────────────────────────


@pytest.mark.parametrize("level", ["calm", "caution", "defensive"])
def test_limits_from_budget_reads_every_real_tier_level(level: str) -> None:
    """三档真实文档都能直接消费，目标就是档位表里的 ``leverage_trim_to``。"""
    # Arrange
    doc = LEVELS[level]

    # Act
    read = limits_from_budget(doc, level=level, source="doc")

    # Assert
    assert read.reason == ""
    assert read.kind == LIMITS_OK
    assert read.limits is not None
    assert read.limits.leverage_max == pytest.approx(doc["leverage_max"])
    assert read.limits.target == pytest.approx(doc["leverage_trim_to"])
    assert read.limits.level == level


def test_fallback_limits_stay_unconsumable_by_design() -> None:
    """``FALLBACK_LIMITS`` 有意不含减仓键：数据故障绝不触发强减。

    这条用例钉的是 tiers.py 的设计决定（注释里写明的「强减是风险动作」）。
    谁哪天往 ``FALLBACK_LIMITS`` 里加回 ``leverage_trim_to``，这里会先红。
    """
    # Act
    read = limits_from_budget(FALLBACK_LIMITS, level="defensive", source="fallback")

    # Assert
    assert read.limits is None
    assert read.kind == LIMITS_ABSENT  # 有意姿态：静默（不告警）
    assert "未给出减仓参数" in read.reason


def test_absent_budget_is_idle_posture_not_failure() -> None:
    """从未定档（空预算）：回原因串，不抛、不报警。"""
    read = limits_from_budget({}, level="", source="absent")
    assert read.limits is None
    assert read.kind == LIMITS_ABSENT
    assert read.reason
    assert limits_from_budget(None).limits is None


@pytest.mark.parametrize(
    "budget",
    [
        {"leverage_max": 1.0, "leverage_trim_to": 0.0},
        {"leverage_max": 0.0, "leverage_trim_to": 1.0},
        {"leverage_max": -1.0, "leverage_trim_to": -0.5},
        {"leverage_max": 1.0, "leverage_trim_to": "abc"},
        {"leverage_max": None, "leverage_trim_to": 1.0},
        {"leverage_max": 1.0, "leverage_trim_to": float("nan")},
        {"leverage_max": 1.0, "leverage_trim_to": float("inf")},
    ],
)
def test_limits_from_budget_rejects_dirty_documents(budget: dict) -> None:
    """非正 / 非数 / 非有限的档位参数一律不执行（**绝不猜一个替代值**）。"""
    read = limits_from_budget(budget)
    assert read.limits is None
    assert read.reason
    assert read.kind == LIMITS_DIRTY  # 与「没给」分开：这类要喊人


def test_limits_from_budget_rejects_target_far_below_max() -> None:
    """带外目标（< 上限的一半）视为脏文档：手写 0.01 的文档会把账户减到 1%。"""
    # Act：0.4 / 1.0 恰在带外
    read = limits_from_budget({"leverage_max": 1.0, "leverage_trim_to": 0.4})

    # Assert
    assert read.limits is None
    assert read.kind == LIMITS_DIRTY
    assert "带外" in read.reason


def test_limits_from_budget_accepts_band_lower_boundary() -> None:
    """边界取闭区间：``trim_to == max × 0.5`` 放行（误伤合法的激进减仓也是错）。"""
    read = limits_from_budget(
        {"leverage_max": 1.0, "leverage_trim_to": 1.0 * TRIM_TO_MIN_RATIO}
    )
    assert read.reason == ""
    assert read.limits is not None
    assert read.limits.target == pytest.approx(0.5)


def test_limits_from_budget_clamps_target_to_max() -> None:
    """目标高于上限时夹到上限——防「减到仍超限」的强平循环。"""
    read = limits_from_budget({"leverage_max": 1.0, "leverage_trim_to": 1.3})
    assert read.limits is not None
    assert read.limits.trim_to == pytest.approx(1.3)  # 原值保留（留痕用）
    assert read.limits.target == pytest.approx(1.0)  # 生效值夹取


def test_limits_from_budget_accepts_numeric_strings() -> None:
    """Redis/JSON 往返有时把数写成字符串：能解析就认，不因此停工。"""
    read = limits_from_budget({"leverage_max": "1.2", "leverage_trim_to": "1.15"})
    assert read.reason == ""
    assert read.limits is not None
    assert read.limits.target == pytest.approx(1.15)


# ── aggregate_value：敞口两口径取大 ─────────────────────────────────


def test_aggregate_value_takes_larger_of_two_measures() -> None:
    """自报值漏腿时用逐腿重建值（保守侧取大，与闸门同向）。"""
    legs = [_leg(volume=1000.0, price=100.0)]  # 重建 100,000
    assert aggregate_value(60_000.0, legs) == pytest.approx(100_000.0)
    assert aggregate_value(reported=200_000.0, legs=legs) == pytest.approx(200_000.0)


def test_aggregate_value_rebuilds_when_account_unavailable() -> None:
    assert aggregate_value(None, [_leg(volume=200.0, price=50.0)]) == pytest.approx(
        10_000.0
    )


def test_aggregate_value_falls_back_to_broker_market_value_without_quote() -> None:
    """无行情时用柜台自报市值估值——**不用成本价**（那是另一个量纲）。"""
    leg = _leg(price=None, market_value=88_888.0)
    assert leg.value == pytest.approx(88_888.0)
    assert aggregate_value(None, [leg]) == pytest.approx(88_888.0)


def test_aggregate_value_is_zero_when_nothing_is_known() -> None:
    """两个口径都拿不到 → 0（调用点因此判「未超限」而不是拿脏数强减）。"""
    assert aggregate_value(None, [_leg(price=None, market_value=None)]) == 0.0
    assert aggregate_value(None, []) == 0.0


# ── plan_trim：先谈分母，再谈卖什么 ─────────────────────────────────

_BASE_KWARGS = {
    "equity": _EQUITY,
    "position_value": 1_300_000.0,  # 杠杆 1.3
    "limits": _DEFENSIVE,
}


def test_plan_trim_idle_without_limits_and_says_why() -> None:
    plan = plan_trim(
        equity=_EQUITY,
        position_value=1.3e6,
        legs=[_leg()],
        limits=None,
        limits_reason="档位未给出减仓参数（leverage_max/leverage_trim_to 缺一）",
    )
    assert plan.action == ACTION_IDLE
    assert "未给出减仓参数" in plan.reason
    assert plan.legs == ()


def test_plan_trim_idle_when_leverage_at_limit_inclusive() -> None:
    """等于上限放行（``>max+eps`` 才算超限），与 builtin_rules 的 ``_EPS`` 同口径。"""
    plan = plan_trim(**{**_BASE_KWARGS, "position_value": _EQUITY}, legs=[_leg()])
    assert plan.action == ACTION_IDLE
    assert plan.leverage == pytest.approx(1.0)


@pytest.mark.parametrize("equity", [None, 0.0, -1.0, float("nan"), "abc"])
def test_plan_trim_blocked_when_equity_unavailable(equity) -> None:
    """没有分母就不减仓：按假的小分母强减 = 拿数据故障当卖出信号。"""
    plan = plan_trim(**{**_BASE_KWARGS, "equity": equity}, legs=[_leg()])
    assert plan.action == ACTION_BLOCKED
    assert plan.legs == ()
    assert "权益" in plan.reason


def test_plan_trim_blocked_when_position_value_unavailable() -> None:
    plan = plan_trim(**{**_BASE_KWARGS, "position_value": None}, legs=[_leg()])
    assert plan.action == ACTION_BLOCKED
    assert plan.legs == ()


def test_plan_trim_blocked_on_contradictory_inputs() -> None:
    """敞口 ≤ 目标却判超限：不动手并说清。

    ``TrimLimits.target`` 自己夹取了 ``min(trim_to, max)``，所以这个分支在当前实现下
    **按构造不可达**（``lev > max ≥ target`` ⇒ 缺口恒正）。用例故意绕过夹取去踩它：
    将来谁把 ``target`` 改成不夹取的写法（或让档位文档直接喂进目标值），
    这里必须先红——「减到仍超限」的强平循环正是 tiers.py 注释里点名要防的东西。
    """

    # Arrange：模拟 target 不再夹取（脏文档里 trim_to > leverage_max）
    class _UnclampedLimits(TrimLimits):
        @property
        def target(self) -> float:
            return self.trim_to

    dirty = _UnclampedLimits(
        leverage_max=1.0, trim_to=2.0, level="defensive", source="doc"
    )
    assert dirty.target == pytest.approx(2.0)

    # Act
    plan = plan_trim(equity=_EQUITY, position_value=1.3e6, legs=[_leg()], limits=dirty)

    # Assert
    assert plan.action == ACTION_BLOCKED
    assert "矛盾" in plan.reason
    assert plan.legs == ()


# ── plan_trim：目标与轮内循环 ───────────────────────────────────────


def test_plan_trim_sells_to_tier_target_not_to_the_line() -> None:
    """**按档位目标减，不贴线补缺口**（与隔壁差异 ②）。

    谨慎档：上限 1.2、目标 1.15。1,300,000 敞口下缺口是 150,000（减到 1.15e6），
    不是「刚好不超限」的 100,000——后者价格一抖就再次超限，形成抖动卖出。
    """
    plan = plan_trim(
        equity=_EQUITY,
        position_value=1.3e6,
        legs=[_leg(available=5000.0)],
        limits=_CAUTION,
    )
    assert plan.action == ACTION_TRIM
    assert plan.target_value == pytest.approx(1_150_000.0)
    assert plan.need_value == pytest.approx(150_000.0)
    assert plan.legs[0].quantity == pytest.approx(1500.0)
    assert plan.remaining_value == pytest.approx(0.0)


def test_plan_trim_largest_leg_first() -> None:
    """最大腿优先（保留隔壁做对的部分）：缺口小的时候只动一条腿。"""
    small = _leg("000001.SZ", volume=100.0, available=100.0, price=10.0)  # 1,000
    big = _leg("600036.SH", volume=1000.0, available=1000.0, price=100.0)  # 100,000
    plan = plan_trim(
        **{**_BASE_KWARGS, "position_value": 1_010_000.0}, legs=[small, big]
    )
    # 缺口 = 1,010,000 − 1,000,000 = 10,000 → 只卖大腿 100 股
    assert [leg.symbol for leg in plan.legs] == ["600036.SH"]
    assert plan.legs[0].value == pytest.approx(10_000.0)


def test_plan_trim_loops_within_round_until_gap_covered() -> None:
    """**轮内循环**（与隔壁差异 ③）：一轮里排完所有腿，大缺口不必等下一拍。

    最大腿（市值 400,000）受 T+1 可用量限制只卖得动 100,000，剩下的缺口由次大腿
    当场补齐——隔壁「一轮只卖最大的一腿」在同样的账户上要 3 拍才修完（每 3s 一拍）。
    """
    # Arrange：缺口 400,000
    locked_big = _leg(
        "600036.SH", volume=4000.0, available=1000.0, price=100.0
    )  # 可卖 100,000
    second = _leg(
        "000001.SZ", volume=6000.0, available=6000.0, price=50.0
    )  # 可卖 300,000
    untouched = _leg("300750.SZ", volume=1000.0, available=1000.0, price=200.0)

    # Act
    plan = plan_trim(
        **{**_BASE_KWARGS, "position_value": 1_400_000.0},
        legs=[untouched, second, locked_big],  # 传入顺序故意打乱
    )

    # Assert：两条腿都在计划里，缺口覆盖到 0，第三条腿完全没动
    assert plan.action == ACTION_TRIM
    assert [leg.symbol for leg in plan.legs] == ["600036.SH", "000001.SZ"]
    assert plan.planned_value == pytest.approx(400_000.0)
    assert plan.remaining_value == pytest.approx(0.0)
    assert all(leg.symbol != "300750.SZ" for leg in plan.legs)


def test_plan_trim_records_partial_coverage_instead_of_hoping() -> None:
    """覆盖不全时把缺口显式记下来（``remaining_value`` + 原因），不靠下一拍碰运气。"""
    only_100 = _leg(
        "600036.SH", volume=1000.0, available=100.0, price=100.0
    )  # 可卖 10,000
    locked = _leg("000001.SZ", volume=5000.0, available=0.0, price=50.0)

    plan = plan_trim(
        **{**_BASE_KWARGS, "position_value": 1_400_000.0}, legs=[only_100, locked]
    )

    assert plan.action == ACTION_TRIM  # 有腿可动 → 是 trim，不是 blocked
    assert plan.planned_value == pytest.approx(10_000.0)
    assert plan.remaining_value == pytest.approx(390_000.0)
    assert "缺口未覆盖" in plan.reason
    assert ("000001.SZ", "柜台可用 0（T+1 锁定或已被挂单占用）") in plan.skipped


def test_plan_trim_blocked_when_over_limit_but_no_executable_leg() -> None:
    """超限却一条腿都排不出来 = 该动手却动不了 → ``blocked`` 且原因必须显眼。"""
    legs = [
        _leg("600036.SH", inflight=True),
        _leg("000001.SZ", available=0.0),
        _leg("300750.SZ", price=None),
    ]
    plan = plan_trim(**{**_BASE_KWARGS, "position_value": 1_400_000.0}, legs=legs)

    assert plan.action == ACTION_BLOCKED
    assert plan.legs == ()
    assert plan.need_value == pytest.approx(400_000.0)
    assert len(plan.skipped) == 3
    assert all(sym in plan.reason for sym in ("600036.SH", "000001.SZ", "300750.SZ"))


def test_plan_trim_blocked_with_no_legs_at_all() -> None:
    plan = plan_trim(**{**_BASE_KWARGS, "position_value": 1_400_000.0}, legs=[])
    assert plan.action == ACTION_BLOCKED
    assert "无持仓腿" in plan.reason


# ── 腿级跳过判据 ───────────────────────────────────────────────────


def test_plan_trim_skips_leg_with_inflight_sell() -> None:
    """在途卖单去重（跨轮）：同标的同方向已有委托就不再加一条。"""
    plan = plan_trim(
        **_BASE_KWARGS,
        legs=[_leg(inflight=True), _leg("000001.SZ", price=50.0, volume=1000.0)],
    )
    assert [leg.symbol for leg in plan.legs] == ["000001.SZ"]
    assert plan.skipped[0][1].startswith("已有在途卖单")


def test_plan_trim_skips_leg_at_limit_down() -> None:
    """已封跌停不接（唯一谓词 ``at_limit_down``）：卖不出去只会占住可用量。"""
    plan = plan_trim(
        **_BASE_KWARGS,
        legs=[
            _leg("600036.SH", day_chg_ratio=-0.10, limit_threshold_ratio=0.10),
            _leg("000001.SZ", price=50.0, volume=1000.0),
        ],
    )
    assert plan.skipped[0][0] == "600036.SH"
    assert "跌停" in plan.skipped[0][1]
    assert [leg.symbol for leg in plan.legs] == ["000001.SZ"]


def test_plan_trim_sells_leg_when_st_status_unknown() -> None:
    """ST 状态未知 → 阈值 None → **不判跌停、照卖**（缺一不判，错过卖点的代价更大）。"""
    leg = _leg("600036.SH", day_chg_ratio=-0.099, limit_threshold_ratio=None)
    plan = plan_trim(**_BASE_KWARGS, legs=[leg])
    assert plan.action == ACTION_TRIM
    assert [leg.symbol for leg in plan.legs] == ["600036.SH"]


def test_plan_trim_raises_on_percent_point_threshold() -> None:
    """单位错（10.0 当成比例）必须炸出来，不能静默按错单位比较。"""
    leg = _leg("600036.SH", day_chg_ratio=-0.099, limit_threshold_ratio=10.0)
    with pytest.raises(ValueError, match="比例区间"):
        plan_trim(**_BASE_KWARGS, legs=[leg])


@pytest.mark.parametrize(
    ("kwargs", "fragment"),
    [
        ({"available": None}, "可用量读不出"),
        ({"available": 0.0}, "可用 0"),
        ({"price": None}, "无行情"),
        ({"price": 0.0}, "无行情"),
        ({"volume": 0.0, "available": 0.0}, "持仓为 0"),
    ],
)
def test_plan_trim_skip_reasons_are_specific(kwargs: dict, fragment: str) -> None:
    """「读不出」与「是 0」是两种意思：文案要能直接指向排查方向。"""
    legs = [_leg("600036.SH", **kwargs), _leg("000001.SZ", price=50.0, volume=2000.0)]
    plan = plan_trim(**_BASE_KWARGS, legs=legs)
    reasons = dict(plan.skipped)
    assert fragment in reasons.get("600036.SH", "")


# ── 数量口径（唯一实现 align_sell_quantity）────────────────────────


def test_plan_trim_aligns_quantity_to_lot_and_records_note() -> None:
    """整手对齐的说明要带出来（2026-09-08：600×33% 意图 199 被地板取整成 100）。"""
    # 缺口 30,000 / 价 70 → 意图 429 股 → 最近整手 400
    plan = plan_trim(
        equity=_EQUITY,
        position_value=1_030_000.0,
        limits=_DEFENSIVE,
        legs=[_leg(price=70.0, volume=10_000.0, available=10_000.0)],
    )
    assert plan.legs[0].quantity == pytest.approx(400.0)
    assert "429→400" in plan.legs[0].note
    assert plan.planned_value == pytest.approx(28_000.0)
    assert plan.remaining_value == pytest.approx(2_000.0)  # 整手取整吃掉的零头


def test_plan_trim_never_sells_below_min_lot_for_star_board() -> None:
    """科创板不足最小申报量时抬到 200 股（不是清仓、也不是报个会被拒的数）。"""
    plan = plan_trim(
        equity=_EQUITY,
        position_value=1_030_000.0,
        limits=_DEFENSIVE,
        legs=[_leg(_STAR, price=100.0, volume=2000.0, available=2000.0)],
    )
    assert plan.legs[0].quantity == pytest.approx(300.0)  # 意图 300 已是整手
    assert plan.planned_value == pytest.approx(30_000.0)
    # 部分卖出（可用 2000 只卖 300）**不许**带整仓断言：那会让派发层跳过整手预检
    assert plan.legs[0].full_exit is False


def test_plan_trim_clears_odd_lot_remainder() -> None:
    """卖完会剩碎股 → 一次性全清（否则那点碎股之后卖不掉）。"""
    plan = plan_trim(
        equity=_EQUITY,
        position_value=1_010_000.0,
        limits=_DEFENSIVE,
        legs=[_leg("600036.SH", price=100.0, volume=150.0, available=150.0)],
    )
    # 缺口 10,000 → 意图 100 股 → 卖后剩 50 股碎股 → 全清 150
    assert plan.legs[0].quantity == pytest.approx(150.0)
    assert "碎股" in plan.legs[0].note
    # 卖的就是**全部**可用量 ⇒ 派发层要带整仓断言（评审 M4：预检只看当日快照）
    assert plan.legs[0].full_exit is True


def test_plan_trim_skips_a_leg_the_lot_helper_zeroes(monkeypatch) -> None:
    """整手口径回 0 时跳过该腿并说清——**绝不落一条 0 股的委托**。

    走不到的分支也要钉：``_leg_skip_reason`` 目前已在前面挡掉「可用 ≤ 0」，但
    ``align_sell_quantity`` 是**共享**口径（隔壁守护单/策略控制台同用），它的契约写明了
    「可用量为 0 → 返回 (0, 原因)」。契约一旦变化，这里若顺手放行，出去的就是一条
    0 股报单（柜台拒单 / 语义未定义）。0 股委托在任何情况下都不是我们要的东西。
    """

    def _zeroes(symbol, want, can_use):
        return 0.0, "柜台可用数量为 0（T+1 锁定或已被挂单占用）"

    monkeypatch.setattr(core, "align_sell_quantity", _zeroes)
    plan = plan_trim(
        equity=_EQUITY,
        position_value=1_010_000.0,
        limits=_DEFENSIVE,
        legs=[_leg("600036.SH", price=100.0, volume=150.0, available=150.0)],
    )

    assert plan.legs == ()
    assert plan.skipped == (
        ("600036.SH", "柜台可用数量为 0（T+1 锁定或已被挂单占用）"),
    )
    assert plan.action == core.ACTION_BLOCKED  # 该减却一腿都排不出 → 响亮


def test_plan_trim_caps_legs_per_round() -> None:
    """单轮腿数上限是护栏：极端分散的账户也一轮不超过 ``MAX_LEGS_PER_ROUND`` 条腿。"""
    legs = [
        _leg(f"60{i:04d}.SH", price=100.0, volume=100.0, available=100.0)
        for i in range(12)
    ]
    plan = plan_trim(
        equity=_EQUITY, position_value=50 * _EQUITY, limits=_DEFENSIVE, legs=legs
    )
    assert len(plan.legs) == MAX_LEGS_PER_ROUND
    assert plan.remaining_value > 0  # 缺口远未覆盖，靠下一轮继续


def test_plan_trim_is_pure_and_order_insensitive() -> None:
    """同一入参永远同一结果；腿的传入顺序不影响计划（内部按市值排序）。"""
    legs = [
        _leg("600036.SH", price=100.0, volume=1000.0),
        _leg("000001.SZ", price=50.0, volume=1000.0),
        _leg("300750.SZ", price=200.0, volume=1000.0),
    ]
    plan_a = plan_trim(**{**_BASE_KWARGS, "position_value": 1_400_000.0}, legs=legs)
    plan_b = plan_trim(
        **{**_BASE_KWARGS, "position_value": 1_400_000.0}, legs=list(reversed(legs))
    )
    assert plan_a == plan_b
    assert [leg.symbol for leg in plan_a.legs] == [leg.symbol for leg in plan_b.legs]


# ── 幂等委托号 ─────────────────────────────────────────────────────


def test_trim_client_order_id_shape() -> None:
    """``trim-`` 前缀被五处登记表认作「券商托管/强制退出族」，形态钉死。"""
    assert (
        trim_client_order_id("600036.SH", "20260924", 1) == "trim-600036.SH-20260924-g1"
    )


def test_trim_client_order_id_generation_and_day_edges() -> None:
    assert (
        trim_client_order_id("600036.SH", "20260924", 0) == "trim-600036.SH-20260924-g1"
    )
    assert (
        trim_client_order_id("600036.SH", "20260924", None)
        == "trim-600036.SH-20260924-g1"
    )
    assert (
        trim_client_order_id("600036.SH", " 20260924 ", 3)
        == "trim-600036.SH-20260924-g3"
    )
    assert trim_client_order_id("600036.SH", "", 2) == "trim-600036.SH--g2"


def test_trim_client_order_id_is_stable_across_crash_retry() -> None:
    """崩溃重试按同号幂等：同 (标的, 日, 代次) 必须逐字符相同。"""
    a = trim_client_order_id("600036.SH", "20260924", 2)
    b = trim_client_order_id("600036.SH", "20260924", 2)
    assert a == b
    assert a != trim_client_order_id("600036.SH", "20260925", 2)
    assert a != trim_client_order_id("000001.SZ", "20260924", 2)


def test_plan_codes_are_stable_causes_not_numbers() -> None:
    """``TrimPlan.code`` 是**稳定成因码**：同一个成因下，杠杆/金额怎么变都不变。

    调用点拿它做告警去重键。用 ``reason`` 当键会退化：reason 里带着 ``1.3000`` 这类
    每轮都在变的数，于是「同一成因当日一次」变成「每 60s 一次」。
    """
    from backend.services.trade.services import leverage_trim_core as core

    legs = [LegInput(_MAIN, 13_000.0, available=13_000.0, price=100.0)]

    def _plan(equity: float, value: float) -> core.TrimPlan:
        return plan_trim(
            equity=equity, position_value=value, legs=legs, limits=_DEFENSIVE
        )

    # 同是「超限该减」：杠杆 1.3 / 1.5 / 2.0 三份不同的 reason，同一个码
    codes = {
        _plan(1_000_000.0, 1_300_000.0).code,
        _plan(1_000_000.0, 1_500_000.0).code,
        _plan(1_000_000.0, 2_000_000.0).code,
    }
    assert codes == {core.CODE_TRIM}

    # 每个分支各有各的码，且都不是空串
    assert _plan(1_000_000.0, 900_000.0).code == core.CODE_UNDER_LIMIT
    assert _plan(None, 1_300_000.0).code == core.CODE_EQUITY_UNAVAILABLE
    assert _plan(1_000_000.0, None).code == core.CODE_VALUE_UNAVAILABLE
    assert (
        core.plan_trim(
            equity=1_000_000.0,
            position_value=1_300_000.0,
            legs=[],
            limits=_DEFENSIVE,
        ).code
        == core.CODE_NO_EXECUTABLE_LEG
    )
    assert code_of_no_limits() == core.CODE_NO_LIMITS
    for code in (
        core.CODE_TRIM,
        core.CODE_UNDER_LIMIT,
        core.CODE_NO_LIMITS,
        core.CODE_EQUITY_UNAVAILABLE,
        core.CODE_VALUE_UNAVAILABLE,
        core.CODE_NEED_NONPOSITIVE,
        core.CODE_NO_EXECUTABLE_LEG,
    ):
        # 可读的成因码：非空、无空格、不含数字（带了数字就说明它其实在描述状态）
        assert code and " " not in code and not any(ch.isdigit() for ch in code)


def code_of_no_limits() -> str:
    return plan_trim(
        equity=1_000_000.0,
        position_value=1_300_000.0,
        legs=[],
        limits=None,
        limits_reason="档位未给出减仓参数",
    ).code


def test_limits_read_codes_split_absent_from_dirty() -> None:
    """缺键（有意姿态）与脏文档（疑似故障）的**成因码**必须分得开——
    调用点按码告警：absent 静默，dirty 每成因每日一次。"""
    from backend.services.trade.services import leverage_trim_core as core

    absent = limits_from_budget({"leverage_max": None, "leverage_trim_to": None})
    assert absent.kind == LIMITS_ABSENT and absent.code == core.LIMITS_CODE_NO_PARAMS

    unparseable = limits_from_budget({"leverage_max": "abc", "leverage_trim_to": 1.0})
    assert unparseable.kind == LIMITS_DIRTY
    assert unparseable.code == core.LIMITS_CODE_UNPARSEABLE

    non_positive = limits_from_budget({"leverage_max": -1.0, "leverage_trim_to": 1.0})
    assert non_positive.code == core.LIMITS_CODE_NON_POSITIVE

    out_of_band = limits_from_budget({"leverage_max": 1.2, "leverage_trim_to": 0.1})
    assert out_of_band.code == core.LIMITS_CODE_OUT_OF_BAND

    ok = limits_from_budget({"leverage_max": 1.2, "leverage_trim_to": 1.15})
    assert ok.kind == LIMITS_OK and ok.code == ""


def test_leg_value_matches_the_gate_numerator_caliber() -> None:
    """分子口径与闸门 ``l1.leverage_cap`` 逐条对齐（L10）：错一条就会
    「闸门说没超、执行器说超了」，而执行器是按**它自己算的缺口**去卖真单的。

    三条规则：有价按 ``|价×量|``、**0 量整行不计**（柜台快照留的已清仓历史行残值
    会虚增分子 → 多卖）、取绝对值（空头不净掉多头）。
    """
    # 有行情：用实时价，不用柜台自报市值（那是无价时的兜底）
    assert LegInput(_MAIN, 1_000.0, price=10.0, market_value=9_999.0).value == 10_000.0
    # 无行情：退回柜台自报市值
    assert LegInput(_MAIN, 1_000.0, market_value=9_999.0).value == 9_999.0
    # 0 量行（已清仓的幻影行）：残值一律不计
    assert LegInput(_MAIN, 0.0, price=10.0, market_value=8_888.0).value == 0.0
    # 空头：按敞口取绝对值
    assert LegInput(_MAIN, -1_000.0, price=10.0).value == 10_000.0

    # 合计口径 = 逐腿求和（与闸门「逐行口径」同式）
    legs = [
        LegInput(_MAIN, 1_000.0, price=10.0),
        LegInput(_STAR, 0.0, price=100.0, market_value=50_000.0),  # 幻影行
    ]
    assert aggregate_value(None, legs) == 10_000.0
    # 账户自报与逐腿重建**取大**（自报可能漏腿、重建可能因无行情低配）
    assert aggregate_value(12_345.0, legs) == 12_345.0


# ── enforcing_cap_from_config：配置侧正在强制的上限（评审 M2）─────────
# 病灶：闸门真正拦单的上限是 min(配置值, 档位值)（``tiers.apply_to_rules`` 取更严者），
# 只读档位就会在 [配置上限, 档位上限) 这段**死区**里判「没超限」——闸门已按更严的
# 配置上限拒买，账户却整天没人压仓。
class _RiskConfigView:
    """``risk_gate_service.RiskConfig`` 的**鸭子镜像**（本层禁 IO 依赖，不 import 它）。

    字段名与真实 dataclass 的一致性由 ``test_enforcing_cap_agrees_with_the_real_risk_config``
    单独钉住（改名会让那条用例红，而不是让这里跟着改）。
    """

    def __init__(self, enabled=True, shadow=False, rules=None) -> None:
        self.enabled = enabled
        self.shadow = shadow
        self.rules = (
            {"l1.leverage_cap": {"max_leverage": 1.1}} if rules is None else rules
        )


def test_enforcing_cap_reads_the_configured_value() -> None:
    assert core.enforcing_cap_from_config(_RiskConfigView()) == (1.1, "")


def test_enforcing_cap_uses_the_registry_default_when_params_are_absent() -> None:
    """规则对象在、参数没写 → 取**注册表里的默认值**，不抄一个写死的 1.0。

    写死会在闸门默认值与执行器常量分叉时静静错开（闸门按新默认拦、执行器按旧值减）。
    """
    from backend.shared.risk.registry import get_rule

    spec = get_rule("l1.leverage_cap")
    assert spec is not None
    expected = float(dict(spec.default_params)["max_leverage"])

    cap, problem = core.enforcing_cap_from_config(
        _RiskConfigView(rules={"l1.leverage_cap": {}})
    )

    assert problem == "" and cap == pytest.approx(expected)


@pytest.mark.parametrize(
    "config",
    [
        None,  # 键不存在 = 风控整套没启用
        _RiskConfigView(enabled=False),  # 关着
        _RiskConfigView(shadow=True),  # 影子期：只记不拦
        _RiskConfigView(rules={}),  # 配置根本没列这条规则
        _RiskConfigView(rules={"l1.position_cap": {"pct": 0.1}}),  # 列了别条
        _RiskConfigView(rules=["not-a-mapping"]),  # 畸形 rules
    ],
)
def test_enforcing_cap_is_none_when_the_config_does_not_constrain(config) -> None:
    """配置侧不构成约束的六种形态 → ``(None, "")``：**都不是故障**，按档位单独走。"""
    assert core.enforcing_cap_from_config(config) == (None, "")


def test_enforcing_cap_reads_string_flags_the_way_the_gate_does() -> None:
    """配置视图可能是**原始字符串**形态（Redis/JSON 往返）：``"false"`` 在 Python 里是
    真值，按 ``bool()`` 读会把「配置关着」读成「正在强制」，于是按一个不该生效的上限
    减真仓。开关位必须走 ``_flag`` 的同一词表。"""
    assert core.enforcing_cap_from_config(
        _RiskConfigView(enabled="true", shadow="false")
    ) == (1.1, "")
    assert core.enforcing_cap_from_config(
        _RiskConfigView(
            enabled="true",
            shadow=False,
            rules={"l1.leverage_cap": {"max_leverage": "1.1"}},
        )
    ) == (1.1, "")


def test_enforcing_cap_reads_numeric_flags_the_way_the_gate_does() -> None:
    """开关位也可能是 **JSON 数字**（0/1）：``_flag`` 的数值分支与字符串分支同一结论。

    只钉字符串的话，``enabled=0`` 这类形态一旦走到别的分支（比如 ``int("0")`` 抛错被
    ``except`` 吞成 ``cap-unusable``）就会把「配置关着」报成「配置读不出」——两者都
    fail-closed，但一个是每日告警的假警报，一个是真该响的警报，混在一起就没法运维。
    """
    assert core.enforcing_cap_from_config(_RiskConfigView(enabled=1, shadow=0)) == (
        1.1,
        "",
    )
    assert core.enforcing_cap_from_config(_RiskConfigView(enabled=0)) == (None, "")
    assert core.enforcing_cap_from_config(_RiskConfigView(enabled=1, shadow=1)) == (
        None,
        "",
    )


@pytest.mark.parametrize(
    "entry",
    [
        {"max_leverage": 0},
        {"max_leverage": -1.5},
        {"max_leverage": "abc"},
        {"max_leverage": None},
    ],
)
def test_enforcing_cap_refuses_an_unusable_value(entry: dict) -> None:
    """说自己在强制、却读不出强制多少 → **问题串**（调用点停手喊人）。"""
    cap, problem = core.enforcing_cap_from_config(
        _RiskConfigView(rules={"l1.leverage_cap": entry})
    )
    assert cap is None
    assert "不可用" in problem and "max_leverage" in problem


def test_enforcing_cap_refuses_a_non_mapping_entry() -> None:
    cap, problem = core.enforcing_cap_from_config(
        _RiskConfigView(rules={"l1.leverage_cap": 1.1})  # 直接给了个数，不是参数对象
    )
    assert cap is None
    assert "不是参数对象" in problem


def test_enforcing_cap_reports_an_unregistered_rule(monkeypatch) -> None:
    """配置比代码新（规则没注册在本进程）→ 问题串，不许当作「没有约束」。"""
    monkeypatch.setattr(core, "get_rule", lambda _rule_id: None)

    cap, problem = core.enforcing_cap_from_config(_RiskConfigView())

    assert cap is None
    assert "未注册规则" in problem


def test_enforcing_cap_agrees_with_the_real_risk_config() -> None:
    """字段名/语义与真实 ``RiskConfig`` 对齐（鸭子类型读的就是它，改名必须红）。"""
    from backend.services.trade.services.risk_gate_service import RiskConfig

    real = RiskConfig(
        enabled=True, shadow=False, rules={"l1.leverage_cap": {"max_leverage": 1.25}}
    )

    assert core.enforcing_cap_from_config(real) == (1.25, "")
    # 影子期的真实默认（load_config 的 shadow 默认就是 True）→ 不构成约束
    assert core.enforcing_cap_from_config(RiskConfig(enabled=True)) == (None, "")


# ── 生效上限 = min(配置, 档位)：夹取与冲突拒执（评审 M2）─────────────
_CALM_BUDGET = {"leverage_max": 1.5, "leverage_trim_to": 1.3}


def test_tighter_enforcing_cap_clamps_the_effective_line_and_names_the_source() -> None:
    read = limits_from_budget(_CALM_BUDGET, level="calm", enforcing_cap=1.1)

    assert read.kind == LIMITS_OK
    assert read.limits is not None
    assert read.limits.leverage_max == pytest.approx(1.1)  # 触发线降到配置值
    assert read.limits.target == pytest.approx(1.1)  # 目标不高于上限（夹取）
    assert read.limits.trim_to == pytest.approx(1.3)  # 档位原值保留（留痕）
    assert "1.1" in read.limits.cap_note  # 「档位写着 1.3，为什么减到 1.1」答得出来
    assert "1.1" in read.limits.describe and "档位 calm" in read.limits.describe


@pytest.mark.parametrize("cap", [1.5, 2.0])
def test_looser_or_equal_enforcing_cap_leaves_the_tier_line(cap: float) -> None:
    """配置更松或相等 → 档位是更严的那一侧，按档位减（不留注记，免得噪声）。"""
    read = limits_from_budget(_CALM_BUDGET, level="calm", enforcing_cap=cap)

    assert read.limits is not None
    assert read.limits.leverage_max == pytest.approx(1.5)
    assert read.limits.target == pytest.approx(1.3)
    assert read.limits.cap_note == ""


def test_enforcing_cap_far_below_the_tier_line_is_a_conflict_not_a_trim() -> None:
    """配置上限比档位低太多（< 档位上限的一半）→ **拒绝执行并喊人**。

    强减会把仓位**真的卖掉**：一个写错的配置值（1.5 打成 0.15）若被照单执行，就是拿
    typo 当清仓指令。可疑的输入不产生不可逆动作——停下喊人，由人裁决。
    """
    read = limits_from_budget(_CALM_BUDGET, level="calm", enforcing_cap=0.6)

    assert read.limits is None
    assert read.kind == LIMITS_DIRTY
    assert read.code == core.LIMITS_CODE_CAP_CONFLICT
    assert "矛盾" in read.reason


@pytest.mark.parametrize("cap", [0, -1.0, "abc"])
def test_unusable_enforcing_cap_is_dirty_not_absent(cap) -> None:
    """值不可用 ≠ 没有约束：说自己在强制执行却读不出数，必须走 dirty（喊人）。

    ``None`` **不**在此列——那是「配置侧不构成约束」的哨兵值（见下一条用例）：
    「读不出强制多少」由 ``enforcing_cap_from_config`` 的问题串表达，调用点在那之前
    就停手了，不会拿 ``None`` 走到这里。
    """
    read = limits_from_budget(_CALM_BUDGET, level="calm", enforcing_cap=cap)

    assert read.limits is None
    assert read.kind == LIMITS_DIRTY
    assert read.code == core.LIMITS_CODE_CAP_UNUSABLE


def test_absent_enforcing_cap_leaves_the_tier_untouched() -> None:
    """``enforcing_cap=None`` = 配置侧不构成约束（关着/影子/没列）→ 档位原样。"""
    read = limits_from_budget(_CALM_BUDGET, level="calm", enforcing_cap=None)

    assert read.limits is not None
    assert read.limits.leverage_max == pytest.approx(1.5)
    assert read.limits.cap_note == ""


# ── 整手取整的超卖：披露而非停手（评审 M5 复核）─────────────────────


def test_plan_trim_discloses_lot_rounding_overshoot() -> None:
    """缺口不是整手的整数倍 → 只能按手卖 → **超卖**，而超卖必须看得见。

    缺口 5,000 元 / 最小一手 100 股 × 100 元 = 10,000 元：按手只能卖一手（超卖 5,000）。
    这里钉的是**披露**（``overshoot_value`` + 文案），不是「不动手」——停手会把账户
    永久留在触发线上方，而执行器唯一的职责就是把它压回去。
    """
    plan = plan_trim(
        equity=_EQUITY,
        position_value=1_005_000.0,  # 10,050 股 × 100 元
        legs=[_leg(volume=10_050.0)],
        limits=_DEFENSIVE,
    )

    assert plan.action == ACTION_TRIM
    assert [leg.quantity for leg in plan.legs] == [100.0]
    assert plan.planned_value == pytest.approx(10_000.0)
    assert plan.overshoot_value == pytest.approx(5_000.0)
    assert plan.remaining_value == 0.0  # 超卖与欠卖互斥
    assert "整手取整超卖 5000.00 元" in plan.reason


def test_plan_trim_reports_no_overshoot_when_the_plan_covers_the_gap_exactly() -> None:
    plan = plan_trim(
        equity=_EQUITY,
        position_value=1_300_000.0,
        legs=[_leg(volume=13_000.0, available=13_000.0)],
        limits=_DEFENSIVE,
    )

    assert plan.planned_value == pytest.approx(300_000.0)
    assert plan.overshoot_value == 0.0
    assert "超卖" not in plan.reason
