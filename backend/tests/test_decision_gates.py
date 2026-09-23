"""P2.1b：买入闸门（纯核心）。

**为什么要有这一层**：隔壁 BayMax 的 21 条闸口对到本仓 20 条规则后，缺的四条
（`symbol.boundary` / `pool.not_member` / `limit.up` / `limit.down`）**全部在买入侧**
——正是「LLM 凭空开仓」与「追高」两个风险口；再加一条 `budget.unaffordable`
（买不起一手的票**整行不推给模型**，`kind=unseen`）。这四条不是移植，是新建：
本仓 `l3.stale_quote`/`l3.price_deviation` 只管「价新不新、离不离谱」，
`ghost_pricing` 只**算价**判可成交性、`lot_rules` 明说不在本地重算阈值——
没有任何一层拦「涨停追高」或「买不在池里的票」。

**为什么不能塞进 `backend/shared/risk/` 的 `RiskContext` 规则表**：那张表的规则
签名是 `fn(ctx, params)`，而 `RiskContext` 里**没有** `day_chg`/`prev_close`/
候选池/`name` 这些字段，且「规则不读任何配置存储」。把判定挂到没人填的字段上，
就是本仓已经吃过的那个坑：**判定与单测都在，但 ctx 字段没人填 → 规则永远不
触发**。所以决策层闸门是**第二族规则**：同样的纯函数纪律，入参由决策轮自己给，
规则 id 仍进同一张登记表 `gate_registry`，两族由 `test_risk_gate_registry.py`
一起守。

**fail-open / fail-closed 的分界（照抄隔壁实测口径，不许自由发挥）**：

* 名称取不到 → **放行**（不能因为名称表拉不到就把当天所有买入停掉）；
  代码黑名单不依赖名称，任何时候都生效。
* 额度/现金类字段取不到 → **不判**并留 note（本层不臆造数字）；真正的资金闸在
  执行段（`l1.available_cash`，fail-closed），本层不复制它。
* **卖出永远放行**：被套的仓位出不来比买错更糟。本模块只有 `check_buy`，
  没有对称的 `check_sell`——不是没写，是刻意不设。

涨跌停阈值**一律由调用点注入**（唯一事实源 `local_market_data.limit_threshold`，
按板别/ST/制度日期解析、返回**比例**）。本层的测试值只为「测比较」而存在，
故标了 fidelity 豁免——见下面两个常量的注释。
"""

from __future__ import annotations

import pytest

from backend.shared.decision import gates
from backend.shared.decision.contract import (
    BUY,
    HOLD,
    PCT_DIRTY,
    SELL,
    WATCH,
    Decision,
    parse_pct,
)
from backend.shared.decision.gates import (
    ALL_RULE_IDS,
    RULE_BELOW_MIN_LOT,
    RULE_HALTED,
    RULE_LIMIT_DOWN,
    RULE_LIMIT_UP,
    RULE_PCT_INVALID,
    RULE_PCT_ZERO,
    RULE_POOL_NOT_MEMBER,
    RULE_POOL_ROW_INVALID,
    RULE_ROUND_NEW_BUYS,
    RULE_SYMBOL_BOUNDARY,
    RULE_UNAFFORDABLE,
    RULE_VCASH,
    BuyGate,
    check_buy,
    check_halted,
    check_limit_reach,
    check_symbol_boundary,
    filter_pool,
)
from backend.shared.risk.gate_registry import gate_spec

#: 涨跌停阈值/涨幅在**本层只是两个被注入的数**（本层不产出、不猜测阈值）。
#: 这两个值只为测「比较」本身而写死，与任何真实板别口径无关——故标豁免。
_LIMIT_THRESHOLD = (
    0.095  # fidelity: allow-limit-threshold — 单测语料：只测比较，阈值由调用点注入
)
_DAY_CHG_AT_LIMIT = 0.098  # fidelity: allow-limit-threshold — 同上
_DAY_CHG_BELOW = 0.094


def _buy(code: str = "600036.SH", pct: float = 0.2, name: str = "") -> Decision:
    return Decision(action=BUY, code=code, name=name, pct=parse_pct(pct))


def _gate(**kw) -> BuyGate:
    base = {
        "pool_codes": frozenset({"600036.SH", "000001.SZ"}),
        "blocked_symbols": frozenset(),
        "per_stock_pct": 0.2,
    }
    base.update(kw)
    return BuyGate(**base)


def _check(decision: Decision, gate: BuyGate | None = None, **kw):
    """默认走「新开仓、池内、无额外约束」，只让被测的那一条起作用。"""
    params = {"held": False, "new_buys_round": 0}
    params.update(kw)
    return check_buy(decision, gate=gate or _gate(), **params)


# --------------------------------------------------------------------------
# 1. 标的边界（l4.symbol_boundary）
# --------------------------------------------------------------------------


class TestSymbolBoundary:
    """ST/退市/黑名单——模型说什么都不放行（`symbol_policy` 的硬约束）。"""

    def test_blacklist_blocks_exact_code(self) -> None:
        gate = _gate(blocked_symbols=frozenset({"600036.SH"}))
        v = _check(_buy(), gate)
        assert not v.allowed and v.rule == RULE_SYMBOL_BOUNDARY

    def test_blacklist_hits_regardless_of_code_form(self) -> None:
        """名单里是后缀式、模型吐的是裸码/前缀式——不归一就是**静默放行**。"""
        gate = _gate(blocked_symbols=frozenset({"600036.SH"}))
        for form in ("600036", "SH600036", "sh600036"):
            v = _check(_buy(code=form), gate)
            assert not v.allowed, f"{form} 未被黑名单拦住"

    def test_blacklist_entry_in_any_form_still_hits(self) -> None:
        gate = _gate(blocked_symbols=frozenset({"SH600036"}))
        assert not _check(_buy(), gate).allowed

    @pytest.mark.parametrize("name", ["ST海航", "*ST三圣", "SST前锋", "S*ST生化"])
    def test_st_names_blocked_by_prefix(self, name: str) -> None:
        v = _check(_buy(name=name))
        assert not v.allowed and v.rule == RULE_SYMBOL_BOUNDARY

    def test_st_with_space_still_blocked(self) -> None:
        assert not _check(_buy(name="ST 三圣")).allowed

    def test_delisting_name_blocked(self) -> None:
        """退市整理期（「退市海润」/「海润退」）：中文名里「退」基本只出现在这类。"""
        assert not _check(_buy(name="退市海润")).allowed
        assert not _check(_buy(name="海润退")).allowed

    def test_normal_name_not_blocked(self) -> None:
        assert _check(_buy(name="招商银行")).allowed

    def test_missing_name_fails_open(self) -> None:
        """名称取不到 → 放行。理由：不能因为名称表拉不到就停掉当天所有买入。"""
        assert _check(_buy(name="")).allowed

    def test_missing_name_does_not_weaken_blacklist(self) -> None:
        """fail-open 只对**名称类**判据成立；代码黑名单不依赖名称。"""
        gate = _gate(blocked_symbols=frozenset({"600036.SH"}))
        assert not _check(_buy(name=""), gate).allowed

    def test_empty_code_blocked(self) -> None:
        """没有代码的买入指令执行不了——拒，且理由说清是「没有代码」。"""
        v = _check(_buy(code=""))
        assert not v.allowed and v.rule == RULE_SYMBOL_BOUNDARY

    def test_allow_st_explicitly_opens_the_gate(self) -> None:
        assert _check(_buy(name="ST海航"), _gate(allow_st=True)).allowed

    def test_boundary_helper_returns_a_verdict(self) -> None:
        assert check_symbol_boundary("600036.SH", "招商银行", _gate()).allowed
        assert not check_symbol_boundary("600036.SH", "ST海航", _gate()).allowed


# --------------------------------------------------------------------------
# 2. 候选池成员资格（l2.pool_not_member）
# --------------------------------------------------------------------------


class TestPoolMembership:
    def test_new_position_outside_pool_blocked(self) -> None:
        v = _check(_buy("300750.SZ"))
        assert not v.allowed and v.rule == RULE_POOL_NOT_MEMBER

    def test_new_position_inside_pool_allowed(self) -> None:
        assert _check(_buy("600036.SH")).allowed

    def test_empty_pool_blocks_every_new_position(self) -> None:
        """空池 = 无候选，必须拒——否则「池子构建失败」会变成「全市场随便买」。"""
        v = _check(_buy(), _gate(pool_codes=frozenset()))
        assert not v.allowed and v.rule == RULE_POOL_NOT_MEMBER

    def test_adding_to_existing_position_ignores_pool(self) -> None:
        """持仓内加仓不受池约束：池子约束的是「新开仓买什么」。"""
        assert _check(_buy("300750.SZ"), held=True).allowed

    def test_pool_matching_is_form_insensitive(self) -> None:
        gate = _gate(pool_codes=frozenset({"SH600036"}))
        assert _check(_buy("600036.SH"), gate).allowed


# --------------------------------------------------------------------------
# 3. 执行比例可执行性（l2.pct_invalid / l2.pct_zero）
# --------------------------------------------------------------------------


class TestPctExecutability:
    def test_dirty_pct_blocked_with_raw_value(self) -> None:
        """给了值读不出 → 停手留痕（不许当成「没表达」按默认额度买）。"""
        d = Decision(action=BUY, code="600036.SH", pct=parse_pct("0.3股"))
        v = _check(d)
        assert not v.allowed and v.rule == RULE_PCT_INVALID
        assert "0.3股" in v.reason, (
            "脏值原文必须进 reason（否则事后查不出模型给了什么）"
        )

    def test_missing_pct_blocked(self) -> None:
        """买入必须自己给出幅度：「没表达」不是「用满额度」。"""
        d = Decision(action=BUY, code="600036.SH", pct=parse_pct(None))
        v = _check(d)
        assert not v.allowed and v.rule == RULE_PCT_ZERO

    def test_explicit_zero_blocked(self) -> None:
        v = _check(_buy(pct=0.0))
        assert not v.allowed and v.rule == RULE_PCT_ZERO

    def test_negative_pct_blocked(self) -> None:
        assert not _check(_buy(pct=-0.3)).allowed

    def test_pct_clamped_not_rejected(self) -> None:
        """超过单票额度是**夹取**（隔壁口径），不是拒——模型多要一点不该丢机会。"""
        v = _check(_buy(pct=0.9), _gate(per_stock_pct=0.2))
        assert v.allowed and v.pct == pytest.approx(0.2)

    def test_pct_within_budget_kept_as_is(self) -> None:
        v = _check(_buy(pct=0.15), _gate(per_stock_pct=0.2))
        assert v.allowed and v.pct == pytest.approx(0.15)

    def test_percent_string_pct_is_honoured(self) -> None:
        d = Decision(action=BUY, code="600036.SH", pct=parse_pct("20%"))
        v = _check(d, _gate(per_stock_pct=0.5))
        assert v.allowed and v.pct == pytest.approx(0.2)

    def test_unconfigured_cap_does_not_clamp_and_says_so(self) -> None:
        """额度未配置（0）→ 不夹取 + 留 note。**不许**静默夹成 0：那会让每笔买入
        都被 `pct_zero` 拒，把配置缺失伪装成模型的错。"""
        v = _check(_buy(pct=0.9), _gate(per_stock_pct=0.0))
        assert v.allowed and v.pct == pytest.approx(0.9) and v.note

    def test_percent_unit_cap_is_rejected(self) -> None:
        """传 15 表示「十五个百分点」是单位错误：静默当比例会让夹取变空操作。"""
        with pytest.raises(ValueError):
            _gate(per_stock_pct=15)


# --------------------------------------------------------------------------
# 4. 涨跌停（l4.limit_up / l4.limit_down）
# --------------------------------------------------------------------------


class TestLimitReach:
    """阈值一律**注入**（唯一事实源 `local_market_data.limit_threshold`，按板别/ST/
    制度日期解析）：本模块不写任何阈值字面量，`shared/market_fidelity.py` 的
    「自写涨跌停阈值」扫描器就盯着这件事。"""

    def test_at_limit_up_blocked(self) -> None:
        v = _check(
            _buy(),
            day_chg_ratio=_DAY_CHG_AT_LIMIT,
            limit_threshold_ratio=_LIMIT_THRESHOLD,
        )
        assert not v.allowed and v.rule == RULE_LIMIT_UP

    def test_at_limit_down_blocked(self) -> None:
        v = _check(
            _buy(),
            day_chg_ratio=-_DAY_CHG_AT_LIMIT,
            limit_threshold_ratio=_LIMIT_THRESHOLD,
        )
        assert not v.allowed and v.rule == RULE_LIMIT_DOWN

    def test_exactly_at_threshold_blocked(self) -> None:
        """「到板」即算到——比较是 `>=`，不是 `>`。差一点就是追高。"""
        v = _check(
            _buy(),
            day_chg_ratio=_LIMIT_THRESHOLD,
            limit_threshold_ratio=_LIMIT_THRESHOLD,
        )
        assert not v.allowed and v.rule == RULE_LIMIT_UP

    def test_just_below_threshold_allowed(self) -> None:
        assert _check(
            _buy(),
            day_chg_ratio=_DAY_CHG_BELOW,
            limit_threshold_ratio=_LIMIT_THRESHOLD,
        ).allowed

    def test_ratio_threshold_in_percent_units_is_rejected(self) -> None:
        """阈值传了百分点（9.5 而非 0.095）→ **报错**，不静默判错。

        本仓两族单位并存：`limit_threshold` 返回比例、`market_breadth` 那边是
        百分点。静默按错单位比较的后果是**一条都不拦**。
        """
        with pytest.raises(ValueError):
            _check(
                _buy(),
                day_chg_ratio=_DAY_CHG_AT_LIMIT,
                limit_threshold_ratio=_LIMIT_THRESHOLD * 100,
            )

    def test_nan_day_chg_is_not_judged(self) -> None:
        """NaN 会穿过所有比较（恒 False）→ 假放行。必须显式按「未判」处理。"""
        v = _check(
            _buy(),
            day_chg_ratio=float("nan"),
            limit_threshold_ratio=_LIMIT_THRESHOLD,
        )
        assert v.allowed and v.note, "NaN 应当落到「未判」并留下说明"

    def test_missing_inputs_are_not_judged(self) -> None:
        v = _check(_buy())
        assert v.allowed and v.note

    def test_only_one_of_the_two_inputs_is_a_programming_error(self) -> None:
        """只给一半是调用点的错（不是数据缺失），必须炸而不是静默不判。"""
        with pytest.raises(ValueError):
            check_limit_reach(
                day_chg_ratio=_DAY_CHG_AT_LIMIT, limit_threshold_ratio=None
            )

    def test_zero_threshold_is_not_judged(self) -> None:
        """阈值 0 是脏输入（字段缺失/无限制的市场），不是「什么都算到板」。"""
        v = _check(_buy(), day_chg_ratio=_DAY_CHG_AT_LIMIT, limit_threshold_ratio=0.0)
        assert v.allowed and v.note

    def test_garbage_day_chg_is_not_judged(self) -> None:
        v = _check(_buy(), day_chg_ratio="涨了", limit_threshold_ratio=_LIMIT_THRESHOLD)
        assert v.allowed and v.note


# --------------------------------------------------------------------------
# 5. 停牌（l4.halted）
# --------------------------------------------------------------------------


class TestHalted:
    def test_halted_blocks(self) -> None:
        v = _check(_buy(), halted=True)
        assert not v.allowed and v.rule == RULE_HALTED

    def test_not_halted_passes(self) -> None:
        assert _check(_buy(), halted=False).allowed

    def test_unknown_is_not_false(self) -> None:
        """`None`（不知道）≠ `False`（没停牌）：不知道时不判，但要留痕。"""
        v = _check(_buy(), halted=None)
        assert v.allowed and "停牌" in v.note

    def test_halted_helper_shape(self) -> None:
        assert check_halted(False).allowed
        assert not check_halted(True).allowed
        assert check_halted(None).allowed


# --------------------------------------------------------------------------
# 6. 本轮新开仓上限（l2.round_new_buys）
# --------------------------------------------------------------------------


class TestRoundNewBuysCap:
    def test_cap_blocks_new_position(self) -> None:
        v = _check(_buy(), _gate(max_new_buys_round=2), new_buys_round=2)
        assert not v.allowed and v.rule == RULE_ROUND_NEW_BUYS

    def test_below_cap_passes(self) -> None:
        assert _check(_buy(), _gate(max_new_buys_round=2), new_buys_round=1).allowed

    def test_adding_to_position_does_not_count(self) -> None:
        v = _check(_buy(), _gate(max_new_buys_round=2), new_buys_round=2, held=True)
        assert v.allowed

    def test_no_cap_configured_means_no_limit(self) -> None:
        """未配置（0）不判——但留 note：配置缺失必须可见，不能默默变成「不限量」。"""
        v = _check(_buy(), _gate(max_new_buys_round=0), new_buys_round=9)
        assert v.allowed and v.note


# --------------------------------------------------------------------------
# 7. 资金类：子账户虚拟现金 / 买不起一手
# --------------------------------------------------------------------------


class TestCashGates:
    def test_vcash_blocks_when_insufficient(self) -> None:
        v = _check(_buy(), _gate(virtual_cash=1_000.0), need_amount=2_000.0)
        assert not v.allowed and v.rule == RULE_VCASH

    def test_vcash_passes_when_sufficient(self) -> None:
        assert _check(_buy(), _gate(virtual_cash=5_000.0), need_amount=2_000.0).allowed

    def test_vcash_unmodelled_is_not_judged(self) -> None:
        """子账户未建模（P2.7 之前）→ 不判并留 note，不假装「现金无限」。"""
        v = _check(_buy(), _gate(virtual_cash=None), need_amount=2_000.0)
        assert v.allowed and v.note

    def test_need_amount_missing_is_not_judged(self) -> None:
        v = _check(_buy(), _gate(virtual_cash=1_000.0))
        assert v.allowed and v.note

    def test_below_min_lot_blocks(self) -> None:
        """预算连一手都买不起 → 拒（执行兜底口径，与 `filter_pool` 的 unseen 同判据）。"""
        v = _check(
            _buy("688183.SH"),
            _gate(pool_codes=frozenset({"688183.SH"})),
            budget=3_000.0,
            price=120.0,
        )
        assert not v.allowed and v.rule == RULE_BELOW_MIN_LOT

    def test_main_board_one_lot_is_enough(self) -> None:
        """同一笔钱在主板够一手（100 股）、在科创板不够（200 股）——板别差异必须生效。"""
        gate = _gate(pool_codes=frozenset({"600036.SH", "688183.SH"}))
        main = _check(_buy("600036.SH"), gate, budget=3_000.0, price=29.0)
        star = _check(_buy("688183.SH"), gate, budget=3_000.0, price=29.0)
        assert main.allowed and not star.allowed

    def test_price_missing_is_not_judged(self) -> None:
        v = _check(_buy(), budget=1_000.0, price=None)
        assert v.allowed and v.note


# --------------------------------------------------------------------------
# 8. 候选池前置过滤（unseen：进模型视野之前就剔除）
# --------------------------------------------------------------------------


class TestFilterPool:
    """`l1.unaffordable` 是 **unseen** 语义：模型**压根没看见**这只票，
    记的不是「被否决的决策」而是「假设模型会选它」的弱反事实。"""

    def test_unaffordable_row_dropped(self) -> None:
        rows = [{"code": "688183.SH", "name": "生益电子", "price": 120.0}]
        kept, dropped = filter_pool(rows, _gate(), budget=3_000.0)
        assert kept == [] and len(dropped) == 1
        assert dropped[0][1].rule == RULE_UNAFFORDABLE

    def test_star_board_needs_two_hundred_shares(self) -> None:
        """科创板最小 200 股：够买 100 股 ≠ 买得起。口径与执行段同源。"""
        rows = [{"code": "688183.SH", "name": "生益电子", "price": 100.0}]
        kept, dropped = filter_pool(
            rows, _gate(), budget=15_000.0
        )  # 够 100 股、不够 200
        assert kept == [] and dropped[0][1].rule == RULE_UNAFFORDABLE

    def test_affordable_row_kept(self) -> None:
        rows = [{"code": "600036.SH", "name": "招商银行", "price": 40.0}]
        kept, dropped = filter_pool(rows, _gate(), budget=10_000.0)
        assert len(kept) == 1 and dropped == []

    def test_slack_is_explicit(self) -> None:
        """`slack` 是显式参数：隔壁用 1.02 给成交价漂移留余量，本仓不设默认余量。"""
        rows = [{"code": "600036.SH", "name": "招商银行", "price": 100.0}]
        # 预算刚好够 1 手：slack=1.0 保留；slack=1.02 要求 2% 余量，剔除
        kept_1, _ = filter_pool(rows, _gate(), budget=10_000.0, slack=1.0)
        kept_2, dropped_2 = filter_pool(rows, _gate(), budget=10_000.0, slack=1.02)
        assert len(kept_1) == 1
        assert kept_2 == [] and dropped_2[0][1].rule == RULE_UNAFFORDABLE

    def test_slack_below_one_is_rejected(self) -> None:
        """slack < 1 = 要求买到不足一手，是无意义的配置错误。"""
        with pytest.raises(ValueError):
            filter_pool([], _gate(), budget=1.0, slack=0.9)

    def test_boundary_row_dropped_with_boundary_rule(self) -> None:
        rows = [{"code": "600036.SH", "name": "ST海航", "price": 4.0}]
        kept, dropped = filter_pool(rows, _gate(), budget=10_000.0)
        assert kept == [] and dropped[0][1].rule == RULE_SYMBOL_BOUNDARY

    def test_dirty_rows_are_dropped_not_crashed(self) -> None:
        """池子是外部数据：非 dict / 缺 code 的行按 `pool_row_invalid` 丢弃并**可见**
        （与「排除了一只票」区分开），不能让整池崩掉。"""
        rows = [
            None,
            "600036.SH",
            {"name": "无码"},
            {"code": "600036.SH", "price": 40.0},
        ]
        kept, dropped = filter_pool(rows, _gate(), budget=10_000.0)
        assert [r["code"] for r in kept] == ["600036.SH"]
        assert len(dropped) == 3
        assert {v.rule for _, v in dropped} == {RULE_POOL_ROW_INVALID}

    def test_row_without_price_dropped_when_budget_known(self) -> None:
        """有额度却没有价 → 判不了「买得起吗」，按池子缺陷剔除（不是静默保留）。"""
        rows = [{"code": "600036.SH", "name": "招商银行"}]
        kept, dropped = filter_pool(rows, _gate(), budget=10_000.0)
        assert kept == [] and dropped[0][1].rule == RULE_POOL_ROW_INVALID

    def test_budget_none_means_no_budget_filtering(self) -> None:
        """预算未知时**不筛资金**（显式选择），但边界照筛——两件事不互相掩护。"""
        rows = [
            {"code": "688183.SH", "price": 9_999.0},
            {"code": "600036.SH", "name": "ST海航", "price": 4.0},
        ]
        kept, dropped = filter_pool(rows, _gate(), budget=None)
        assert [r["code"] for r in kept] == ["688183.SH"]
        assert dropped[0][1].rule == RULE_SYMBOL_BOUNDARY

    def test_empty_and_none_rows_are_safe(self) -> None:
        assert filter_pool([], _gate(), budget=1.0) == ([], [])
        assert filter_pool(None, _gate(), budget=1.0) == ([], [])


# --------------------------------------------------------------------------
# 9. 形状与词汇表
# --------------------------------------------------------------------------


class TestVerdictShape:
    def test_allowed_verdict_carries_no_rule(self) -> None:
        v = _check(_buy())
        assert v.allowed and v.rule == "" and v.reason == ""

    def test_blocked_verdict_always_has_reason(self) -> None:
        for d, kw in (
            (_buy(code="300750.SZ"), {}),
            (_buy(pct=0.0), {}),
            (_buy(), {"halted": True}),
        ):
            v = _check(d, **kw)
            assert not v.allowed and v.reason.strip(), f"{v.rule} 拒了却没有理由"

    def test_evidence_is_immutable_and_serialisable(self) -> None:
        v = _check(
            _buy(),
            day_chg_ratio=_DAY_CHG_AT_LIMIT,
            limit_threshold_ratio=_LIMIT_THRESHOLD,
        )
        assert isinstance(v.evidence, tuple)
        assert dict(v.evidence)["limit_threshold_ratio"] == pytest.approx(
            _LIMIT_THRESHOLD
        )

    def test_check_buy_refuses_non_buy_actions(self) -> None:
        """买入闸门只判买入。卖出走这条路径是调用点的错——**卖出永远放行**。"""
        for action in (SELL, HOLD, WATCH):
            d = Decision(action=action, code="600036.SH", pct=parse_pct(0.5))
            with pytest.raises(ValueError):
                _check(d)

    def test_verdict_is_deterministic(self) -> None:
        """纯函数：同输入同输出（重放/审计依赖这一点）。"""
        kw = {
            "day_chg_ratio": _DAY_CHG_BELOW,
            "limit_threshold_ratio": _LIMIT_THRESHOLD,
        }
        assert _check(_buy(), **kw) == _check(_buy(), **kw)

    def test_dirty_pct_object_is_not_mistaken_for_given(self) -> None:
        """`Pct.state` 是这里唯一的判据来源：`dirty` 与 `0.0` 必须分开。"""
        d = Decision(action=BUY, code="600036.SH", pct=parse_pct("三成"))
        assert d.pct.state == PCT_DIRTY and d.pct.state != "given"
        assert _check(d).rule == RULE_PCT_INVALID


class TestRuleVocabulary:
    """跨模块不变量：本层每条规则都要有登记表条目，且常量表不许漏项。"""

    def test_all_rule_ids_are_unique(self) -> None:
        assert len(ALL_RULE_IDS) == len(set(ALL_RULE_IDS))

    def test_every_rule_constant_is_in_all_rule_ids(self) -> None:
        """新加了 `RULE_*` 常量却忘了进 `ALL_RULE_IDS` → 红。

        这条盯的是**静默漏登记**：少一个 id，登记表的双向覆盖就少查一条规则，
        而漏掉的那条恰恰最可能是刚加的那条。
        """
        declared = {
            name
            for name, value in vars(gates).items()
            if name.startswith("RULE_") and isinstance(value, str)
        }
        # 非空断言：扫不到任何常量时上面那句会**空集通过**，正是「零项参与=通过」
        assert declared, "没在 gates 模块里扫到任何 RULE_* 常量——扫描方式失效了"
        missing = sorted(
            name for name in declared if getattr(gates, name) not in ALL_RULE_IDS
        )
        assert not missing, f"这些 RULE_* 常量没进 ALL_RULE_IDS：{missing}"

    @pytest.mark.parametrize("rule_id", ALL_RULE_IDS)
    def test_rule_is_registered(self, rule_id: str) -> None:
        spec = gate_spec(rule_id)
        assert spec is not None, f"{rule_id} 没有登记表条目"
        assert spec.where.startswith("gates."), (
            f"{rule_id} 的 where={spec.where!r} 没指向本模块的函数"
        )

    def test_unaffordable_is_unseen_not_veto(self) -> None:
        """`unseen` 与 `veto` 混一起，会把「视野被资金规模截断」读成「规则成本」。"""
        assert gate_spec(RULE_UNAFFORDABLE).kind == "unseen"

    def test_halted_is_structural(self) -> None:
        """停牌放行也不会成交（交易所不接），代价恒为 0 → structural。"""
        assert gate_spec(RULE_HALTED).kind == "structural"

    def test_pct_rules_are_structural(self) -> None:
        """比例缺失/读不出**无法统一定价反事实**（不知道模型想买多少）——
        记成 veto 会让影子账拿一个编出来的仓位去算收益。"""
        assert gate_spec(RULE_PCT_ZERO).kind == "structural"
        assert gate_spec(RULE_PCT_INVALID).kind == "structural"

    def test_limit_gates_are_priced(self) -> None:
        """涨停/跌停**买得到**（封板有成交、跌停可以接），拦住就是放弃机会 →
        必须可计价（veto），否则「这条闸值不值」永远无法回答。"""
        assert gate_spec(RULE_LIMIT_UP).kind == "veto"
        assert gate_spec(RULE_LIMIT_DOWN).kind == "veto"
