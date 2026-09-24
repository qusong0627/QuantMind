"""P1.3：守护单三缺口——绝对价止损 / ``move_stop`` 条件棘轮 / ``pct`` 部分减仓。

对应迁移计划 ``docs/local/quant-trader-migration-plan.md`` P1.3：隔壁 ``live_price_watch``
产出「watch + 条件价位」，执行侧必须能表达：

1. **绝对价**（``stop_loss: 44.6``）——支撑位是价位不是百分比；
2. **``move_stop`` 条件棘轮**——现价上触某价即把防守抬到保本（只升不降）；
3. **``pct`` 部分减仓**——触价先减三分之一，而不是一次清仓。

本文件即为验收口径（每条都对应一个会亏钱的错法）：

* 绝对价与 ``stop_loss_pct`` 同时配置 → 取**更高（更紧）**的那条线；
* 棘轮 ``move_stop_to`` 必须**低于** ``move_stop_trigger``，否则「武装即触发」
  → 整条拒绝，不武装；
* 抬高的防守位**跨日保留**（持仓拿几周是常态），但持仓一旦换了
  （成本价/总量对不上）立即作废并告警——否则新开的仓会被上一个仓的
  防守位**秒杀**；
* ``reduce_pct`` 非法一律**整条拒绝**，绝不静默回落成「全量卖出」
  （把「减三分之一」执行成「清仓」是真金白银的错）。

**单次触发语义**：部分减仓与全量减仓一样「当日触发一次即终态」，剩余仓位需要
下一次武装（``POST /reset`` 或改规则）才继续受保护——通知文案必须说清这一点，
否则用户以为剩下的仓还在防守里。
"""

from __future__ import annotations

import pytest

from backend.services.live_trading.services import sltp_executor as ex
from backend.shared.exit_rules import (
    RULE_HARD_STOP,
    ExitRuleSet,
    PositionState,
    evaluate_exit,
    ratchet_stop_price,
)
from backend.tests.test_qmt_sltp_executor import Harness, _cfg, _rule

DAY = "20260911"
PREV_DAY = "20260910"


def _rule_ext(symbol: str = "600036.SH", **over) -> dict:
    """带新字段的规则（默认只给绝对防守，不含 pct）。"""
    base = {
        "symbol": symbol,
        "entry_price": 100.0,
        "stop_loss_pct": None,
        "take_profit_pct": None,
        "trailing_stop_pct": None,
    }
    base.update(over)
    return base


def _pos(
    symbol: str = "600036.SH",
    can_use: float = 1000,
    cost: float = 100.0,
    volume: float | None = None,
):
    return {
        "stock_code": symbol,
        "symbol": symbol,
        "can_use_volume": can_use,
        "volume": can_use if volume is None else volume,
        "open_price": cost,
        "avg_price": cost,
    }


# --------------------------------------------------------------------------
# 1. 绝对价止损（exit_rules 纯函数层）
# --------------------------------------------------------------------------
class TestAbsoluteStopPrice:
    def test_absolute_price_triggers(self) -> None:
        rules = ExitRuleSet(hard_stop_price=97.0)
        pos = PositionState(entry_price=100.0, last_price=96.9)
        decision = evaluate_exit(rules, pos)
        assert decision.should_exit is True
        assert decision.rule_id == RULE_HARD_STOP
        assert "97.00" in decision.reason

    def test_absolute_price_not_triggered_above(self) -> None:
        rules = ExitRuleSet(hard_stop_price=97.0)
        decision = evaluate_exit(
            rules, PositionState(entry_price=100.0, last_price=97.1)
        )
        assert decision.should_exit is False

    def test_tighter_of_pct_and_price_wins(self) -> None:
        """pct 5% → 95，绝对价 97 → 取 97（更高 = 更紧），96 应触发。"""
        rules = ExitRuleSet(hard_stop_pct=0.05, hard_stop_price=97.0)
        decision = evaluate_exit(
            rules, PositionState(entry_price=100.0, last_price=96.0)
        )
        assert decision.should_exit is True
        assert decision.snapshot.get("stop_source") == "price"

    def test_looser_absolute_price_is_ignored(self) -> None:
        """绝对价 93 低于 pct 线 95 → 95 仍生效，96 不触发。"""
        rules = ExitRuleSet(hard_stop_pct=0.05, hard_stop_price=93.0)
        decision = evaluate_exit(
            rules, PositionState(entry_price=100.0, last_price=96.0)
        )
        assert decision.should_exit is False
        decision2 = evaluate_exit(
            rules, PositionState(entry_price=100.0, last_price=94.9)
        )
        assert decision2.should_exit is True
        assert decision2.snapshot.get("stop_source") == "pct"

    def test_legacy_pct_only_reason_text_unchanged(self) -> None:
        """存量配置的文案一字不改（桥/执行器/测试都按这句对齐）。"""
        decision = evaluate_exit(
            ExitRuleSet(hard_stop_pct=0.05),
            PositionState(entry_price=100.0, last_price=94.9),
        )
        assert decision.reason == "止损触发 现价94.90 ≤ 95.00"

    def test_absolute_price_only_reason_text(self) -> None:
        decision = evaluate_exit(
            ExitRuleSet(hard_stop_price=44.6),
            PositionState(entry_price=50.0, last_price=44.6),
        )
        assert decision.reason == "止损触发 现价44.60 ≤ 44.60"


# --------------------------------------------------------------------------
# 2. 棘轮（纯函数）
# --------------------------------------------------------------------------
class TestRatchetPure:
    def test_arms_when_price_touches_trigger(self) -> None:
        new_stop, note = ratchet_stop_price(
            trigger=105.0, move_to=100.0, current=None, price=106.0
        )
        assert new_stop == 100.0
        assert note  # 有说明（用于通知）

    def test_quiet_below_trigger(self) -> None:
        new_stop, note = ratchet_stop_price(
            trigger=105.0, move_to=100.0, current=None, price=104.9
        )
        assert (new_stop, note) == (None, "")

    def test_never_lowers_existing_stop(self) -> None:
        """已经抬到 101，目标 100 → 不动（棘轮只升不降）。"""
        new_stop, note = ratchet_stop_price(
            trigger=105.0, move_to=100.0, current=101.0, price=106.0
        )
        assert (new_stop, note) == (None, "")

    def test_raises_when_target_above_current(self) -> None:
        new_stop, _ = ratchet_stop_price(
            trigger=105.0, move_to=100.0, current=95.0, price=105.0
        )
        assert new_stop == 100.0

    def test_move_to_above_trigger_is_rejected(self) -> None:
        """目标防守价**高于**触发价 → 拒配（note 非空 = 要告警）。

        2026-09-23（P1.3b）口径变更：原判据是 ``move_to >= trigger`` 全拒，
        现放宽为只拒 ``move_to > trigger``。**零间隙**（``move_to == trigger``，
        隔壁 BayMax LLM 决策语料 ``move_stop`` 覆盖 49.3% 的形态）改为合法，
        由调用方 ``_apply_ratchet`` 的同轮去抖兜住「武装即触发」。
        放宽的依据与演示见 ``test_sltp_tp_price_and_zerogap.py``。
        """
        new_stop, note = ratchet_stop_price(
            trigger=105.0, move_to=105.5, current=None, price=106.0
        )
        assert new_stop is None
        assert note

    def test_non_positive_move_to_is_rejected(self) -> None:
        new_stop, note = ratchet_stop_price(
            trigger=105.0, move_to=0.0, current=None, price=106.0
        )
        assert new_stop is None
        assert note

    def test_disabled_when_unset(self) -> None:
        """两端都没配 = 没启用棘轮 → 静默。"""
        assert ratchet_stop_price(
            trigger=None, move_to=None, current=None, price=106.0
        ) == (None, "")

    def test_unpaired_config_is_rejected(self) -> None:
        """只配一半（有目标没触发价）→ 报错不静默。

        配置层 ``rule_reject_reason`` 会把不成对的规则整条拒掉（正常路径走不到
        这里）；纯函数仍需 fail-closed——半配置若被静默忽略，用户以为有棘轮，
        实际整轮防守停在规则初始位。
        """
        new_stop, note = ratchet_stop_price(
            trigger=None, move_to=100.0, current=None, price=106.0
        )
        assert new_stop is None
        assert note

    def test_invalid_price_fails_closed(self) -> None:
        new_stop, note = ratchet_stop_price(
            trigger=105.0, move_to=100.0, current=None, price=float("nan")
        )
        assert new_stop is None
        assert note


# --------------------------------------------------------------------------
# 3. 棘轮（执行器整轮）
# --------------------------------------------------------------------------
class TestRatchetCycle:
    def test_arms_then_fires_at_breakeven(self) -> None:
        rule = _rule_ext(move_stop_trigger=105.0, move_stop_to=100.0)
        h = Harness(
            cfg=_cfg([rule]),
            ticks={"600036.SH": {"lastPrice": 106.0}},
            positions=[_pos()],
            default_detail={"DownStopPrice": 88.0},
        )
        summary = h.cycle()
        # 上触触发价：只抬防守，不卖
        assert h.dispatched == []
        assert summary["triggered"] == 0
        assert h.state()["rules"]["600036.SH"]["stop_price"] == 100.0
        assert any("防守" in n["title"] for n in h.notices)

        # 回落到保本价 → 按抬高的防守位卖出（不再是 95 的原始止损）
        h.client.ticks = {"600036.SH": {"lastPrice": 99.5}}
        summary2 = h.cycle()
        assert len(h.dispatched) == 1
        assert summary2["triggered"] == 1
        assert (
            "100.00" in h.dispatched[0]["remarks"]
            or "100.00" in h.notices[-1]["content"]
        )

    def test_arm_is_idempotent_across_cycles(self) -> None:
        rule = _rule_ext(move_stop_trigger=105.0, move_stop_to=100.0)
        h = Harness(
            cfg=_cfg([rule]),
            ticks={"600036.SH": {"lastPrice": 106.0}},
            positions=[_pos()],
        )
        h.cycle()
        armed_notices = [n for n in h.notices if "防守" in n["title"]]
        h.cycle()
        assert (
            len([n for n in h.notices if "防守" in n["title"]])
            == len(armed_notices)
            == 1
        )

    def test_raised_stop_does_not_fire_while_price_above(self) -> None:
        rule = _rule_ext(move_stop_trigger=105.0, move_stop_to=100.0)
        h = Harness(
            cfg=_cfg([rule]),
            ticks={"600036.SH": {"lastPrice": 106.0}},
            positions=[_pos()],
        )
        h.cycle()
        h.client.ticks = {"600036.SH": {"lastPrice": 101.0}}
        h.cycle()
        assert h.dispatched == []


# --------------------------------------------------------------------------
# 4. 抬高防守位的跨日保留与作废
# --------------------------------------------------------------------------
class TestCarriedStopLifecycle:
    def _state_with_stop(self, **over) -> dict:
        item = {
            "status": "filled",
            "stop_price": 100.0,
            "stop_entry": 100.0,
            "stop_volume": 1000.0,
        }
        item.update(over)
        return {"date": PREV_DAY, "rules": {"600036.SH": item}}

    def test_stop_survives_day_roll(self) -> None:
        h = Harness(
            cfg=_cfg([_rule_ext(move_stop_trigger=105.0, move_stop_to=100.0)]),
            state=self._state_with_stop(),
            ticks={"600036.SH": {"lastPrice": 99.5}},
            positions=[_pos()],
        )
        h.cycle()
        # 跨日后仍按抬高的 100 防守（若丢了就会退回到「无止损线」而不卖）
        assert len(h.dispatched) == 1
        assert h.state()["rules"]["600036.SH"]["stop_price"] == 100.0

    def test_stop_dropped_when_entry_changed(self) -> None:
        """成本 100→120（换了持仓/加过仓）→ 旧防守位作废并告警，不卖。

        作废后**同一轮按规则重评**：现价 110 ≥ 触发价 105，棘轮条件仍成立，
        于是防守位以**新持仓**（stop_entry=120）重新落账——这不是把旧的捡回来，
        而是重新推导（旧凭据 100 已丢）。重挂是安全的：棘轮只在
        ``price ≥ trigger > move_to`` 时武装，武装价必然低于现价，不会立刻成交。
        """
        h = Harness(
            cfg=_cfg(
                [
                    _rule_ext(
                        entry_price=120.0, move_stop_trigger=105.0, move_stop_to=100.0
                    )
                ]
            ),
            state=self._state_with_stop(),
            ticks={"600036.SH": {"lastPrice": 110.0}},
            positions=[_pos(cost=120.0)],
        )
        h.cycle()
        assert h.dispatched == []
        assert any("复位" in n["title"] for n in h.notices)
        st = h.state()["rules"]["600036.SH"]
        assert st["stop_entry"] == 120.0  # 重新挂到新持仓，而不是沿用旧的 100
        assert st["stop_price"] == 100.0

    def test_stop_dropped_when_position_grew(self) -> None:
        """持仓量变大（清仓后又买回 / 加了仓）→ 作废，防旧防守位秒杀新仓。

        重挂时 ``stop_volume`` 跟着涨到新总量：防守位覆盖的是**当前**持仓，
        旧量留着会让后续「持仓又变大」的校验永远失灵。
        """
        h = Harness(
            cfg=_cfg([_rule_ext(move_stop_trigger=105.0, move_stop_to=100.0)]),
            state=self._state_with_stop(),
            ticks={"600036.SH": {"lastPrice": 110.0}},
            positions=[_pos(can_use=2000, volume=2000)],
        )
        h.cycle()
        assert any("复位" in n["title"] for n in h.notices)
        st = h.state()["rules"]["600036.SH"]
        assert st["stop_volume"] == 2000.0

    def test_changed_position_below_carried_stop_does_not_sell(self) -> None:
        """秒杀场景（这条守卫真正要防的）：换了持仓 + 现价已在旧防守位之下。

        旧仓成本 100、棘轮抬到 100；清仓后在 95 重新买回。若把 100 直接套到
        新仓上，现价 95 ≤ 100 → 开仓即被卖出。作废优先于评估，且此时现价
        95 < 触发价 105，棘轮条件不成立 → 不会重新挂 —— 结果是**不卖**。
        """
        h = Harness(
            cfg=_cfg(
                [
                    _rule_ext(
                        entry_price=95.0, move_stop_trigger=105.0, move_stop_to=100.0
                    )
                ]
            ),
            state=self._state_with_stop(),
            ticks={"600036.SH": {"lastPrice": 95.0}},
            positions=[_pos(cost=95.0)],
        )
        h.cycle()
        assert h.dispatched == []
        assert "stop_price" not in h.state()["rules"]["600036.SH"]

    def test_stop_kept_when_positions_unreadable(self) -> None:
        """读不到持仓 ≠ 持仓变了：保留防守位（宁可保护过度，不可静默撤防）。"""
        h = Harness(
            cfg=_cfg([_rule_ext(move_stop_trigger=105.0, move_stop_to=100.0)]),
            state=self._state_with_stop(),
            ticks={"600036.SH": {"lastPrice": 99.5}},
        )

        async def _boom() -> list:
            raise RuntimeError("桥账户查询超时")

        h.client.get_positions = _boom  # type: ignore[method-assign]
        h.cycle()
        st = h.state()["rules"]["600036.SH"]
        assert st.get("stop_price") == 100.0

    def test_reset_clears_raised_stop(self) -> None:
        """重新武装 = 回到规则配置的初始防守位（当日状态机从头来过）。"""
        h = Harness(
            cfg=_cfg([_rule_ext(move_stop_trigger=105.0, move_stop_to=100.0)]),
            state=self._state_with_stop(),
        )
        h.reset(["600036.SH"])
        assert "stop_price" not in h.state()["rules"]["600036.SH"]


# --------------------------------------------------------------------------
# 4b. 移动止损的高水位跨日保留（2026-09-24 补）
# --------------------------------------------------------------------------
class TestCarriedHighWater:
    """跨日保留的是「**持仓以来**最高价」，不是「今日最高价」。

    ``exit_rules`` 的移动止损线是 ``high_water × (1 - trail)``，而 ``high_water``
    缺省回落到 ``entry``。日切丢弃 ``highest_price`` 时不会立刻变松——同一轮
    的第一个报价又把它重置成**今日现价**（``update_highest_price(None, price)``），
    于是回撤线每个交易日往下挪一档：只升不降的保护变成只降不升，恰好在这个
    保护唯一有意义的场景（冲高之后回落）里失效。

    与棘轮防守位同一条纪律：抬高的保护跨日保留，但**持仓换了就作废**——
    旧仓的高点是旧仓的成绩，套到新仓上等于开仓即被卖出。
    """

    def _state_with_hw(self, **over) -> dict:
        item = {
            "status": "armed",
            "highest_price": 120.0,
            "highest_entry": 100.0,  # 这个高点是在成本 100 的持仓上创下的
        }
        item.update(over)
        return {"date": PREV_DAY, "rules": {"600036.SH": item}}

    def test_high_water_survives_day_roll(self) -> None:
        """成本 100、昨日冲到 120、trail 5% → 回撤线 114；今日 112 必须卖出。

        丢高水位的话这一轮算出来的是「今日 112 的 5%」= 106.4，112 在其之上
        → 不卖。要一直跌到 106.4 才动，等于把昨日那 8 毛浮盈的保护全让回去。
        """
        h = Harness(
            cfg=_cfg([_rule_ext(trailing_stop_pct=0.05)]),
            state=self._state_with_hw(),
            ticks={"600036.SH": {"lastPrice": 112.0}},
            positions=[_pos()],
        )
        h.cycle()
        assert len(h.dispatched) == 1, (
            "日切后高水位被丢掉，回撤线从 114 退到 106.4——"
            "移动止损在「冲高后回落」这个唯一有意义的场景里没有起作用"
        )
        assert any("移动止损" in n["content"] for n in h.notices), h.notices

    def test_new_high_is_recorded_with_its_entry(self) -> None:
        """创新高时把「这笔高点是在哪个成本上创的」一并记下。

        没有这个凭据，日切后无从判断旧高点该不该作废（棘轮防守位用
        ``stop_entry`` 做同一件事）。
        """
        h = Harness(
            cfg=_cfg([_rule_ext(trailing_stop_pct=0.05)]),
            state=self._state_with_hw(),
            ticks={"600036.SH": {"lastPrice": 130.0}},
            positions=[_pos()],
        )
        h.cycle()
        st = h.state()["rules"]["600036.SH"]
        assert st["highest_price"] == 130.0
        assert st["highest_entry"] == 100.0

    def test_stale_high_water_dropped_when_entry_changed(self) -> None:
        """换了持仓（成本 100→120）→ 旧高点作废，不得秒杀新仓。

        旧仓冲到 150 是**上一个仓**的成绩。沿用它得到回撤线 142.5，于是一笔
        成本 120、现价 130（浮盈中）的新仓当场被卖——正是棘轮那道校验要防的
        「新持仓被旧防守位秒杀」，只是换成了百分比形态。
        """
        h = Harness(
            cfg=_cfg([_rule_ext(entry_price=120.0, trailing_stop_pct=0.05)]),
            state=self._state_with_hw(highest_price=150.0, highest_entry=100.0),
            ticks={"600036.SH": {"lastPrice": 130.0}},
            positions=[_pos(cost=120.0)],
        )
        h.cycle()
        assert h.dispatched == [], (
            "新持仓（成本 120、现价 130）被上一个仓 150 的高水位卖出"
        )
        assert any("最高价" in n["title"] for n in h.notices), h.notices
        st = h.state()["rules"]["600036.SH"]
        assert float(st.get("highest_price") or 0) < 150.0

    def test_legacy_high_water_without_provenance_is_kept(self) -> None:
        """旧状态没有 ``highest_entry``（本次修复之前写入的）→ 保留，不静默撤防。

        缺凭据 ≠ 持仓变了——``carried_stop_invalid_reason`` 对读不到的持仓也是
        这条纪律（宁可保护过度，不可静默撤防）。保留的代价是万一持仓真换了会
        多卖一次；作废的代价是必然丢掉一整段保护。

        非空过：这一轮的判定确实用上了 120——回撤线 114，现价 112 → 卖出。
        """
        h = Harness(
            cfg=_cfg([_rule_ext(trailing_stop_pct=0.05)]),
            state={
                "date": PREV_DAY,
                "rules": {"600036.SH": {"status": "armed", "highest_price": 120.0}},
            },
            ticks={"600036.SH": {"lastPrice": 112.0}},
            positions=[_pos()],
        )
        h.cycle()
        assert len(h.dispatched) == 1, (
            "缺凭据的旧高水位被当成「持仓变了」作废——升级那一刻把在跑的"
            "移动止损保护全撤了"
        )


# --------------------------------------------------------------------------
# 5. pct 部分减仓
# --------------------------------------------------------------------------
class TestReducePct:
    def test_partial_reduce_quantity(self) -> None:
        rule = _rule_ext(stop_loss_pct=0.05, reduce_pct=0.33)
        h = Harness(
            cfg=_cfg([rule]),
            ticks={"600036.SH": {"lastPrice": 94.9}},
            positions=[_pos(can_use=1000)],
        )
        h.cycle()
        assert len(h.dispatched) == 1
        assert h.dispatched[0]["quantity"] == 300  # 330 → 主板最近整手
        assert any("重新武装" in n["content"] for n in h.notices)

    def test_full_reduce_equals_full_sell_with_odd_lot(self) -> None:
        """reduce_pct=1.0 且可用 1234 股（碎股）→ 全量卖出豁免整手。"""
        rule = _rule_ext(stop_loss_pct=0.05, reduce_pct=1.0)
        h = Harness(
            cfg=_cfg([rule]),
            ticks={"600036.SH": {"lastPrice": 94.9}},
            positions=[_pos(can_use=1234)],
        )
        h.cycle()
        assert h.dispatched[0]["quantity"] == 1234

    def test_explicit_quantity_wins_over_nothing(self) -> None:
        """显式 quantity 时按原语义（不与 reduce_pct 同给，由配置层拒绝）。"""
        rule = _rule_ext(stop_loss_pct=0.05, quantity=200.0)
        h = Harness(
            cfg=_cfg([rule]),
            ticks={"600036.SH": {"lastPrice": 94.9}},
            positions=[_pos(can_use=1000)],
        )
        h.cycle()
        assert h.dispatched[0]["quantity"] == 200

    def test_gem_board_partial_alignment(self) -> None:
        """创业板同样按最近整手（0.33 × 700 = 231 → 200）。"""
        rule = _rule_ext(symbol="300750.SZ", stop_loss_pct=0.05, reduce_pct=0.33)
        h = Harness(
            cfg=_cfg([rule]),
            ticks={"300750.SZ": {"lastPrice": 94.9}},
            positions=[_pos(symbol="300750.SZ", can_use=700, cost=100.0)],
        )
        h.cycle()
        assert h.dispatched[0]["quantity"] == 200


# --------------------------------------------------------------------------
# 6. 配置层：非法一律整条拒绝（不能只清字段）
# --------------------------------------------------------------------------
class TestConfigRejection:
    def test_reduce_pct_out_of_range_rejects_whole_rule(self) -> None:
        """reduce_pct=50（把"50%"写成了整数）→ 整条拒绝，绝不回落成全量卖出。"""
        cfg = ex.merge_config(_cfg([_rule(stop_loss_pct=0.05, reduce_pct=50.0)]))
        assert cfg["rules"] == []
        assert cfg["rejected_rules"][0]["symbol"] == "600036.SH"
        assert "reduce_pct" in cfg["rejected_rules"][0]["reason"]

    def test_reduce_pct_with_quantity_rejected(self) -> None:
        cfg = ex.merge_config(
            _cfg([_rule(stop_loss_pct=0.05, reduce_pct=0.5, quantity=200.0)])
        )
        assert cfg["rules"] == []
        assert cfg["rejected_rules"]

    def test_move_stop_must_be_paired(self) -> None:
        cfg = ex.merge_config(
            _cfg([_rule(stop_loss_pct=0.05, move_stop_trigger=105.0)])
        )
        assert cfg["rules"] == []
        assert "成对" in cfg["rejected_rules"][0]["reason"]

    def test_move_stop_target_above_trigger_rejected(self) -> None:
        """目标**高于**触发价 → 整条拒绝（P1.3b 起只拒这一侧，见纯函数同名用例）。"""
        cfg = ex.merge_config(
            _cfg(
                [_rule(stop_loss_pct=0.05, move_stop_trigger=105.0, move_stop_to=106.0)]
            )
        )
        assert cfg["rules"] == []
        assert cfg["rejected_rules"]

    def test_move_stop_target_equal_to_trigger_accepted(self) -> None:
        """零间隙棘轮（``move_to == trigger``）自 P1.3b 起合法，必须能落库。"""
        cfg = ex.merge_config(
            _cfg(
                [_rule(stop_loss_pct=0.05, move_stop_trigger=105.0, move_stop_to=105.0)]
            )
        )
        assert len(cfg["rules"]) == 1
        assert cfg["rules"][0]["move_stop_to"] == 105.0

    def test_stop_loss_price_non_positive_rejected(self) -> None:
        cfg = ex.merge_config(_cfg([_rule(stop_loss_price=0.0)]))
        assert cfg["rules"] == []

    def test_valid_new_fields_survive_round_trip(self) -> None:
        """DEFAULT_RULE 是字段白名单——新字段忘了加进去会被静默丢掉。"""
        cfg = ex.merge_config(
            _cfg(
                [
                    _rule_ext(
                        stop_loss_price=97.0,
                        move_stop_trigger=105.0,
                        move_stop_to=100.0,
                        reduce_pct=0.5,
                    )
                ]
            )
        )
        rule = cfg["rules"][0]
        assert rule["stop_loss_price"] == 97.0
        assert rule["move_stop_trigger"] == 105.0
        assert rule["move_stop_to"] == 100.0
        assert rule["reduce_pct"] == 0.5
        assert cfg["rejected_rules"] == []

    def test_rejected_rules_not_persisted(self) -> None:
        """rejected_rules 是读时派生（每次读都重算），不写回 Redis。"""
        h = Harness(cfg=_cfg([_rule(stop_loss_pct=0.05, reduce_pct=50.0)]))
        ex.save_config(h.redis, ex.load_config(h.redis))
        stored = h.redis.store[ex.CONFIG_KEY]
        assert "rejected_rules" not in stored
        assert stored["rules"] == []


# --------------------------------------------------------------------------
# 7. API 契约（pydantic 层，写错误在入口就说清）
# --------------------------------------------------------------------------
class TestRouterContract:
    def _rule_model(self, **over):
        from backend.services.trade.routers.qmt_sltp import SltpRule

        base = {"symbol": "600036.SH", "stop_loss_pct": 0.05}
        base.update(over)
        return SltpRule(**base)

    def test_new_fields_accepted(self) -> None:
        rule = self._rule_model(
            stop_loss_price=97.0,
            move_stop_trigger=105.0,
            move_stop_to=100.0,
            reduce_pct=0.33,
        )
        assert rule.reduce_pct == 0.33

    def test_reduce_pct_out_of_range_422(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="reduce_pct"):
            self._rule_model(reduce_pct=50.0)

    def test_reduce_pct_with_quantity_422(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="reduce_pct"):
            self._rule_model(quantity=200.0, reduce_pct=0.5)

    def test_move_stop_pair_422(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="成对"):
            self._rule_model(move_stop_trigger=105.0)

    def test_move_stop_order_422(self) -> None:
        """目标价**高于**触发价 → 422（P1.3b 起只拒这一侧）。"""
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="move_stop_to"):
            self._rule_model(move_stop_trigger=105.0, move_stop_to=106.0)

    def test_move_stop_zero_gap_accepted(self) -> None:
        """零间隙棘轮过 API（隔壁 LLM 决策 49.3% 的形态，不能卡在 422）。"""
        rule = self._rule_model(move_stop_trigger=105.0, move_stop_to=105.0)
        assert rule.move_stop_to == rule.move_stop_trigger == 105.0

    def test_api_validation_uses_the_same_single_source(self) -> None:
        """API 拒因与执行器拒因同一函数（防两处口径漂移）。"""
        import inspect

        from backend.services.trade.routers import qmt_sltp

        source = inspect.getsource(qmt_sltp)
        assert "rule_reject_reason" in source


# --------------------------------------------------------------------------
# 8. 运维 CLI（--arm 带新字段）
# --------------------------------------------------------------------------
class TestCliArm:
    """CLI 是运维盘中改规则的手，写错比不写更危险。"""

    def _args(self, **over):
        import argparse

        base = {
            "entry": None,
            "qty": None,
            "stop": None,
            "take": None,
            "trail": None,
            "stop_price": None,
            "take_price": None,
            "move_trigger": None,
            "move_to": None,
            "reduce_pct": None,
        }
        base.update(over)
        return argparse.Namespace(**base)

    def _redis(self, cfg: dict | None = None):
        from backend.tests.test_qmt_sltp_executor import FakeRedis

        return FakeRedis(cfg=cfg if cfg is not None else ex.merge_config({}))

    def test_new_flags_land_in_saved_rule(self) -> None:
        from backend.scripts import qmt_sltp_ctl as ctl

        redis = self._redis()
        ctl.cmd_arm(
            redis,
            self._args(
                arm="600036.SH",
                stop_price=44.6,
                take_price=52.0,
                move_trigger=105.0,
                move_to=100.0,
                reduce_pct=0.33,
            ),
        )
        rule = ex.load_config(redis)["rules"][0]
        assert rule["stop_loss_price"] == 44.6
        assert rule["take_profit_price"] == 52.0
        assert rule["move_stop_trigger"] == 105.0
        assert rule["move_stop_to"] == 100.0
        assert rule["reduce_pct"] == 0.33

    def test_invalid_combo_exits_without_writing(self) -> None:
        """目标价高于触发价 → 退出码 2 且**不写配置**。

        不拦的话 save_config 会静默丢规则、CLI 还报「已武装」——用户以为有止损，
        实际一条都没落下。

        P1.3b 起 ``move_to == trigger``（零间隙）**合法**，故此处用 ``106 > 105``
        的真错单构造；零间隙的 CLI 路径由 ``test_zero_gap_ratchet_arms`` 正向钉住。
        """
        from backend.scripts import qmt_sltp_ctl as ctl

        redis = self._redis()
        with pytest.raises(SystemExit) as excinfo:
            ctl.cmd_arm(
                redis,
                self._args(arm="600036.SH", move_trigger=105.0, move_to=106.0),
            )
        assert excinfo.value.code == 2
        assert ex.load_config(redis)["rules"] == []

    def test_zero_gap_ratchet_arms(self) -> None:
        """零间隙棘轮是隔壁 LLM 决策的主流形态，CLI 必须能直接武装。"""
        from backend.scripts import qmt_sltp_ctl as ctl

        redis = self._redis()
        ctl.cmd_arm(
            redis, self._args(arm="600036.SH", move_trigger=105.0, move_to=105.0)
        )
        rule = ex.load_config(redis)["rules"][0]
        assert rule["move_stop_to"] == rule["move_stop_trigger"] == 105.0

    def test_valid_rule_still_reports_armed(self, capsys) -> None:
        from backend.scripts import qmt_sltp_ctl as ctl

        redis = self._redis()
        ctl.cmd_arm(redis, self._args(arm="600036.SH", stop=0.05))
        assert "已武装 600036.SH" in capsys.readouterr().out


# --------------------------------------------------------------------------
# 9. 单源判定输入（执行器 / CLI 预演不许各算一份）
# --------------------------------------------------------------------------
class TestSharedDecisionInputs:
    """``--evaluate`` 是盘中确认「规则还灵不灵」的工具——它必须和真单同口径。"""

    def test_effective_stop_takes_the_tighter_of_rule_and_carried(self) -> None:
        assert (
            ex.effective_stop_price({"stop_loss_price": 95.0}, {"stop_price": 100.0})
            == 100.0
        )
        assert (
            ex.effective_stop_price({"stop_loss_price": 97.0}, {"stop_price": 96.0})
            == 97.0
        )
        assert ex.effective_stop_price({}, {}) is None

    def test_trigger_inputs_does_not_mutate_rule(self) -> None:
        """状态是持仓的、规则是配置的——叠加时不许把状态写进规则。"""
        rule = {"symbol": "600036.SH", "stop_loss_price": 95.0}
        merged = ex.trigger_inputs(rule, {"stop_price": 100.0, "highest_price": 108.0})
        assert merged["stop_loss_price"] == 100.0
        assert merged["highest_price"] == 108.0
        assert rule == {"symbol": "600036.SH", "stop_loss_price": 95.0}

    def test_plan_sell_quantity_uses_reduce_pct(self) -> None:
        qty, _ = ex.plan_sell_quantity("600036.SH", {"reduce_pct": 0.33}, 1000.0)
        assert qty == 300

    def test_plan_sell_quantity_explicit_quantity_wins(self) -> None:
        qty, _ = ex.plan_sell_quantity("600036.SH", {"quantity": 200.0}, 1000.0)
        assert qty == 200

    def test_dry_run_uses_ratcheted_stop_and_reduce_pct(self) -> None:
        """预演必须报出**抬高后**的防守位与**部分**减仓量。

        防守位用规则初值 95、量报全量 1000 的话，操作员会读到「现价 96 未触发、
        计划卖出 1000」，而实盘在同一时刻按 100 防守卖出 300。
        """
        from backend.scripts import qmt_sltp_ctl as ctl

        cfg = {
            "rules": [
                _rule_ext(move_stop_trigger=105.0, move_stop_to=100.0, reduce_pct=0.33)
            ]
        }
        state = {
            "rules": {
                "600036.SH": {
                    "stop_price": 100.0,
                    "stop_entry": 100.0,
                    "stop_volume": 1000.0,
                }
            }
        }
        ticks = {"600036.SH": {"lastPrice": 99.0}}
        positions = [_pos(can_use=1000)]
        lines = ctl.evaluate_lines(cfg, state, ticks, positions, {"enabled": False})
        assert len(lines) == 1
        assert "触发=是" in lines[0]
        assert "防守位=100.0" in lines[0]
        assert "计划卖出=300" in lines[0]
