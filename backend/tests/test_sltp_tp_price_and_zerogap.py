"""P1.3b：绝对价止盈 + 零间隙棘轮——P2.4（LLM 守护意图落地）的两个前置缺口。

背景（迁移计划 §P2 结论二，实测数字）：隔壁 BayMax 的 LLM 决策语料 1421 条里
``take_profit`` 覆盖 **86.8%**、``move_stop`` 覆盖 **49.3%**；哨兵实盘 190 次触发中
止盈 **79 次**。而本仓守护单此前只能表达：

1. ``take_profit_pct``（相对成本的比例）——**装不下**绝对价止盈。LLM 给的是
   「压力位 125 元」，不是「成本 +25%」；把价位换算成比例既是改写模型意图，
   又依赖成本价（换仓/加仓后同一价位会漂成另一条线）。
2. ``move_stop_to < move_stop_trigger``（严格小于）——**装不下**零间隙棘轮。
   隔壁语义是 ``move_stop: 105`` = 「上触 105 就把防守抬到 105」。

两个缺口都由本文件钉住，每条用例对应一个会亏钱的错法：

* 绝对价止盈与 ``take_profit_pct`` 并存 → 取**更低（更早触发）**的那条线。
  与止损「取更高（更紧）」同构：两条都是退出触发线，取更松的等于让显式配置失效。
* 零间隙棘轮**必须配同轮去抖**：上触即抬、抬完本轮就判定 → 「武装即触发」，
  在同一轮以刚设定的防守价卖出（等于白设一个止盈保护）。隔壁用 ``stop_at_entry``
  （本轮判定用**抬升前**的防守价）解决，本仓必须移植同一语义。
* 去抖用的「抬升前防守价」必须在**跨日校验之后**取——承载位被判定作废
  （持仓换了）时若仍拿它做判定，新开的仓会被上一个仓的防守位**秒杀**。

**兼容性声明**：``check_sltp_trigger`` 与 ``ExitRuleSet`` 同时被 TDX 桥自带的
stop-loss daemon 使用（桥 daemon 默认开启，与 sltp_executor 是两套独立默认值）。
故本次改动全部为**加性可选字段**：不传新键时两侧逐字行为不变，本文件末尾有
回归钉子。
"""

from __future__ import annotations

import pytest

from backend.services.live_trading.services import sltp_executor as ex
from backend.services.live_trading.services.tdx_quote_feed import check_sltp_trigger
from backend.shared.exit_rules import (
    RULE_HARD_STOP,
    RULE_TAKE_PROFIT,
    ExitRuleSet,
    PositionState,
    evaluate_exit,
    ratchet_stop_price,
)
from backend.tests.test_qmt_sltp_executor import Harness, _cfg
from backend.tests.test_sltp_absolute_ratchet_reduce import _pos, _rule_ext

DAY = "20260911"


# --------------------------------------------------------------------------
# 1. 绝对价止盈（exit_rules 纯函数层）
# --------------------------------------------------------------------------
class TestTakeProfitPricePure:
    def test_absolute_price_triggers(self) -> None:
        rules = ExitRuleSet(take_profit_price=110.0)
        d = evaluate_exit(rules, PositionState(entry_price=100.0, last_price=110.0))
        assert d.should_exit and d.rule_id == RULE_TAKE_PROFIT
        assert d.snapshot["line"] == 110.0

    def test_not_reached(self) -> None:
        rules = ExitRuleSet(take_profit_price=110.0)
        d = evaluate_exit(rules, PositionState(entry_price=100.0, last_price=109.99))
        assert not d.should_exit

    def test_earlier_of_price_and_pct_wins(self) -> None:
        """pct 0.10 → 110；绝对价 108 → 取更低的 108（更早触发）。"""
        rules = ExitRuleSet(take_profit_pct=0.10, take_profit_price=108.0)
        d = evaluate_exit(rules, PositionState(entry_price=100.0, last_price=108.0))
        assert d.should_exit and d.rule_id == RULE_TAKE_PROFIT
        assert d.snapshot["line"] == 108.0

    def test_pct_wins_when_it_is_lower(self) -> None:
        """反向：pct 0.05 → 105 比绝对价 108 更早，按 105 走。"""
        rules = ExitRuleSet(take_profit_pct=0.05, take_profit_price=108.0)
        d = evaluate_exit(rules, PositionState(entry_price=100.0, last_price=105.0))
        assert d.should_exit and d.snapshot["line"] == 105.0

    def test_pct_only_reason_text_unchanged(self) -> None:
        """兼容钉子：只配 pct 时文案与历史逐字一致（桥 daemon 在跑同一份实现）。"""
        rules = ExitRuleSet(take_profit_pct=0.05)
        d = evaluate_exit(rules, PositionState(entry_price=100.0, last_price=105.0))
        assert d.reason == "止盈触发 现价105.00 ≥ 105.00"

    def test_absolute_only_reason_text(self) -> None:
        rules = ExitRuleSet(take_profit_price=110.0)
        d = evaluate_exit(rules, PositionState(entry_price=100.0, last_price=110.5))
        assert d.reason == "止盈触发 现价110.50 ≥ 110.00"

    def test_non_positive_is_ignored(self) -> None:
        """0 / 负数 = 该规则关闭（与 hard_stop_price 同口径），不触发。"""
        rules = ExitRuleSet(take_profit_price=0.0)
        d = evaluate_exit(rules, PositionState(entry_price=100.0, last_price=999.0))
        assert not d.should_exit

    def test_hard_stop_priority_unchanged(self) -> None:
        """优先级阶梯不受新字段影响：止损先于止盈。"""
        rules = ExitRuleSet(hard_stop_pct=0.05, take_profit_price=110.0)
        d = evaluate_exit(rules, PositionState(entry_price=100.0, last_price=94.0))
        assert d.rule_id == RULE_HARD_STOP


# --------------------------------------------------------------------------
# 2. check_sltp_trigger 透传（桥 daemon 与执行器共用同一份判定）
# --------------------------------------------------------------------------
class TestCheckSltpTriggerPassthrough:
    def test_passes_take_profit_price(self) -> None:
        ok, reason = check_sltp_trigger(110.0, 100.0, {"take_profit_price": 110.0})
        assert ok and "止盈" in reason

    def test_legacy_cfg_without_new_key_unchanged(self) -> None:
        """回归钉子：不带新键的旧 cfg（桥 daemon 现网形态）行为不变。"""
        ok, reason = check_sltp_trigger(105.0, 100.0, {"take_profit_pct": 0.05})
        assert ok and reason == "止盈触发 现价105.00 ≥ 105.00"
        not_ok, _ = check_sltp_trigger(104.0, 100.0, {"take_profit_pct": 0.05})
        assert not not_ok


# --------------------------------------------------------------------------
# 3. 零间隙棘轮（纯函数）
# --------------------------------------------------------------------------
class TestZeroGapRatchetPure:
    def test_move_to_equal_to_trigger_is_allowed(self) -> None:
        """``move_to == trigger`` = 隔壁 ``move_stop`` 语义（上触即把防守抬到该价）。"""
        new_stop, note = ratchet_stop_price(
            trigger=105.0, move_to=105.0, current=None, price=106.0
        )
        assert new_stop == 105.0
        assert note

    def test_move_to_above_trigger_still_rejected(self) -> None:
        """目标**高于**触发价才是真错单（抬完立刻低于现价）→ 仍 fail-closed。"""
        new_stop, note = ratchet_stop_price(
            trigger=105.0, move_to=105.5, current=None, price=106.0
        )
        assert new_stop is None
        assert note

    def test_zero_gap_never_lowers_existing_stop(self) -> None:
        """棘轮只升不降：承载位已到 106，目标 105 → 不动。"""
        new_stop, note = ratchet_stop_price(
            trigger=105.0, move_to=105.0, current=106.0, price=106.0
        )
        assert (new_stop, note) == (None, "")

    def test_gap_ratchet_behaviour_unchanged(self) -> None:
        """回归钉子：``move_to < trigger`` 的既有口径逐位不变。"""
        new_stop, _ = ratchet_stop_price(
            trigger=105.0, move_to=100.0, current=None, price=105.0
        )
        assert new_stop == 100.0
        new_stop2, note2 = ratchet_stop_price(
            trigger=105.0, move_to=100.0, current=None, price=104.9
        )
        assert (new_stop2, note2) == (None, "")


# --------------------------------------------------------------------------
# 4. 零间隙棘轮（执行器整轮）——同轮去抖
# --------------------------------------------------------------------------
class TestZeroGapRatchetCycle:
    def test_touch_does_not_sell_in_same_cycle(self) -> None:
        """上触 105 的**同一轮不卖**：防守位下一轮才生效。

        没有这条去抖，「抬到 105」与「现价 105 ≤ 防守位 105」在同一轮同时成立
        → 当场卖出，等于这个止盈保护白设。
        """
        rule = _rule_ext(move_stop_trigger=105.0, move_stop_to=105.0)
        h = Harness(
            cfg=_cfg([rule]),
            ticks={"600036.SH": {"lastPrice": 105.0}},
            positions=[_pos()],
        )
        summary = h.cycle()
        assert h.dispatched == []
        assert summary["triggered"] == 0
        assert h.state()["rules"]["600036.SH"]["stop_price"] == 105.0

    def test_fires_next_cycle_after_falling_back(self) -> None:
        rule = _rule_ext(move_stop_trigger=105.0, move_stop_to=105.0)
        h = Harness(
            cfg=_cfg([rule]),
            ticks={"600036.SH": {"lastPrice": 105.0}},
            positions=[_pos()],
        )
        h.cycle()
        h.client.ticks = {"600036.SH": {"lastPrice": 104.9}}
        summary = h.cycle()
        assert len(h.dispatched) == 1
        assert summary["triggered"] == 1

    def test_still_above_after_arm_does_not_fire(self) -> None:
        rule = _rule_ext(move_stop_trigger=105.0, move_stop_to=105.0)
        h = Harness(
            cfg=_cfg([rule]),
            ticks={"600036.SH": {"lastPrice": 106.0}},
            positions=[_pos()],
        )
        h.cycle()
        h.client.ticks = {"600036.SH": {"lastPrice": 105.5}}
        h.cycle()
        assert h.dispatched == []

    def test_zero_gap_does_not_rearm_every_cycle(self) -> None:
        """上触后防守位稳定在 105，不该每轮重复通知（棘轮幂等）。"""
        rule = _rule_ext(move_stop_trigger=105.0, move_stop_to=105.0)
        h = Harness(
            cfg=_cfg([rule]),
            ticks={"600036.SH": {"lastPrice": 106.0}},
            positions=[_pos()],
        )
        h.cycle()
        armed = [n for n in h.notices if "防守" in n["title"]]
        h.cycle()
        assert len([n for n in h.notices if "防守" in n["title"]]) == len(armed) == 1

    def test_gap_ratchet_cycle_behaviour_unchanged(self) -> None:
        """回归钉子：``m < t`` 的整轮行为与 P1.3 完全一致（不卖、落在目标价）。"""
        rule = _rule_ext(move_stop_trigger=105.0, move_stop_to=100.0)
        h = Harness(
            cfg=_cfg([rule]),
            ticks={"600036.SH": {"lastPrice": 106.0}},
            positions=[_pos()],
        )
        h.cycle()
        assert h.dispatched == []
        assert h.state()["rules"]["600036.SH"]["stop_price"] == 100.0


# --------------------------------------------------------------------------
# 5. 去抖与「跨日承载位作废」的交互（最危险的边界）
# --------------------------------------------------------------------------
class TestDeferralVsCarriedStopInvalidation:
    def test_invalidated_carried_stop_is_not_used_for_deferral(self) -> None:
        """承载位被判定作废后，本轮判定必须按**规则防守价**，不能拿作废的价位。

        构造：规则防守 95，承载位 120（上一个仓抬高的），持仓成本已从 50 变 100
        → ``carried_stop_invalid_reason`` 判作废。若实现是「进 ``_apply_ratchet``
        之前取一次防守价」，取到的会是 120 → 现价 100 ≤ 120 → **当场卖出新仓**，
        正是校验证要防的「新持仓被旧防守位秒杀」。
        """
        rule = _rule_ext(stop_loss_price=95.0)
        stale = {
            "date": DAY,
            "rules": {
                "600036.SH": {
                    "status": ex.ST_ARMED,
                    "stop_price": 120.0,
                    "stop_entry": 50.0,
                    "stop_volume": 500,
                }
            },
        }
        h = Harness(
            cfg=_cfg([rule]),
            ticks={"600036.SH": {"lastPrice": 100.0}},
            positions=[_pos(cost=100.0, can_use=1000)],
            state=stale,
        )
        h.cycle()
        assert h.dispatched == []
        assert "stop_price" not in (h.state()["rules"]["600036.SH"] or {})
        assert any("复位" in n["title"] for n in h.notices)

    def test_valid_carried_stop_still_used_for_deferral(self) -> None:
        """承载位**有效**时照常参与判定（去抖只影响棘轮抬升那一刻）。"""
        rule = _rule_ext()
        carried = {
            "date": DAY,
            "rules": {
                "600036.SH": {
                    "status": ex.ST_ARMED,
                    "stop_price": 102.0,
                    "stop_entry": 100.0,
                    "stop_volume": 1000,
                }
            },
        }
        h = Harness(
            cfg=_cfg([rule]),
            ticks={"600036.SH": {"lastPrice": 101.0}},
            positions=[_pos(cost=100.0, can_use=1000)],
            state=carried,
        )
        summary = h.cycle()
        assert len(h.dispatched) == 1
        assert summary["triggered"] == 1


# --------------------------------------------------------------------------
# 6. 规则层：字段归一化、整条拒绝、HTTP 契约
# --------------------------------------------------------------------------
class TestRuleLayer:
    def test_take_profit_price_survives_round_trip(self) -> None:
        rule = _rule_ext(take_profit_price=123.45)
        cfg = ex.merge_config(_cfg([rule]))
        assert cfg["rules"][0]["take_profit_price"] == 123.45

    def test_non_positive_take_profit_price_rejects_whole_rule(self) -> None:
        """0/负数是**非法**而非「关闭」：规则级字段写 0 是笔误，静默关掉等于没保护。"""
        rule = _rule_ext(take_profit_price=0.0)
        cfg = ex.merge_config(_cfg([rule]))
        assert cfg["rules"] == []
        assert (
            cfg["rejected_rules"] and cfg["rejected_rules"][0]["symbol"] == "600036.SH"
        )

    def test_null_take_profit_price_is_fine(self) -> None:
        rule = _rule_ext(take_profit_price=None)
        cfg = ex.merge_config(_cfg([rule]))
        assert len(cfg["rules"]) == 1

    def test_trigger_config_has_no_settings_page_fallback_for_price(self) -> None:
        """绝对价是规则级信息，**不回落设置页**（与 ``stop_loss_price`` 同口径）。

        回落的话，用户把设置页的止盈比例调高，会让 LLM 给的绝对价止盈线
        悄悄变成另一条线。
        """
        rule = _rule_ext(take_profit_price=120.0)
        tcfg = ex.trigger_config(
            ex.trigger_inputs(rule, {}),
            {"enabled": True, "take_profit_pct": 0.50},
        )
        assert tcfg["take_profit_price"] == 120.0

    def test_api_contract_rejects_non_positive(self) -> None:
        """API 与执行器同一份校验（``rule_reject_reason`` 单源）。"""
        from backend.services.trade.routers.qmt_sltp import SltpRule

        with pytest.raises(ValueError):
            SltpRule(symbol="600036.SH", take_profit_price=0.0)

    def test_api_contract_accepts_zero_gap_ratchet(self) -> None:
        """零间隙棘轮必须能过 API（隔壁 49.3% 的决策形态）。"""
        from backend.services.trade.routers.qmt_sltp import SltpRule

        rule = SltpRule(symbol="600036.SH", move_stop_trigger=105.0, move_stop_to=105.0)
        assert rule.move_stop_to == 105.0

    def test_api_contract_still_rejects_inverted_ratchet(self) -> None:
        from backend.services.trade.routers.qmt_sltp import SltpRule

        with pytest.raises(ValueError):
            SltpRule(symbol="600036.SH", move_stop_trigger=105.0, move_stop_to=105.5)
