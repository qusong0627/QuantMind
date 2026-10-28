"""决策提示词补块（``shared/decision_blocks``）单测：模型分数 + 市场状态。

两条纪律的守卫：

1. **绝不编数**——``rank_pct`` 缺失/越界不给「信号分」；regime 整包读不到、指数快照
   没取到、``inputs_ok=False``，全部如实降级并标注，绝不静默消失或补 0
   （``signal_score=0`` 的语义是「引擎明确不入场」，编 0 会被模型读成明确看空）。
2. **不可变 + 纯函数**——只吃快照对象，不连 Redis/库；行情补分返回新元组。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from backend.shared.decision.context import HoldingRow, PoolQuote
from backend.shared.decision_blocks import (
    CONSISTENCY_RULES,
    INDEX_CODES,
    SeriesSummary,
    attach_signal_scores,
    book_imbalance,
    build_cross_agent_block,
    build_freshness_block,
    build_indices_block,
    build_l2_block,
    build_market_state_block,
    build_minute_block,
    build_mirror_feedback_block,
    build_mirror_gates_block,
    build_news_block,
    build_orderbook_block,
    build_prev_round_block,
    build_score_leaders_block,
    build_signal_meta_block,
    build_tier_block,
    parse_brief_ts,
)

CST = timezone(timedelta(hours=8))
NOW = datetime(2026, 10, 14, 9, 35, tzinfo=CST)


def _quote(code: str = "600036.SH", signal_score: float | None = None) -> PoolQuote:
    return PoolQuote(
        code=code, name="测试票", price=40.0, age_min=0.5, signal_score=signal_score
    )


class TestAttachSignalScores:
    def test_fills_research_score_scale_from_rank_pct(self):
        # rank_pct 0.882 → 研究评分 88.2（与前端 researchScore.ts 同一公式/金样；
        # 渲染器行内格式 `· 信号分 {:.1f}` 是 0–100 刻度）
        quotes = (_quote(),)
        score_map = {"SH600036": {"value": 0.31, "rank_pct": 0.882}}
        out, filled = attach_signal_scores(quotes, score_map)

        assert filled == 1
        assert out[0].signal_score == 88.2
        assert quotes[0].signal_score is None  # 不可变：原对象不动

    def test_missing_or_out_of_range_rank_pct_gives_no_score(self):
        # 查无此票 / rank_pct=None / 越界（1.5——调用方把百分数当分位传了）
        # 一视同仁：不给分，也不给 0
        quotes = (_quote("600036.SH"), _quote("600519.SH"), _quote("000001.SZ"))
        score_map = {
            "SH600519": {"value": 0.2, "rank_pct": None},
            "SZ000001": {"value": 0.1, "rank_pct": 1.5},
        }
        out, filled = attach_signal_scores(quotes, score_map)

        assert filled == 0
        assert all(q.signal_score is None for q in out)

    def test_non_a_share_quote_is_untouched(self):
        quotes = (_quote("00700.HK"),)
        out, filled = attach_signal_scores(quotes, {"SH600036": {"rank_pct": 0.9}})

        assert filled == 0 and out[0].signal_score is None

    def test_entries_without_rank_pct_key_are_tolerated(self):
        # 老测试替身/老调用方给的条目没有 rank_pct 键也不能炸
        out, filled = attach_signal_scores((_quote(),), {"SH600036": {"value": 0.3}})

        assert filled == 0 and out[0].signal_score is None

    def test_empty_inputs(self):
        assert attach_signal_scores((), {"SH600036": {"rank_pct": 0.9}}) == ((), 0)
        out, filled = attach_signal_scores((_quote(),), None)
        assert filled == 0 and out[0].signal_score is None


class TestBuildSignalMetaBlock:
    META = {
        "ok": True,
        "model_id": "mdl_abc",
        "signal_date": "2026-10-14",
        "rows": 591,
        "realtime_rows": 591,
        "fallback": False,
    }

    def test_ok_path_carries_source_and_scale_note(self):
        text = build_signal_meta_block(self.META, filled=12, total=15)

        assert "【盘中模型信号】" in text
        assert "mdl_abc" in text and "2026-10-14" in text
        assert "覆盖 591 只" in text and "盘中实时行 591" in text
        # 口径句必须在：分数是该行所属批次内部的截面分位；快照可能混批（mode=off
        # 实测四批混合），绝不能写成「同一模型、同一次推理批次」的单一来源断言
        assert "该行所属推理批次内部的截面分位" in text
        assert "可能混合多个推理批次" in text
        assert "同一模型、同一次推理批次" not in text
        assert "本轮行情块 15 只中 12 只有分" in text

    def test_fallback_day_is_marked(self):
        text = build_signal_meta_block(
            {**self.META, "fallback": True}, filled=1, total=1
        )

        assert "回退日" in text

    def test_not_ok_renders_unavailable_with_reason(self):
        text = build_signal_meta_block(
            {"ok": False, "reason": "分数快照取数失败: db down"}, filled=0, total=15
        )

        assert "【盘中模型信号】本轮不可得" in text
        assert "分数快照取数失败: db down" in text
        assert "不要凭空引用模型分数" in text

    def test_ok_but_no_signal_date_is_also_unavailable(self):
        # ok=True 但没有信号日 = 表查得到、里面没数据（推理从未落库）——
        # 不能渲染成「覆盖 0 只」的正常快照
        text = build_signal_meta_block({"ok": True}, filled=0, total=15)

        assert "本轮不可得" in text

    def test_none_meta_is_unavailable(self):
        assert "本轮不可得" in build_signal_meta_block(None, filled=0, total=0)


class TestBuildMarketStateBlock:
    """``qm:regime:intraday`` 真形状 = 全字符串 Hash（含 ``str(None)`` 的字面
    "None"）。渲染必须逐字段容错：脏值至多丢一行，不许炸穿整块。"""

    REGIME = {
        "state": "bear",
        "breadth": (
            '{"coverage": "hot_set", "sample": 540, "up": 107, "down": 417,'
            ' "flat": 16, "limit_up": 9, "limit_down": 2}'
        ),
        "ret": "-0.0748",
        "window": "20",
        "vol": "0.0083",
        "position_hint": "0.3",
        "generated_at": NOW.isoformat(),
        "inputs_ok": "True",
        "index": "000300.SH",
        "notes": "[]",
    }
    INDEX_SNAP = {"Now": 4590.12, "PreClose": 4615.5, "timestamp": NOW.timestamp()}

    def test_full_real_shaped_hash(self):
        text = build_market_state_block(self.REGIME, self.INDEX_SNAP, NOW)

        assert text.startswith("【盘中市场状态】")
        assert "状态：偏弱（bear）" in text
        assert "建议仓位系数 0.3" in text
        assert "20 日收益 -7.48%" in text
        assert "日波动率（20 日窗口）0.83%" in text
        assert "指数 000300.SH：现价 ¥4590.12（-0.55%）" in text
        assert "涨 107 ／ 跌 417 ／ 平 16" in text
        assert "涨停 9 ／ 跌停 2" in text
        assert "热集样本 540" in text
        assert "快照生成于 2026-10-14 09:35:00（0 分钟前）" in text
        assert "超新鲜窗口" not in text

    def test_missing_regime_renders_unavailable(self):
        for regime in (None, {}):
            text = build_market_state_block(regime, None, NOW)
            assert "【盘中市场状态】本轮不可得" in text
            assert "不要凭空断言大盘强弱" in text

    def test_unrendered_index_says_so_instead_of_omitting(self):
        # 指数代码在、快照没取到：如实写「未取到（不参与定价）」——
        # 静默少一行会让模型以为「今天指数没动」
        text = build_market_state_block(self.REGIME, None, NOW)

        assert "指数 000300.SH：本轮行情未取到（不参与定价）" in text

    def test_stale_generated_at_is_marked(self):
        stale = {
            **self.REGIME,
            "generated_at": (NOW - timedelta(minutes=27)).isoformat(),
        }
        text = build_market_state_block(stale, None, NOW)

        assert "27 分钟前，超新鲜窗口：引擎可能停摆或非交易时段" in text

    def test_none_literal_window_does_not_crash(self):
        # 写侧 str(None) 会留下字面 "None"（实测 vratio 就是）——window 脏值
        # 只丢「N 日」前缀，不丢整块、也不出「（窗口窗口）」这种洋相
        dirty = {**self.REGIME, "window": "None"}
        text = build_market_state_block(dirty, None, NOW)

        assert "窗口收益 -7.48%" in text
        assert "日波动率0.83%" in text
        assert "（窗口窗口）" not in text

    def test_inputs_not_ok_marks_default_verdict(self):
        # inputs_ok=False 时写侧的 neutral 是**缺省值**不是判定值——标出来，
        # 防模型把缺省当结论
        text = build_market_state_block(
            {**self.REGIME, "state": "neutral", "inputs_ok": "False"}, None, NOW
        )

        assert "状态：中性（neutral）（输入不完整：以上为缺省判定）" in text

    def test_unknown_state_is_echoed_not_guessed(self):
        text = build_market_state_block({**self.REGIME, "state": "weird"}, None, NOW)

        assert "状态：weird" in text
        assert "（weird）" not in text  # 未知态不回显成「weird（weird）」

    def test_bad_breadth_json_drops_the_line_only(self):
        text = build_market_state_block(
            {**self.REGIME, "breadth": "{不是 JSON"}, None, NOW
        )

        assert "广度" not in text
        assert "状态：偏弱（bear）" in text  # 其余照常

    def test_notes_ride_the_stamp_line(self):
        regime = {
            **self.REGIME,
            "notes": '["volume_ratio_implausible(0.01)——量纲/单位可疑，弃用该输入"]',
        }
        text = build_market_state_block(regime, None, NOW)

        assert "注：volume_ratio_implausible(0.01)" in text

    def test_ts_epoch_fallback_for_stamp(self):
        # 没有 generated_at 时退 epoch 秒；两个都没有 = 不渲染时间行（但块还在）
        regime = {k: v for k, v in self.REGIME.items() if k != "generated_at"}
        regime["ts"] = NOW.timestamp()
        assert "快照生成于 2026-10-14 09:35:00" in build_market_state_block(
            regime, None, NOW
        )

        regime.pop("ts")
        text = build_market_state_block(regime, None, NOW)
        assert "快照生成于" not in text


class TestRendererIntegration:
    """补块经过 ``RebalanceContext`` 的落位：市场状态在大盘方向后、行情块里带
    信号分后缀；缺省（""）时整段不渲染——存量金样逐字节不变。"""

    def test_market_state_block_sits_after_direction_before_pool(self):
        from backend.shared.decision.context import (
            DirectionBlock,
            PoolRow,
            RebalanceContext,
            render_prompt,
        )

        ctx = RebalanceContext(
            agent="test-agent",
            now=NOW,
            direction=DirectionBlock(direction="弱势震荡"),
            pool=(PoolRow(code="600036.SH", name="招商银行"),),
            quotes=(_quote(signal_score=88.2),),
            market_state_block="【盘中市场状态】X",
            signal_meta_block="【盘中模型信号】Y",
        )
        text = render_prompt(ctx)

        assert "· 信号分 88.2" in text
        assert text.index("弱势震荡") < text.index("【盘中市场状态】X")
        assert text.index("【盘中市场状态】X") < text.index("【候选池】")
        assert text.index("【候选池实时行情") < text.index("【盘中模型信号】Y")

    def test_signal_meta_block_needs_the_quote_block(self):
        # 信号分后缀活在行情行里：行情块没渲染（无池/无报价）→ 出处块跟着不出现
        from backend.shared.decision.context import RebalanceContext, render_prompt

        ctx = RebalanceContext(
            agent="test-agent",
            now=NOW,
            quotes=(),
            market_state_block="【盘中市场状态】X",
            signal_meta_block="【盘中模型信号】Y",
        )
        text = render_prompt(ctx)

        assert "【盘中模型信号】Y" not in text
        assert "【盘中市场状态】X" in text  # 市场状态块不依赖行情块

    def test_defaults_keep_the_renderer_byte_neutral(self):
        from backend.shared.decision.context import (
            DirectionBlock,
            PoolRow,
            RebalanceContext,
            render_prompt,
        )

        base = {
            "agent": "test-agent",
            "now": NOW,
            "direction": DirectionBlock(direction="弱势震荡"),
            "pool": (PoolRow(code="600036.SH", name="招商银行"),),
            "quotes": (_quote(),),
        }
        without = render_prompt(RebalanceContext(**base))
        with_empty = render_prompt(
            RebalanceContext(**base, market_state_block="", signal_meta_block="")
        )

        assert without == with_empty
        assert "【盘中市场状态】" not in without
        assert "【盘中模型信号】" not in without


# ══════════════════════════════════════════════════════════════════════
# Stage 2 第二批：丁1–丁9 渲染器
#
# 同一组守卫：缺失一律显式「—/不可得」绝不编数；未接线 → 空串（字节冻结）；
# 坏值（指数五档垃圾大数）挡在渲染外但绝不静默——降级必须说出来。
# ══════════════════════════════════════════════════════════════════════


def _holding(code: str = "600036.SH", name: str = "招商银行") -> HoldingRow:
    return HoldingRow(
        code=code,
        name=name,
        volume=1000,
        cost=38.0,
        price=40.0,
        pnl_pct=5.26,
        day_chg=1.2,
        avail=1000,
    )


def _book_snap(
    *,
    now: float = 40.0,
    pre_close: float = 39.5,
    open_px: float | None = 39.8,
    bid1: float = 39.99,
    ask1: float = 40.01,
    bid_vol1: float = 300.0,
    ask_vol1: float = 100.0,
    extra_bids: tuple[tuple[float, float], ...] = (),
    extra_asks: tuple[tuple[float, float], ...] = (),
) -> dict:
    snap: dict = {
        "Now": now,
        "PreClose": pre_close,
        "bid1": bid1,
        "ask1": ask1,
        "bid_vol1": bid_vol1,
        "ask_vol1": ask_vol1,
    }
    if open_px is not None:
        snap["Open"] = open_px
    for i, (px, vol) in enumerate(extra_bids, start=2):
        snap[f"bid{i}"] = px
        snap[f"bid_vol{i}"] = vol
    for i, (px, vol) in enumerate(extra_asks, start=2):
        snap[f"ask{i}"] = px
        snap[f"ask_vol{i}"] = vol
    return snap


class TestBookImbalance:
    def test_computes_gross_imbalance(self):
        snap = _book_snap(extra_bids=((39.98, 300.0),), extra_asks=((40.02, 100.0),))
        # (600−200)/(600+200) = +0.5
        assert book_imbalance(snap) == 0.5

    def test_missing_half_of_book_is_none(self):
        # 只有买侧无卖侧 → 不可验证，绝不返回 +1.0 冒充单边强势
        assert book_imbalance({"bid1": 10.0, "bid_vol1": 100.0}) is None
        assert book_imbalance(None) is None
        assert book_imbalance({}) is None

    def test_index_garbage_prices_are_rejected(self):
        # 指数帧五档实测是垃圾大数（415065178112.0）→ 整体 None（不出失衡行）
        snap = _book_snap(
            bid1=415065178112.0,
            ask1=415065178112.0,
            bid_vol1=1.0,
            ask_vol1=1.0,
        )
        assert book_imbalance(snap) is None
        # 0 价同样不可验证（撮合价永非 0）
        assert book_imbalance(_book_snap(bid1=0.0)) is None

    def test_zero_total_volume_is_none(self):
        snap = _book_snap(bid_vol1=0.0, ask_vol1=0.0)
        assert book_imbalance(snap) is None


class TestBuildOrderbookBlock:
    def test_per_holding_line_with_book_gap_and_speed(self):
        snaps = {"600036.SH": _book_snap()}
        series = {"600036.SH": SeriesSummary(mom5m_pct=-0.42)}
        text = build_orderbook_block((_holding(),), snaps, series, NOW)

        assert text.startswith("【实时盘口】")
        assert "量比：5 日均量源未接入，不渲染" in text
        assert "[持仓] 招商银行(600036.SH) 现价 40.00（+1.27%）" in text
        assert "买 39.99×300" in text and "卖 40.01×100" in text
        assert "五档失衡 +50.0%" in text and "买盘强" in text
        assert "跳空 +0.76%（开 39.80）" in text
        assert "5分钟涨速 -0.42%" in text

    def test_renders_full_five_level_ladder(self):
        """两档以上必须全部喂给模型（2026-10-19 用户口径：L2 盘口都喂）。"""
        snap = _book_snap(
            extra_bids=((39.98, 200.0), (39.97, 100.0), (39.96, 50.0)),
            extra_asks=((40.02, 80.0), (40.03, 60.0)),
        )
        text = build_orderbook_block((_holding(),), {"600036.SH": snap}, {}, NOW)

        assert "买 39.99×300 39.98×200 39.97×100 39.96×50" in text
        assert "卖 40.01×100 40.02×80 40.03×60" in text

    def test_pool_rows_get_the_same_depth(self):
        """候选池与持仓同口径：带 [池] 标记、五档/失衡/跳空/成交额都渲染。"""
        snap = _book_snap(
            extra_bids=((39.98, 200.0),),
            extra_asks=((40.02, 80.0),),
        )
        snap["Amount"] = 35200.0  # 万元 → 3.52 亿
        text = build_orderbook_block((), {"600036.SH": snap}, {}, NOW, pool=(_quote(),))

        assert "[池] 测试票(600036.SH)" in text
        assert "买 39.99×300 39.98×200" in text
        assert "卖 40.01×100 40.02×80" in text
        # (500−180)/(500+180) = +47.1%
        assert "五档失衡 +47.1%" in text
        assert "成交额 3.52 亿" in text

    def test_pool_and_holding_overlap_is_rendered_once(self):
        """同一只票同时是持仓和池票 → 只出行一次（持仓口径优先）。"""
        text = build_orderbook_block(
            (_holding(),), {"600036.SH": _book_snap()}, {}, NOW, pool=(_quote(),)
        )

        assert text.count("(600036.SH)") == 1
        assert "测试票" not in text

    def test_missing_speed_renders_dash_not_zero(self):
        text = build_orderbook_block(
            (_holding(),), {"600036.SH": _book_snap()}, {}, NOW
        )
        assert "5分钟涨速 —" in text

    def test_unverifiable_book_renders_dash_not_silence(self):
        # 五档全是 0（指数垃圾/单边帧）→ 失衡行显式「—」，不编数也不消失
        snap = _book_snap(bid1=0.0, ask1=0.0, bid_vol1=0.0, ask_vol1=0.0)
        text = build_orderbook_block((_holding(),), {"600036.SH": snap}, {}, NOW)
        assert "五档失衡 —" in text

    def test_all_snapshots_missing_says_unavailable(self):
        text = build_orderbook_block((_holding(),), {}, {}, NOW)
        assert "【实时盘口】本轮不可得" in text
        assert "不要凭记忆里的价格判断盘口" in text

    def test_partial_missing_is_counted(self):
        snaps = {"600036.SH": _book_snap()}
        text = build_orderbook_block(
            (_holding(), _holding("600519.SH", "贵州茅台")), snaps, {}, NOW
        )
        assert "另有 1 只标的（持仓/池）本轮快照未取到" in text

    def test_no_holdings_and_no_pool_is_empty(self):
        # 无对象 → 空串；整段不进提示词（未接线/无对象一律字节中性）
        assert build_orderbook_block((), {"600036.SH": _book_snap()}, {}, NOW) == ""


class TestBuildMinuteBlock:
    def test_renders_series_summary(self):
        series = {
            "600036.SH": SeriesSummary(
                mom30m_pct=0.85,
                mom5m_pct=-0.42,
                vol5m_shares=123400.0,
                source="tdx_bridge",
            )
        }
        text = build_minute_block((_holding(),), series, NOW)

        assert "30 分钟 +0.85%" in text
        assert "5 分钟 -0.42%" in text
        assert "5 分钟量 1,234 手（同源差）" in text

    def test_empty_series_renders_unavailable(self):
        text = build_minute_block((_holding(),), {}, NOW)
        assert "【分钟特征】本轮不可得" in text

    def test_missing_code_in_series_says_dash(self):
        text = build_minute_block((_holding(),), {"600519.SH": SeriesSummary()}, NOW)
        assert "30 分钟 —" in text and "5 分钟量 —" in text

    def test_no_holdings_is_empty(self):
        assert build_minute_block((), {"600036.SH": SeriesSummary()}, NOW) == ""


class TestParseBriefTs:
    def test_aware_iso_parses(self):
        stamp = parse_brief_ts({"ts": "2026-10-14T09:00:00+08:00"})
        assert stamp == datetime(2026, 10, 14, 9, 0, tzinfo=CST).timestamp()

    def test_naive_and_garbage_are_none(self):
        # 契约是 aware ISO；naive 当坏值不猜时区
        assert parse_brief_ts({"ts": "2026-10-14T09:00:00"}) is None
        assert parse_brief_ts({"ts": "{不是时间}"}) is None
        assert parse_brief_ts({"ts": ""}) is None
        assert parse_brief_ts(None) is None
        assert parse_brief_ts({}) is None

    def test_epoch_number_accepted(self):
        assert parse_brief_ts({"ts": 1000.0}) == 1000.0


class TestBuildNewsBlock:
    BRIEF = {
        "ts": NOW.isoformat(),
        "holdings": [
            {
                "code": "600036.SH",
                "name": "招商银行",
                "verdict": "利空",
                "impact": 1.0,
                "event_type": "公告",
                "headline": "某利空",
                "source": "东财",
                "note": "关注",
            },
            {
                "code": "600519.SH",
                "name": "贵州茅台",
                "verdict": "利好",
                "impact": 2.0,
                "event_type": "业绩",
                "headline": "某利好（非持仓）",
            },
        ],
    }
    WATCH = {
        "ts": NOW.isoformat(),
        "items": [
            {"code": "000001.SZ", "ts": NOW.isoformat(), "why": "直线拉升"},
            {"code": "600036.SH", "ts": NOW.isoformat(), "why": "持仓的池外重复项须滤"},
        ],
    }

    def test_holding_hits_and_watch_pool_render(self):
        text = build_news_block(self.BRIEF, self.WATCH, ("600036.SH",), NOW)

        assert "[利空] 招商银行(600036.SH) 公告：某利空（东财）；关注" in text
        assert "贵州茅台" not in text  # 非持仓不进命中
        assert "池外异动 000001.SZ：直线拉升" in text
        assert "持仓的池外重复项须滤" not in text  # 持仓码不进池外段

    def test_no_holding_hit_says_so_not_silent(self):
        text = build_news_block(self.BRIEF, None, ("000002.SZ",), NOW)
        assert "本轮简报无持仓相关条目" in text

    def test_missing_file_says_unavailable(self):
        text = build_news_block(None, None, (), NOW)
        assert "【盘中新闻】本轮不可得" in text

    def test_unwired_is_empty(self):
        assert build_news_block(None, None, (), NOW, wired=False) == ""

    def test_stale_brief_marked(self):
        stale = {**self.BRIEF, "ts": (NOW - timedelta(hours=9)).isoformat()}
        text = build_news_block(stale, None, ("600036.SH",), NOW)
        assert "简报已 9.0 小时未更新，当旧闻读" in text

    def test_watch_capped_at_five(self):
        watch = {
            "items": [{"code": f"00000{i}.SZ", "why": f"异动{i}"} for i in range(1, 8)]
        }
        text = build_news_block(self.BRIEF, watch, (), NOW)
        assert "异动5" in text and "异动6" not in text


class TestBuildIndicesBlock:
    def test_renders_four_indices_and_sector_gated_line(self):
        snaps = {
            # Amount 口径 = 万元（TDX 快照契约），渲染折亿元
            "000300.SH": {"Now": 4590.12, "PreClose": 4615.5, "Amount": 34445960.0},
            "000001.SH": {"Now": 3300.0, "PreClose": 3289.0},
        }
        text = build_indices_block(snaps, NOW)

        assert "沪深300：4,590.12（-0.55%）｜成交额 3,444.60 亿" in text
        assert "上证指数：3,300.00（+0.33%）｜成交额 —" in text
        assert "深证成指：本轮未取到" in text
        assert "创业板指：本轮未取到" in text
        assert "板块涨幅源未接入" in text

    def test_index_codes_table(self):
        assert INDEX_CODES == ("000300.SH", "000001.SH", "399001.SZ", "399006.SZ")


L2_NOW = datetime(2026, 10, 14, 10, 14, tzinfo=CST)


def _holding(code: str = "600036.SH", name: str = "招商银行") -> HoldingRow:
    return HoldingRow(
        code=code,
        name=name,
        volume=1000,
        cost=9.0,
        price=9.45,
        pnl_pct=5.0,
        day_chg=None,
        avail=1000,
    )


def _pool(code: str, name: str = "测试票") -> PoolQuote:
    return PoolQuote(code=code, name=name, price=9.45, age_min=0.5)


def _l2_row(**over) -> dict:
    """真实 ``tdx_l2_snapshot`` 行形状（symbol=前缀码；factors 已解析成 dict）。"""
    row = {
        "symbol": "SH600036",
        "ts": L2_NOW - timedelta(seconds=38),
        "signal_score": 52.3,
        "factors": {
            "micro_zone_distribution": 0.14,
            "micro_vpin_vol_ratio": 0.21,
            "micro_vpin_amount_ratio": 0.18,
            "vol_price_divergence": 0.32,
            "micro_impact_decay_half_life": 0.27,
            "micro_liquidity_daily_pattern": 4.45,
            "micro_zone_rv_ratio_close": 0.62,
            "micro_zone_vol_ratio_T3": 0.33,
            # 采集侧未进入的时段恒 1.0——必须被时段门滤掉
            "micro_zone_vol_ratio_T4": 1.0,
            "micro_open_gap": -0.012,
        },
        "now_price": 9.45,
        "pre_close": 9.51,
        "cjbs": 13604,
        "b_order": 100.0,
        "b_cancel": 15.0,
        "s_order": 100.0,
        "s_cancel": 8.0,
    }
    row.update(over)
    return row


class TestBuildL2Block:
    """丁2【L2 微观结构】：未接线 → 历史门控字节；接线 → 通达信付费 L2 逐股快照。

    口径（2026-10-20 用户要求「付费通达信的数据为啥不用」）：采集侧
    ``tdx_l2_capture_task`` 每轮把 13 因子的 ICIR 加权**原始分**（未截面标准化）
    与快照写进 ``tdx_l2_snapshot``；本块只渲染 **15 分钟内的今日快照**，陈旧行
    按缺位处理并计数——陈旧微观结构当当前盘口用比没有更危险。
    """

    def test_unwired_keeps_the_legacy_gate_bytes(self):
        text = build_l2_block()
        assert "【L2 微观结构】本轮不可得" in text
        assert "五档失衡" in text and "代理" in text
        assert "通达信" not in text

    def test_wired_but_empty_is_a_different_unavailable_reason(self):
        # 「采集没产出」与「QM 没接源」是两件事：接线后不许再谎报未接入。
        text = build_l2_block({}, [_holding()], (), L2_NOW)
        assert "【L2 微观结构】本轮不可得" in text
        assert "通达信 L2" in text and "未接入盘中逐笔" not in text
        assert "1 只标的" in text

    def test_fresh_snapshot_renders_price_score_and_microstructure(self):
        text = build_l2_block({"SH600036": _l2_row()}, [_holding()], (), L2_NOW)
        assert "【L2 微观结构】（通达信付费 L2" in text
        # 原始分口径必须跟着走：跨股不可比，不能让人读成截面分位。
        assert "未做截面标准化" in text and "跨股不可比" in text
        assert "- [持仓] 招商银行(600036.SH) 现价 9.45（-0.63%）" in text
        assert "分 52.3（原始）" in text
        assert "快照 38s 前" in text
        assert "L2五档失衡 +0.14" in text
        assert "VPIN量/额 0.21/0.18" in text
        assert "量价背离 +0.32" in text
        assert "冲击衰减 0.27" in text
        assert "流动性日型 4.45" in text
        assert "近端波动 0.62" in text
        assert "开盘跳空 -1.20%" in text
        assert "委撤 买 15.0%/卖 8.0%" in text
        assert "成交 13,604 笔" in text

    def test_pool_rows_are_labelled_and_holdings_win_the_same_code(self):
        text = build_l2_block(
            {"SH600036": _l2_row(), "SZ000001": _l2_row(symbol="SZ000001")},
            [_holding()],
            [_pool("600036.SH"), _pool("000001.SZ", name="平安银行")],
            L2_NOW,
        )
        assert text.count("招商银行(600036.SH)") == 1
        assert "- [持仓] 招商银行(600036.SH)" in text
        assert "- [池] 平安银行(000001.SZ)" in text

    def test_future_zone_ratio_is_not_rendered(self):
        # 采集侧对**尚未进入**的时段 baseline=0 → ratio 恒 1.0（假值）；
        # 10:14 只应出 T3，T4/T5/T6 一律不渲染。
        text = build_l2_block({"SH600036": _l2_row()}, [_holding()], (), L2_NOW)
        assert "时段量比 T3 0.33" in text
        for zone in ("T4", "T5", "T6"):
            assert f"时段量比 {zone}" not in text

    def test_stale_snapshot_is_counted_not_rendered(self):
        stale = _l2_row(ts=L2_NOW - timedelta(seconds=1200))
        text = build_l2_block({"SH600036": stale}, [_holding()], (), L2_NOW)
        assert "本轮不可得" in text
        assert "超过 15 分钟" in text
        assert "现价" not in text  # 陈旧价不许冒充当前盘口

    def test_missing_factors_are_skipped_never_rendered_as_zero(self):
        row = _l2_row(factors={"micro_zone_distribution": None}, signal_score=None)
        text = build_l2_block({"SH600036": row}, [_holding()], (), L2_NOW)
        assert "L2五档失衡" not in text
        assert " · 分 " not in text  # 分缺失 → 整段不出，绝不写 0
        assert "VPIN" not in text

    def test_partial_coverage_counts_the_gaps(self):
        stale = _l2_row(symbol="SZ000001", ts=L2_NOW - timedelta(seconds=1000))
        text = build_l2_block(
            {"SH600036": _l2_row(), "SZ000001": stale},
            [_holding()],
            [_pool("000001.SZ", name="平安银行"), _pool("601000.SH", name="唐山港")],
            L2_NOW,
        )
        assert "另有 1 只标的（持仓/池）本轮无 L2 快照" in text
        assert "另有 1 只快照超 15 分钟未更新" in text


def _tier(**kw):
    from backend.shared.risk.tiers import TierState

    base = {
        "level": "defensive",
        "label": "防守",
        "source": "doc",
        "budget": {
            "leverage_max": 1.2,
            "per_stock_pct": 0.15,
            "max_new_buys": 2,
            "leverage_trim_to": 1.15,
            "per_stock_pos_pct": 0.25,
        },
    }
    base.update(kw)
    return TierState(**base)


class TestBuildTierBlock:
    def test_five_keys_render_with_zh_labels(self):
        text = build_tier_block(_tier())

        assert "【风险预算档位】（defensive·防守，来源 doc）" in text
        assert "杠杆上限 1.20" in text
        assert "单票买入上限 15%" in text
        assert "单轮最多新开 2 只" in text
        assert "减仓线（触达降至） 1.15" in text
        assert "单票持仓上限 25%" in text
        assert "按此档位收紧" in text

    def test_unconfigured_budget_still_renders_default_note(self):
        # 档位未配置照样出块：说「按默认防守」比让模型假设无约束强
        text = build_tier_block(_tier(level="", label="", budget={}, source="absent"))
        assert "档位未配置" in text and "默认防守参数" in text

    def test_problems_are_flagged(self):
        text = build_tier_block(_tier(problems=("文档日期缺失",)))
        assert "档位异常：文档日期缺失" in text

    def test_unwired_is_empty(self):
        assert build_tier_block(None) == ""


def _rec(
    *,
    id: str,
    agent: str = "a1",
    round_id: str = "rnd-1",
    action: str = "buy",
    code: str = "600036.SH",
    pct: float | None = 0.2,
    reason: str = "理由",
    minute: int = 30,
):
    from backend.shared.decision_ledger_store import DecisionRecord

    return DecisionRecord(
        id=id,
        pool_key="k",
        round_id=round_id,
        tenant_id="t",
        user_id="u",
        agent=agent,
        market="CN",
        trade_date=date(2026, 10, 14),
        decided_at=datetime(2026, 10, 14, 9, minute, tzinfo=CST),
        code=code,
        action=action,
        pct=pct,
        reason=reason,
    )


class TestBuildPrevRoundBlock:
    def test_first_round_says_no_prior(self):
        assert "今日首轮分析" in build_prev_round_block((), "a1", NOW)
        only_others = (_rec(id="x1", agent="a2"),)
        assert "今日首轮分析" in build_prev_round_block(only_others, "a1", NOW)

    def test_latest_round_of_own_agent_with_consistency_rules(self):
        rows = (
            _rec(id="x1", round_id="rnd-1", minute=5, action="buy"),
            _rec(
                id="x2",
                round_id="rnd-2",
                minute=40,
                action="sell",
                pct=1.0,
                reason="破位",
            ),
        )
        text = build_prev_round_block(rows, "a1", NOW)

        assert "09:40，1 条" in text
        assert "- 卖出 100% 600036.SH：破位" in text
        assert "09:05" not in text  # 只取最新一轮
        assert CONSISTENCY_RULES in text

    def test_hold_only_round_renders_explicitly(self):
        rows = (_rec(id="x1", action="hold", minute=12),)
        text = build_prev_round_block(rows, "a1", NOW)
        assert "09:12 一轮全部为 hold" in text

    def test_utc_decided_at_renders_beijing_time(self):
        """``decided_at`` 列是 TIMESTAMPTZ（aware-UTC）：直接 ``%H:%M`` 渲染的是
        UTC 墙钟（09:40 CST 显示成 01:40，2026-10-15 盘中审计）——必须转北京时间，
        且 naive 值按写入契约解释为 UTC。互看块同一口径。"""
        from backend.shared.decision_ledger_store import DecisionRecord

        row = DecisionRecord(
            id="u1",
            pool_key="k",
            round_id="rnd-u",
            tenant_id="t",
            user_id="u",
            agent="a1",
            market="CN",
            trade_date=date(2026, 10, 14),
            decided_at=datetime(2026, 10, 14, 1, 40, tzinfo=timezone.utc),
            code="600036.SH",
            action="buy",
            pct=0.2,
            reason="理由",
        )
        text = build_prev_round_block((row,), "a1", NOW)
        assert "（09:40，1 条）" in text
        assert "01:40" not in text
        assert "a1（09:40）：买入 20% 600036.SH" in build_cross_agent_block(
            (row,), "a2"
        )

    def test_naive_decided_at_is_read_as_utc(self):
        """naive 值（历史脏数据）按「无时区输入一律视为 UTC」的库契约解释。"""
        from backend.shared.decision_ledger_store import DecisionRecord

        row = DecisionRecord(
            id="u2",
            pool_key="k",
            round_id="rnd-n",
            tenant_id="t",
            user_id="u",
            agent="a1",
            market="CN",
            trade_date=date(2026, 10, 14),
            decided_at=datetime(2026, 10, 14, 1, 40),
            code="600036.SH",
            action="buy",
            pct=0.2,
            reason="理由",
        )
        assert "（09:40，1 条）" in build_prev_round_block((row,), "a1", NOW)


class TestBuildCrossAgentBlock:
    def test_lists_other_agents_latest_rounds(self):
        rows = (
            _rec(id="x1", agent="a2", round_id="rnd-1", minute=5),
            _rec(id="x2", agent="a2", round_id="rnd-2", minute=25, action="sell"),
            _rec(id="x3", agent="a1", round_id="rnd-2", minute=26),  # 自己不算
            _rec(id="x4", agent="a3", round_id="rnd-2", minute=27, action="hold"),
        )
        text = build_cross_agent_block(rows, "a1")

        assert "a2（09:25）：卖出 20% 600036.SH" in text
        assert "09:05" not in text
        assert "a3（09:27 一轮）：全 hold" in text
        assert "【其他 agent 动态】" in text

    def test_no_others_says_so(self):
        text = build_cross_agent_block((_rec(id="x1", agent="a1"),), "a1")
        assert "今日暂无可参考的他家决策" in text


class TestBuildFreshnessBlock:
    def test_empty_alerts_unwired_is_empty(self):
        assert build_freshness_block(()) == ""
        assert build_freshness_block(["", " "]) == ""

    def test_renders_downgrades(self):
        text = build_freshness_block(["行情序列缺成员：分钟动量不可得", "L2 无源"])
        assert text.startswith("【数据新鲜度警告】")
        assert "- 行情序列缺成员：分钟动量不可得" in text
        assert "- L2 无源" in text


class TestBuildMirrorGatesBlock:
    """P0-B【真单镜像闸门】（2026-10-15 审计）：生效约束进提示词；缺失项不编。

    背景：提示词的额度框架是「虚拟账本 × 比例」，真单镜像另有单笔/日累计硬上限
    ——不把生效值讲给模型，它就会系统性产出必被跳过的腿。
    """

    LIMITS = {
        "max_order_value": 10000.0,
        "max_daily_value": 50000.0,
        "max_daily_orders": 20,
        "max_daily_symbols": 5,
        "max_slippage_pct": 0.02,
    }

    def test_renders_effective_limits(self):
        text = build_mirror_gates_block(self.LIMITS)
        assert text.startswith("【真单镜像闸门】")
        assert "单笔金额上限 ¥10,000" in text
        assert "偏离 > 2% 的腿拒发" in text
        assert "金额 ≤ ¥50,000 ｜ 笔数 ≤ 20 ｜ 标的 ≤ 5 只" in text
        assert "虚拟账本额度 ≠ 真单额度" in text

    def test_missing_fields_are_omitted_not_invented(self):
        text = build_mirror_gates_block({"max_order_value": 8000.0})
        assert "¥8,000" in text
        assert "偏离" not in text
        assert "当日累计" not in text

    def test_empty_limits_still_warn_about_sizing(self):
        text = build_mirror_gates_block({})
        assert "【真单镜像闸门】" in text
        assert "虚拟账本额度 ≠ 真单额度" in text


class TestBuildMirrorFeedbackBlock:
    """P0-C【今日真单执行受阻】：读不到 ≠ 没有；行内事实全部来自台账。"""

    def test_unreadable_ledger_says_so(self):
        text = build_mirror_feedback_block(None)
        assert "读取失败" in text
        assert "读不到与没有是两件事" in text

    def test_no_skips_is_empty(self):
        assert build_mirror_feedback_block([]) == ""

    def test_renders_skip_lines_with_zh_reasons(self):
        text = build_mirror_feedback_block(
            (
                {
                    "symbol": "601985.SH",
                    "reason": "max_order_value",
                    "count": 1,
                    "side": "BUY",
                    "quantity": 1100,
                    "value": 10362.0,
                    "at": "2026-10-15T09:35:35",
                },
                {
                    "symbol": "600338.SH",
                    "reason": "price_drift",
                    "count": 2,
                    "side": "SELL",
                    "quantity": 800,
                    "drift": 2.15,
                    "at": "2026-10-15T09:38:19",
                },
            )
        )
        assert "被闸门跳过 3 次" in text
        assert "- 09:35 601985.SH 买入 1100 股（约 ¥10,362） · 单笔金额超上限" in text
        assert (
            "- 09:38 600338.SH 卖出 800 股（当日 2 次）（偏离 2.15%）"
            " · 委托价与参考价漂移超限" in text
        )

    def test_old_entries_without_amount_or_drift_still_render(self):
        """旧记录（写侧未带 value/drift）只少两格，行不出假数。"""
        text = build_mirror_feedback_block(
            (
                {
                    "symbol": "601985.SH",
                    "reason": "max_order_value",
                    "count": 1,
                    "side": "BUY",
                    "quantity": 1100,
                    "at": "2026-10-15T09:35:35",
                },
            )
        )
        assert "- 09:35 601985.SH 买入 1100 股 · 单笔金额超上限" in text
        assert "¥" not in text

    def test_unknown_reason_is_echoed_verbatim(self):
        text = build_mirror_feedback_block(
            (
                {
                    "symbol": "600036.SH",
                    "reason": "brand_new_gate",
                    "count": 1,
                    "side": "BUY",
                    "quantity": 100,
                    "at": "2026-10-15T09:00:00",
                },
            )
        )
        assert "brand_new_gate" in text

    def test_account_unavailable_family_is_translated(self):
        text = build_mirror_feedback_block(
            (
                {
                    "symbol": "600036.SH",
                    "reason": "account_unavailable:empty",
                    "count": 1,
                    "side": "BUY",
                    "quantity": 100,
                    "at": "2026-10-15T09:00:00",
                },
            )
        )
        assert "账户不可用（empty）" in text

    def test_renders_at_most_the_limit_and_counts_the_rest(self):
        skips = tuple(
            {
                "symbol": f"60000{i}.SH",
                "reason": "max_order_value",
                "count": 1,
                "at": f"2026-10-15T09:{i:02d}:00",
            }
            for i in range(12)
        )
        text = build_mirror_feedback_block(skips)
        assert "被闸门跳过 12 次" in text
        assert "另有 2 条" in text
        assert text.count("\n- ") == 11  # 10 行明细 + 1 行尾注


class TestScoreLeadersBlock:
    """2026-10-28「股票不限」：池外高分票要有榜面才看得见。"""

    SCORES = {
        "SH600036": {"value": 0.31, "rank_pct": 0.90},
        "SZ000001": {"value": 0.50, "rank_pct": 0.99},
        "SH000001": {"value": 0.90, "rank_pct": 1.0},  # 指数：名称索引无收录 → 不列
        "SZ300750": {"value": 0.20, "rank_pct": None},
        "SH600000": {"value": 0.10, "rank_pct": 0.50},
    }

    def test_ranks_by_score_and_skips_unresolvable_names(self):
        out = build_score_leaders_block(self.SCORES)
        assert "模型分数榜" in out
        # 指数 0.90 被名称索引过滤，榜首是 000001.SZ 0.50
        assert out.index("000001.SZ") < out.index("600036.SH")
        assert out.index("600036.SH") < out.index("300750.SZ")
        assert "SH000001" not in out

    def test_limit_and_missing_rank_renders_dash(self):
        out = build_score_leaders_block(self.SCORES, limit=2)
        assert "000001.SZ" in out and "600036.SH" in out
        assert "300750.SZ" not in out

    def test_empty_scores_render_nothing(self):
        assert build_score_leaders_block({}) == ""
        assert build_score_leaders_block(None) == ""
