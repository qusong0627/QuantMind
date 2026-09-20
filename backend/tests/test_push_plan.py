"""``backend/shared/push_plan.py`` 的纯逻辑单测。

这个模块决定「一键推送」实际会发出去什么，所以每条断言的落点都是**一个真实的错法**：
把「不建议」算成 1 手、把用户填的数悄悄改掉、把全局时段闸说成个股风险、
把 `skipped` 渲染成成功。边界覆盖按计划要求：空样本 / 单票 / 缺信号 / 零资金。
"""

from __future__ import annotations

import pytest

from backend.shared.push_plan import (
    QuantityPlan,
    align_buy_quantity,
    apply_batch_scale,
    batch_scale,
    choose_sell_source,
    classify_decisions,
    mirror_outcome_class,
    plan_quantity,
    scale_note,
    summarize_legs,
)


# --------------------------------------------------------------------------
# classify_decisions：环境闸 vs 标的闸
# --------------------------------------------------------------------------


class TestClassifyDecisions:
    def test_splits_l0_from_subject_level_rules(self):
        # Arrange：一份混合决策，含全局时段闸与个股资金闸
        decisions = [
            {"rule_id": "l0.session", "reason": "非交易时段"},
            {"rule_id": "l1.position_cap", "reason": "单票超 15%"},
            {"rule_id": "l3.stale_quote", "reason": "行情陈旧"},
            {"rule_id": "l6.orderbook", "reason": "盘口异常"},
        ]

        # Act
        out = classify_decisions(decisions)

        # Assert：l0 只归环境，其余全归标的
        assert [d["rule_id"] for d in out["environment"]] == ["l0.session"]
        assert [d["rule_id"] for d in out["subject"]] == [
            "l1.position_cap",
            "l3.stale_quote",
            "l6.orderbook",
        ]

    @pytest.mark.parametrize("payload", [None, []])
    def test_empty_payload_yields_two_empty_buckets(self, payload):
        # Arrange / Act
        out = classify_decisions(payload)

        # Assert：键必须齐（调用方直接下标取值），而不是返回 {} 或缺键
        assert out == {"environment": [], "subject": []}

    def test_decision_without_rule_id_is_treated_as_subject_level(self):
        """没有 rule_id 的决策归「标的」而不是「环境」——环境闸必须显式自证是 l0。

        反过来（缺 id 当环境）会让一条说不清来源的拒绝被当成「全场都这样」而免责。
        """
        # Arrange
        decisions = [{"reason": "来源不明的拒绝"}, {"rule_id": None, "reason": "空 id"}]

        # Act
        out = classify_decisions(decisions)

        # Assert
        assert out["environment"] == []
        assert len(out["subject"]) == 2

    def test_l0_prefix_requires_the_dot(self):
        """``l0x`` / ``l10.foo`` 不是环境闸 —— 前缀匹配必须带点，否则 l10/l0x 会误入。"""
        # Arrange
        decisions = [{"rule_id": "l0x.bogus"}, {"rule_id": "l00.typo"}]

        # Act
        out = classify_decisions(decisions)

        # Assert
        assert out["environment"] == []
        assert len(out["subject"]) == 2


# --------------------------------------------------------------------------
# align_buy_quantity：整手归一只发生在自动算量上
# --------------------------------------------------------------------------


class TestAlignBuyQuantity:
    def test_main_board_floors_down_to_whole_lots(self):
        # Arrange：主板 1234 股
        # Act
        qty, note = align_buy_quantity("600036.SH", 1234)

        # Assert：向下取到 1200，且必须留下说明（否则用户看到数量变了不知道为什么）
        assert qty == 1200
        assert "整手对齐" in note and "1234" in note

    def test_main_board_exact_lot_has_no_note(self):
        # Arrange / Act
        qty, note = align_buy_quantity("600036.SH", 1200)

        # Assert
        assert (qty, note) == (1200, "")

    def test_main_board_below_one_lot_returns_zero_with_reason(self):
        # Arrange：只买得起 99 股
        # Act
        qty, note = align_buy_quantity("600036.SH", 99)

        # Assert：必须是 0 且说清原因，绝不能凑成 1 手（那就是凭空放大仓位）
        assert qty == 0
        assert "不足 1 手" in note

    def test_star_board_allows_one_share_increment_above_200(self):
        # Arrange：科创板 688xxx，可买 233.7 股
        # Act
        qty, note = align_buy_quantity("688981.SH", 233.7)

        # Assert：1 股递增，不是 100 的整数倍
        assert qty == 233
        assert "科创板" in note

    def test_star_board_below_200_returns_zero(self):
        # Arrange：科创板只买得起 150 股
        # Act
        qty, note = align_buy_quantity("688981.SH", 150)

        # Assert
        assert qty == 0
        assert "200" in note

    def test_bj_board_uses_100_min_lot_with_one_share_increment(self):
        # Arrange：北交所 830799
        # Act
        qty, note = align_buy_quantity("830799.BJ", 137.9)

        # Assert：≥100 后按 1 股递增（与 lot_rules 的北交所口径一致）
        assert qty == 137
        assert "北交所" in note

    @pytest.mark.parametrize("symbol", ["600036.SH", "SH600036", "600036"])
    def test_code_form_does_not_change_the_board(self, symbol):
        """三种写法（后缀式/前缀式/裸码）必须解析出同一个板块。

        `resolve_board` 支持这三种形态，但候选项来自 parquet 是后缀式、
        `client_order_id` 里是裸码 —— 归一手数时若形态敏感就会静默差一个板块。
        """
        # Arrange / Act / Assert
        assert align_buy_quantity(symbol, 1234)[0] == 1200

    @pytest.mark.parametrize("raw", [0, -5, None])
    def test_non_positive_input_returns_zero(self, raw):
        # Arrange / Act
        qty, note = align_buy_quantity("600036.SH", raw)

        # Assert：非正数不产生说明（调用方另有 blocked 文案），但绝不放行
        assert (qty, note) == (0, "")


# --------------------------------------------------------------------------
# plan_quantity · 买入自动
# --------------------------------------------------------------------------


class TestPlanQuantityAutoBuy:
    def test_quantity_is_cash_times_score_over_price(self):
        # Arrange：10 万可用、仓位 50%、价 10 元 → 5000 股（整手）
        # Act
        plan = plan_quantity(
            symbol="600036.SH",
            side="buy",
            price=10.0,
            position_score=0.5,
            available_cash=100000.0,
        )

        # Assert：5000 股、auto、可执行，且说明里带出处（10 万 × 50% ÷ 10.00）
        assert plan.quantity == 5000
        assert plan.source == "auto"
        assert plan.executable is True
        assert "50%" in plan.note and "10.00" in plan.note

    def test_result_is_whole_lot_aligned(self):
        # Arrange：33333 元 / 10 元 × 1.0 = 3333.3 股
        # Act
        plan = plan_quantity(
            symbol="600036.SH",
            side="buy",
            price=10.0,
            position_score=1.0,
            available_cash=33333.0,
        )

        # Assert：归到 3300
        assert plan.quantity == 3300
        assert "整手对齐" in plan.note

    def test_zero_position_score_blocks_instead_of_buying_one_lot(self):
        """**本次最关键的一条**：仓位信号 0 = 引擎明确不入场。

        退化成「那就买 1 手」等于把「不建议」执行成「建议」—— 而且 0 股还能
        静默变成 100 股，是最不容易被发现的一类放大。
        """
        # Arrange / Act
        plan = plan_quantity(
            symbol="600036.SH",
            side="buy",
            price=10.0,
            position_score=0.0,
            available_cash=100000.0,
        )

        # Assert
        assert plan.quantity == 0
        assert plan.source == "blocked"
        assert plan.executable is False
        assert "不入场" in plan.problem

    def test_missing_position_score_blocks(self):
        # Arrange：该信号日未推理 / 缺基准
        # Act
        plan = plan_quantity(
            symbol="600036.SH",
            side="buy",
            price=10.0,
            position_score=None,
            available_cash=100000.0,
        )

        # Assert：缺失与 0 同判 —— 都不能当作「可以买」
        assert plan.executable is False
        assert "无仓位信号" in plan.problem

    def test_insufficient_cash_blocks_with_reason(self):
        # Arrange：1000 元买 10 元的票，只够 100 股？→ 100 股刚好 1 手
        # 改为 500 元：只够 50 股
        # Act
        plan = plan_quantity(
            symbol="600036.SH",
            side="buy",
            price=10.0,
            position_score=1.0,
            available_cash=500.0,
        )

        # Assert
        assert plan.quantity == 0
        assert plan.source == "blocked"
        assert "不足 1 手" in plan.problem

    @pytest.mark.parametrize(
        ("price", "cash", "fragment"),
        [
            (0.0, 100000.0, "无有效价格"),
            (None, 100000.0, "无有效价格"),
            (10.0, 0.0, "可用资金为 0"),
        ],
    )
    def test_degenerate_inputs_block_with_distinct_reasons(self, price, cash, fragment):
        # Arrange / Act
        plan = plan_quantity(
            symbol="600036.SH",
            side="buy",
            price=price,
            position_score=1.0,
            available_cash=cash,
        )

        # Assert：三种退化各有各的文案（否则用户无从下手）
        assert plan.executable is False
        assert fragment in plan.problem


# --------------------------------------------------------------------------
# plan_quantity · 卖出自动 / 手填
# --------------------------------------------------------------------------


class TestPlanQuantitySell:
    def test_sell_defaults_to_full_position(self):
        # Arrange：可用持仓 700 股
        # Act
        plan = plan_quantity(
            symbol="600036.SH",
            side="sell",
            price=10.0,
            position_score=0.5,
            available_cash=100000.0,
            available_position=700.0,
        )

        # Assert：全量卖出，且 700 不是整手也不报违规（碎股豁免）
        assert plan.quantity == 700
        assert plan.problem == ""
        assert plan.executable is True
        assert "整仓" in plan.note

    def test_sell_without_position_blocks(self):
        # Arrange：T+1 锁定或未持有
        # Act
        plan = plan_quantity(
            symbol="600036.SH",
            side="sell",
            price=10.0,
            position_score=0.5,
            available_cash=100000.0,
            available_position=0.0,
        )

        # Assert
        assert plan.executable is False
        assert "无可用持仓" in plan.problem

    def test_sell_side_is_case_insensitive(self):
        # Arrange / Act：候选行给的是 SELL 大写
        plan = plan_quantity(
            symbol="600036.SH",
            side="SELL",
            price=10.0,
            position_score=None,
            available_cash=0.0,
            available_position=300.0,
        )

        # Assert：走卖出分支而不是买入（买入分支会因缺信号/零资金 block）
        assert plan.quantity == 300
        assert plan.source == "auto"


class TestPlanQuantityManualOverride:
    def test_manual_quantity_is_preserved_even_when_not_whole_lot(self):
        """手填 150 股主板：原样保留 + 报违规，**不静默改成 100**。

        悄悄改掉用户输入的数等同于伪造回执 —— 用户以为买了 150，成交 100。
        """
        # Arrange / Act
        plan = plan_quantity(
            symbol="600036.SH",
            side="buy",
            price=10.0,
            position_score=0.5,
            available_cash=100000.0,
            override=150,
        )

        # Assert
        assert plan.quantity == 150
        assert plan.source == "manual"
        assert plan.executable is False
        assert "100" in plan.problem

    def test_manual_quantity_valid_passes_without_problem(self):
        # Arrange / Act
        plan = plan_quantity(
            symbol="600036.SH",
            side="buy",
            price=10.0,
            position_score=0.5,
            available_cash=100000.0,
            override=300,
        )

        # Assert
        assert (plan.quantity, plan.source, plan.problem) == (300, "manual", "")
        assert plan.executable is True

    def test_manual_override_wins_over_missing_signal(self):
        """手填数量时不看 position_score —— 用户显式指定即视为已确认。

        这条是刻意的：预检把 `position_score=None` 当阻断，但用户手填后仍能下单，
        否则「引擎没出信号」会连手动补单一起锁死。
        """
        # Arrange / Act
        plan = plan_quantity(
            symbol="600036.SH",
            side="buy",
            price=10.0,
            position_score=None,
            available_cash=100000.0,
            override=100,
        )

        # Assert
        assert plan.executable is True
        assert plan.source == "manual"

    def test_fractional_manual_quantity_is_flagged(self):
        # Arrange / Act
        plan = plan_quantity(
            symbol="600036.SH",
            side="buy",
            price=10.0,
            position_score=0.5,
            available_cash=100000.0,
            override=100.5,
        )

        # Assert：原值保留 + 违规说明
        assert plan.quantity == 100.5
        assert "整数股" in plan.problem

    @pytest.mark.parametrize("override", [0, -100])
    def test_non_positive_override_blocks(self, override):
        # Arrange / Act
        plan = plan_quantity(
            symbol="600036.SH",
            side="buy",
            price=10.0,
            position_score=0.5,
            available_cash=100000.0,
            override=override,
        )

        # Assert
        assert plan.source == "blocked"
        assert plan.executable is False


class TestManualFullPositionSell:
    """手填**全量**卖出碎股要放行：A 股零股只能随整仓一次性卖出。

    真实场景：持仓 246 股（送转/配股来的零股），用户手填 246 全卖掉——
    数量确实不是 100 的整数倍，但它不是「部分卖出」，柜台收。此前一律按
    「部分卖出需整手」拦下，用户看得见持仓却卖不出去。
    """

    def test_manual_quantity_covering_whole_position_passes(self):
        # Arrange：可用 246，用户手填 246
        # Act
        plan = plan_quantity(
            symbol="600036.SH",
            side="sell",
            price=10.0,
            position_score=0.5,
            available_cash=0.0,
            available_position=246.0,
            override=246,
        )

        # Assert：放行且不报违规
        assert plan.quantity == 246
        assert plan.source == "manual"
        assert plan.problem == ""
        assert plan.executable is True

    def test_manual_quantity_above_position_also_counts_as_full(self):
        """手填比可用还多（用户按总持仓填，T+1 锁了一部分）：按全量卖出体检放行，
        真实可卖量由后续腿数量/镜像侧二次校验兜底 —— 这里只判「形状对不对」。"""
        # Arrange / Act
        plan = plan_quantity(
            symbol="600036.SH",
            side="sell",
            price=10.0,
            position_score=0.5,
            available_cash=0.0,
            available_position=246.0,
            override=300,
        )

        # Assert
        assert plan.problem == ""

    def test_manual_partial_sell_still_requires_whole_lot(self):
        """真·部分卖出（数量 < 可用持仓）仍按整手拦——这条不能顺手放过。"""
        # Arrange：可用 346，用户只想卖 246
        # Act
        plan = plan_quantity(
            symbol="600036.SH",
            side="sell",
            price=10.0,
            position_score=0.5,
            available_cash=0.0,
            available_position=346.0,
            override=246,
        )

        # Assert
        assert plan.executable is False
        assert "整数倍" in plan.problem

    def test_manual_sell_without_known_position_stays_strict(self):
        """可用持仓取不到（模拟账户里没有这只票）时**不臆断**成全量卖出，仍按整手判。

        把「不知道持仓多少」当成「那就是全仓」，等于凭想象给用户放行一张碎股卖单。
        """
        # Arrange / Act
        plan = plan_quantity(
            symbol="600036.SH",
            side="sell",
            price=10.0,
            position_score=0.5,
            available_cash=0.0,
            available_position=0.0,
            override=246,
        )

        # Assert
        assert plan.executable is False
        assert "整数倍" in plan.problem


# --------------------------------------------------------------------------
# mirror_outcome_class：只有 ok/submitted 是成功
# --------------------------------------------------------------------------


class TestMirrorOutcomeClass:
    @pytest.mark.parametrize("status", ["ok", "submitted", "OK", " Submitted "])
    def test_success_words(self, status):
        # Arrange / Act / Assert
        assert mirror_outcome_class(status) == "success"

    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            ("queued", "queued"),
            ("duplicate", "duplicate"),
            ("skipped", "skipped"),
            ("failed", "failed"),
            ("error", "failed"),
        ],
    )
    def test_each_non_success_status_keeps_its_own_bucket(self, status, expected):
        # Arrange / Act / Assert：四类必须互相可区分，不能合并成「非成功」
        assert mirror_outcome_class(status) == expected

    @pytest.mark.parametrize("status", [None, "", "weird_future_status", "okay"])
    def test_unknown_status_is_fail_closed(self, status):
        """未知状态归失败。

        将来镜像新增状态词时，宁可显示成失败让用户自己看一眼，也不能默认落进「成功」——
        真钱路径上，把没发出去说成发出去了比说成失败贵得多。
        """
        # Arrange / Act / Assert
        assert mirror_outcome_class(status) == "failed"

    def test_drain_level_statuses_are_not_silently_successful(self):
        """drain 级状态（整批未发）绝不能算成功。

        ``outside_trading_hours`` / ``redis_unavailable`` / ``disabled`` 都是 drain
        的返回值，表示「整批没发出去」；若调用方误把它们当逐笔状态传进来，
        fail-closed 会如实报失败而不是成功。
        """
        # Arrange / Act / Assert
        for status in ("outside_trading_hours", "redis_unavailable", "disabled"):
            assert mirror_outcome_class(status) == "failed"


# --------------------------------------------------------------------------
# summarize_legs：确认面板底部那行计数
# --------------------------------------------------------------------------


class TestSummarizeLegs:
    def test_counts_only_executable_legs_but_reports_blocked(self):
        # Arrange：选 3 只，1 只被阻断
        legs = [
            {"executable": True, "amount": 10000.0},
            {"executable": True, "amount": 5000.5},
            {"executable": False, "amount": 0.0},
        ]

        # Act
        out = summarize_legs(legs)

        # Assert：总额只算可执行的 2 笔，但 total/blocked 如实报出 3 与 1
        assert out == {"total": 3, "executable": 2, "blocked": 1, "est_amount": 15000.5}

    def test_empty_selection_is_all_zeros(self):
        # Arrange / Act
        out = summarize_legs([])

        # Assert
        assert out == {"total": 0, "executable": 0, "blocked": 0, "est_amount": 0.0}

    def test_blocked_legs_with_amount_do_not_inflate_the_total(self):
        """被阻断的腿即使带着金额也不计入 —— 否则「预计金额」会虚高。"""
        # Arrange
        legs = [
            {"executable": True, "amount": 100.0},
            {"executable": False, "amount": 999999.0},
        ]

        # Act
        out = summarize_legs(legs)

        # Assert
        assert out["est_amount"] == 100.0

    def test_missing_amount_is_treated_as_zero(self):
        # Arrange：腿对象可能没带 amount 字段
        # Act
        out = summarize_legs([{"executable": True}])

        # Assert
        assert out["est_amount"] == 0.0
        assert out["executable"] == 1


# --------------------------------------------------------------------------
# QuantityPlan：executable 的组合语义
# --------------------------------------------------------------------------


class TestQuantityPlanExecutable:
    def test_blocked_source_is_never_executable(self):
        # Arrange / Act
        plan = QuantityPlan(quantity=100, source="blocked")

        # Assert：即使数量 > 0，blocked 也不可执行
        assert plan.executable is False

    def test_problem_makes_a_manual_plan_unexecutable(self):
        # Arrange / Act
        plan = QuantityPlan(
            quantity=150, source="manual", problem="主板部分卖出需为 100 股整数倍"
        )

        # Assert
        assert plan.executable is False

    def test_zero_quantity_is_not_executable(self):
        # Arrange / Act
        plan = QuantityPlan(quantity=0, source="auto")

        # Assert
        assert plan.executable is False

    def test_as_dict_round_trips_every_field(self):
        # Arrange
        plan = QuantityPlan(quantity=300, source="auto", note="说明", problem="")

        # Act
        out = plan.as_dict()

        # Assert：前端逐笔表格直接吃这个 dict，字段名不能少
        assert out == {
            "quantity": 300.0,
            "source": "auto",
            "note": "说明",
            "problem": "",
            "executable": True,
        }


# --------------------------------------------------------------------------
# batch_scale：整批资金约束
# --------------------------------------------------------------------------


class TestBatchScale:
    def test_no_over_commit_scales_by_one(self):
        # Arrange：三笔合计 30 万，可用 48 万

        # Act
        factor = batch_scale(480000, [100000, 100000, 100000])

        # Assert：不缩放（四舍五入到分的误差不该触发缩量）
        assert factor == 1.0

    def test_over_commit_scales_to_available_cash(self):
        # Arrange：实测场景 3×41.5 万 vs 可用 48.9 万

        # Act
        factor = batch_scale(488924, [414990, 415036, 415072])

        # Assert
        assert factor == pytest.approx(488924 / 1245098)

    def test_exact_fit_is_not_scaled(self):
        # Act / Assert：合计正好等于可用资金时不缩（等于阈值放行）
        assert batch_scale(300000, [100000, 100000, 100000]) == 1.0

    def test_zero_cash_scales_by_one_not_zero(self):
        # Arrange：账户没钱时量由 plan_quantity 逐笔阻断并说清原因，
        # 这里再乘以 0 会把「可用资金为 0」的说法换成一句莫名其妙的缩量说明

        # Act / Assert
        assert batch_scale(0, [100000]) == 1.0
        assert batch_scale(None, [100000]) == 1.0

    def test_empty_or_nonpositive_needs(self):
        # Act / Assert
        assert batch_scale(100000, []) == 1.0
        assert batch_scale(100000, [0, 0]) == 1.0
        assert batch_scale(100000, [-5]) == 1.0

    def test_factor_is_bounded_by_one(self):
        # Arrange：需求远小于资金时系数恒 1，绝不会被放大

        # Act / Assert
        assert batch_scale(10_000_000, [1000]) == 1.0

    def test_scale_note_is_human_readable(self):
        # Act
        note = scale_note(0.39253)

        # Assert
        assert "缩量" in note
        assert "0.3925" in note

    def test_scale_note_carries_before_and_after_shares(self):
        """带前后股数：note 里原本还有一句「整手对齐（81711.9→81700）」，
        那句说的是**缩放前**的数，缩量后必须给出新的数，否则用户一核对就以为算错了。"""
        # Act
        note = scale_note(0.589, before=81700, after=48100)

        # Assert
        assert "81700→48100" in note

    def test_scale_note_omits_arrow_when_quantity_unchanged(self):
        # Act：整手归一把缩放后的量又抬回原值时不写箭头（写了就是自相矛盾）
        note = scale_note(0.589, before=81700, after=81700)

        # Assert
        assert "→" not in note


# --------------------------------------------------------------------------
# apply_batch_scale：整批缩量的两遍处理
# --------------------------------------------------------------------------


def _buy_leg(symbol: str, qty: float, price: float, **over) -> dict:
    """一条买入腿的骨架（字段名与 `_build_legs` 产出的完全一致）。"""
    leg = {
        "symbol": symbol,
        "price": price,
        "quantity": qty,
        "amount": round(price * qty, 2),
        "source": "auto",
        "executable": True,
        "note": f"整手对齐（{qty}→{qty}）；可用资金 500,000 × 仓位 80% ÷ {price:.2f}",
        "problem": "",
        "blocked_by": "",
    }
    leg.update(over)
    return leg


class TestApplyBatchScale:
    def test_no_over_commit_returns_the_same_list(self):
        # Arrange：两只各 10 万，可用 50 万
        legs = [_buy_leg("600036.SH", 1000, 100.0), _buy_leg("600519.SH", 1000, 100.0)]

        # Act
        out, budget = apply_batch_scale(legs, 500000.0, "buy")

        # Assert：不缩量就原样返回（连复制都省），预算如实报出合计与可用
        assert out is legs
        assert budget["applied"] is False
        assert budget["factor"] == 1.0
        assert budget["planned_amount"] == 200000.0
        assert budget["available_cash"] == 500000.0

    def test_over_commit_scales_every_executable_auto_leg(self):
        # Arrange：两只各 40 万，可用 48 万 → 系数 0.6
        legs = [_buy_leg("600036.SH", 40000, 10.0), _buy_leg("600519.SH", 4000, 100.0)]

        # Act
        out, budget = apply_batch_scale(legs, 480000.0, "buy")

        # Assert
        assert budget["applied"] is True
        assert budget["planned_amount"] == 800000.0
        assert [x["quantity"] for x in out] == [24000, 2400]

    def test_blocked_leg_is_left_alone(self):
        """已被名单/新闻阻断的腿不花钱，缩它只会让这一行的数量与原因对不上。

        它的金额也**不进** `planned_amount`：把那笔根本不会下的单子算进合计，
        系数会偏小 —— 等于为了它腾资金，把真会下的那笔白白缩掉一截。
        """
        # Arrange：可执行的 80 万（自己就超了 48 万）+ 一笔已阻断的 40 万
        blocked = _buy_leg(
            "600606.SH",
            40000,
            10.0,
            executable=False,
            blocked_by="list",
            problem="在排除名单内",
        )
        live = _buy_leg("600036.SH", 80000, 10.0)

        # Act
        out, budget = apply_batch_scale([blocked, live], 480000.0, "buy")

        # Assert：阻断腿量一分未动；系数只看那 80 万（0.6），不看 120 万
        assert out[0] is blocked
        assert out[0]["quantity"] == 40000
        assert budget["planned_amount"] == 800000.0
        assert out[1]["quantity"] == 48000

    def test_manual_leg_counts_toward_the_budget_but_is_never_rescaled(self):
        """手填量参与「合计要花多少钱」的判定，但绝不被改数（纪律 2）。"""
        # Arrange：手填 40 万 + 自动 40 万，可用 48 万
        manual = _buy_leg("600036.SH", 40000, 10.0, source="manual", note="")
        auto = _buy_leg("600519.SH", 4000, 100.0)

        # Act
        out, budget = apply_batch_scale([manual, auto], 480000.0, "buy")

        # Assert：手填原样，自动被缩；planned 含手填那 40 万
        assert budget["planned_amount"] == 800000.0
        assert out[0]["quantity"] == 40000
        assert out[1]["quantity"] == 2400

    def test_original_list_and_dicts_are_not_mutated(self):
        # Arrange
        legs = [_buy_leg("600036.SH", 40000, 10.0), _buy_leg("600519.SH", 4000, 100.0)]
        before = [dict(x) for x in legs]

        # Act
        out, _ = apply_batch_scale(legs, 480000.0, "buy")

        # Assert：入参一字未改（调用方可能还要拿它做别的判断）
        assert legs == before
        assert out is not legs

    def test_scaled_leg_drops_the_stale_lot_alignment_clause(self):
        """note 里「整手对齐（81711.9→81700）」说的是**缩放前**的数。

        缩量后若照抄，用户按 note 核对数量会发现对不上 —— 看起来像系统自己改错了数。
        """
        # Arrange
        leg = _buy_leg("600023.SH", 81700, 5.08)
        leg["note"] = "整手对齐（81711.9→81700）；可用资金 488,924 × 仓位 85% ÷ 5.08"

        # Act
        out, _ = apply_batch_scale(
            [leg, _buy_leg("600558.SH", 87200, 4.76)], 488924.0, "buy"
        )

        # Assert：旧的整手说明没了，资金来源留着，续上带前后股数的缩量说明
        note = out[0]["note"]
        assert "整手对齐" not in note
        assert "可用资金 488,924 × 仓位 85% ÷ 5.08" in note
        assert "缩量" in note
        assert f"{81700}→{out[0]['quantity']:g}" in note

    def test_scaled_amount_follows_the_new_quantity(self):
        # Arrange
        leg = _buy_leg("600036.SH", 40000, 10.0)

        # Act
        out, _ = apply_batch_scale(
            [leg, _buy_leg("600519.SH", 4000, 100.0)], 480000.0, "buy"
        )

        # Assert：金额必须跟着改，否则汇总金额与逐笔数量自相矛盾
        assert out[0]["amount"] == round(10.0 * out[0]["quantity"], 2)

    def test_total_stays_within_available_cash(self):
        """端到端性质：缩量后 Σ(可执行腿金额) ≤ 可用资金（整手向下取整只会更小）。"""
        # Arrange：复刻实测场景 —— 3 只、可用 48.9 万、仓位分 0.849
        legs = [
            _buy_leg("600023.SH", 81700, 5.08),
            _buy_leg("600558.SH", 87200, 4.76),
            _buy_leg("600606.SH", 286200, 1.45),
        ]

        # Act
        out, _ = apply_batch_scale(legs, 488924.09, "buy")

        # Assert
        total = sum(x["amount"] for x in out if x["executable"])
        assert total <= 488924.09

    def test_scaled_below_one_lot_becomes_blocked_with_reason(self):
        # Arrange：4000 股 ×0.001 → 4 股，主板不足 1 手
        leg = _buy_leg("600036.SH", 4000, 10.0)
        other = _buy_leg("600519.SH", 4_000_000, 100.0)

        # Act
        out, _ = apply_batch_scale([leg, other], 100.0, "buy")

        # Assert：如实标 blocked 并说原因，绝不凑成 1 手
        assert out[0]["executable"] is False
        assert out[0]["source"] == "blocked"
        assert out[0]["blocked_by"] == "quantity"
        assert out[0]["quantity"] == 0
        assert "不足 1 手" in out[0]["problem"]

    def test_sell_side_is_never_scaled(self):
        """卖出缩量＝少卖：整仓卖出是用户明确的意图，且卖出不消耗可用资金。"""
        # Arrange
        legs = [_buy_leg("600036.SH", 40000, 10.0)]

        # Act
        out, budget = apply_batch_scale(legs, 100.0, "sell")

        # Assert
        assert out is legs
        assert budget["applied"] is False

    def test_empty_legs(self):
        # Act / Assert
        legs: list[dict] = []
        out, budget = apply_batch_scale(legs, 1000.0, "buy")
        assert out is legs
        assert budget == {"applied": False, "factor": 1.0}


# --------------------------------------------------------------------------
# choose_sell_source：卖出数量按通道取源（模拟台账 / 实盘直发）
# --------------------------------------------------------------------------


class TestChooseSellSource:
    def test_sim_position_wins_and_stays_on_the_sim_ledger(self):
        """模拟盘有票 → 一切照旧（模拟建单 + 可选镜像），实盘有多少都不改变本笔。"""
        # Arrange / Act
        plan = choose_sell_source(
            sim_available=246, real_available=246, real_requested=True
        )

        # Assert
        assert plan.available == 246
        assert plan.source == "sim"
        assert plan.exec_path == "sim"

    def test_real_only_position_goes_direct(self):
        """模拟盘没有、实盘有 → 直发实盘，数量取实盘可用量。"""
        # Arrange / Act
        plan = choose_sell_source(
            sim_available=0, real_available=300, real_requested=True
        )

        # Assert
        assert plan.available == 300
        assert plan.source == "real"
        assert plan.exec_path == "real_direct"
        assert "实盘" in plan.note

    def test_real_only_without_real_channel_tells_the_user_to_enable_it(self):
        """勾的是「只模拟盘」而票只在实盘：**不能**说「无可用持仓」——那是假话，
        用户看得见自己持有这只票。要给出可执行的下一步。"""
        # Arrange / Act
        plan = choose_sell_source(
            sim_available=0, real_available=300, real_requested=False
        )

        # Assert
        assert plan.available == 0
        assert plan.exec_path == ""
        assert "实盘" in plan.problem
        assert "通道" in plan.problem

    def test_nobody_holds_it_stays_a_plain_zero(self):
        """两个账户都没有 → 交回 ``plan_quantity`` 的通用阻断，不另编理由。"""
        # Arrange / Act
        plan = choose_sell_source(
            sim_available=0, real_available=0, real_requested=True
        )

        # Assert
        assert plan.available == 0
        assert plan.problem == ""

    def test_unknown_real_position_is_not_treated_as_held(self):
        """实盘持仓读不到（None）≠ 持有 0 股，但也不能凭想象放行一张真单。"""
        # Arrange / Act
        plan = choose_sell_source(
            sim_available=0, real_available=None, real_requested=True
        )

        # Assert
        assert plan.available == 0
        assert plan.exec_path == ""

    def test_overlap_notes_the_remainder_on_the_real_account(self):
        """同票两个账户都有：本笔只卖模拟台账的可用量，实盘多出来的部分是**事实**，
        要写出来（用户按「一键卖出」后发现自己还持有一大截会以为系统没卖）。"""
        # Arrange / Act
        plan = choose_sell_source(
            sim_available=100, real_available=500, real_requested=True
        )

        # Assert
        assert plan.source == "sim"
        assert plan.available == 100
        assert "400" in plan.note

    def test_sim_only_request_never_mentions_the_real_account(self):
        """没勾实盘通道就不该提实盘（用户会以为系统打算动真账户）。"""
        # Arrange / Act
        plan = choose_sell_source(
            sim_available=100, real_available=500, real_requested=False
        )

        # Assert
        assert plan.note == ""

    def test_empty_sim_position_with_unknown_real_and_no_channel(self):
        """两个来源都没数：不编理由，也不放行。"""
        # Arrange / Act
        plan = choose_sell_source(
            sim_available=0, real_available=None, real_requested=False
        )

        # Assert
        assert plan.available == 0
        assert plan.exec_path == ""
        assert plan.problem == ""
