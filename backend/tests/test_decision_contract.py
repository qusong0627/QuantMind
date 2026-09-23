"""P2.1a：决策契约与解析（纯核心，不碰 LLM）。

背景（迁移计划 §P2 落点表 + `p2-recon-llm-trade.md` §5）：隔壁 BayMax 的 LLM 决策
有两条解析链、两套字段集，且**各自实现**——`live_llm_trade.parse_decision`（4 字段）
与 `live_prompt_context.parse_intraday_decision`（8 字段含 `watch`）。两条链共用一个
抽取器后仍保留了两份字段代码，且既有 3 个已被真实事故验证过的硬口径：

1. **`pct` 三态**（2026-09-12 审查 HIGH-1/HIGH-2/HIGH-4）——`missing`（没表达）与
   `dirty`（表达了但读不出）与 `0` 必须**可区分**。`missing` 在卖出链按清仓执行，
   `dirty` 必须停手留痕；把「想减 30%」静默执行成清仓是资金方向上不可逆的放大。
   `NaN`/`Infinity` 归 dirty 而非 missing——JSON 允许字面量 `NaN`，按 missing 走
   整仓卖出，方向与 dirty 的停手原则正好相反。
2. **抽取器要容忍散文夹 JSON**（2026-09-12 P0-5）——09-11 09:35 `deepseek-v4-pro`
   输出散文 + JSON，旧抽取器只认「整段 / 围栏」→ 解析失败 → **当天该 agent 0 买 0 卖**。
   同一份文本喂另一条链就能取出来，因为那条链多一个「括号平衡块」回退。
3. **空 `decisions` 不是成功**——校验器要求至少一条**合法 action** 的行；空数组或
   全是未知 action 的块继续往后找，找不到才算失败。否则模型吐一句「暂无操作」就会被
   当成「本轮无决策」静默跳过，事后无法解释「今天为什么零交易」。

本仓的处置：两条链**合成一个**按 schema 参数化的解析器（`parse_decisions`），
把上述三条口径固化成类型与返回值，而不是散在两个文件的调用点里各判一次。
"""

from __future__ import annotations

import math

import pytest

from backend.shared.decision.contract import (
    FRAC_DIRTY,
    FRAC_MISSING,
    FRAC_OK,
    FRAC_ZERO,
    PCT_DIRTY,
    PCT_GIVEN,
    PCT_MISSING,
    SCHEMA_INTRADAY,
    SCHEMA_REBALANCE,
    STATUS_API_FAILED,
    STATUS_EMPTY_OUTPUT,
    STATUS_OK,
    STATUS_PARSE_FAILED,
    INTRADAY_SCHEMA_JSON,
    JSON_ONLY_HINT,
    REBALANCE_SCHEMA_JSON,
    Decision,
    DecisionBatch,
    parse_decisions,
    parse_pct,
)
from backend.shared.decision.json_extract import extract_json

# --------------------------------------------------------------------------
# 1. pct 三态（既有事故口径，逐条钉住）
# --------------------------------------------------------------------------


class TestPctThreeState:
    def test_none_is_missing(self) -> None:
        p = parse_pct(None)
        assert (p.state, p.value, p.raw) == (PCT_MISSING, 0.0, "")

    @pytest.mark.parametrize("raw", ["", "   ", "N/A", "n/a"])
    def test_blank_and_na_are_missing(self, raw: str) -> None:
        assert parse_pct(raw).state == PCT_MISSING

    @pytest.mark.parametrize(
        "raw,expect", [(0.3, 0.3), ("0.3", 0.3), (1, 1.0), ("1", 1.0)]
    )
    def test_plain_numbers_are_given(self, raw: object, expect: float) -> None:
        p = parse_pct(raw)
        assert (p.state, p.value) == (PCT_GIVEN, expect)

    @pytest.mark.parametrize("raw", ["30%", "30％", " 30% "])
    def test_percent_string_normalized(self, raw: str) -> None:
        """全角 ％ 与空白都要规范化；`"30%"` → 0.3（模型确实表达了比例，尽量读出来）。"""
        p = parse_pct(raw)
        assert (p.state, p.value) == (PCT_GIVEN, 0.3)

    @pytest.mark.parametrize("raw", ["0.3股", "三成", [0.3], {"a": 1}, "abc"])
    def test_unparsable_is_dirty_with_raw_kept(self, raw: object) -> None:
        p = parse_pct(raw)
        assert p.state == PCT_DIRTY
        assert p.value == 0.0
        assert p.raw, "dirty 必须带回原始值，否则消费点无法留痕"

    def test_nan_is_dirty_not_missing(self) -> None:
        """**方向**：NaN 是「表达了但没法用」。按 missing 处理会走整仓卖出。"""
        assert parse_pct(float("nan")).state == PCT_DIRTY
        assert parse_pct("NaN").state == PCT_DIRTY

    @pytest.mark.parametrize("raw", [float("inf"), float("-inf"), "Infinity", "1e999"])
    def test_non_finite_is_dirty(self, raw: object) -> None:
        assert parse_pct(raw).state == PCT_DIRTY

    @pytest.mark.parametrize("raw", [True, False])
    def test_bool_is_dirty(self, raw: bool) -> None:
        """JSON 的 `true` 不是比例。`float(True) == 1.0` 会让 `pct: true` 静默变成清仓。"""
        p = parse_pct(raw)
        assert p.state == PCT_DIRTY
        assert p.value == 0.0

    def test_explicit_zero_is_given_zero(self) -> None:
        p = parse_pct(0)
        assert (p.state, p.value) == (PCT_GIVEN, 0.0)

    def test_raw_is_truncated(self) -> None:
        """原始值进日志/审计表，必须先截断（模型可能吐一整段理由进 pct）。"""
        assert len(parse_pct("x" * 500).raw) <= 40


# --------------------------------------------------------------------------
# 2. 抽取器（09-11 事故的直接回归）
# --------------------------------------------------------------------------


class TestExtractJson:
    def test_whole_text_is_json(self) -> None:
        assert extract_json('{"a": 1}') == {"a": 1}

    def test_json_fence(self) -> None:
        text = '好的，这是结果：\n```json\n{"a": 1}\n```\n以上。'
        assert extract_json(text) == {"a": 1}

    def test_prose_wrapped_braces(self) -> None:
        """09-11 事故形态：散文里夹一个 JSON 对象（无围栏）。"""
        text = '我先看了持仓，然后决定：{"decisions": [{"action": "hold"}]} 就这样。'
        assert extract_json(text) == {"decisions": [{"action": "hold"}]}

    def test_brace_inside_string_does_not_truncate(self) -> None:
        """朴素括号计数会在这里截断（`}` 在字符串里）。状态机必须不数它。"""
        text = '说明如下 {"reason": "涨到 17.9 } 减半", "pct": 0.5} 完毕'
        assert extract_json(text) == {"reason": "涨到 17.9 } 减半", "pct": 0.5}

    def test_escaped_quote_inside_string(self) -> None:
        text = r'{"reason": "他说 \"卖\""}'
        assert extract_json(text) == {"reason": '他说 "卖"'}

    def test_first_block_failing_want_falls_through(self) -> None:
        """块一不满足校验器时继续往后找——不是「取第一个 JSON 就完事」。"""
        text = '{"decisions": []} 再来 {"decisions": [{"action": "buy"}]}'
        got = extract_json(text, want=lambda d: bool(d.get("decisions")))
        assert got == {"decisions": [{"action": "buy"}]}

    def test_non_dict_json_is_not_a_hit(self) -> None:
        assert extract_json("[1, 2, 3]") is None

    @pytest.mark.parametrize("text", [None, "", "   ", "没有任何 JSON 的纯文本"])
    def test_no_json_returns_none(self, text: str | None) -> None:
        assert extract_json(text) is None

    def test_never_raises(self) -> None:
        assert extract_json('{"a": ') is None


# --------------------------------------------------------------------------
# 3. 解析成决策（两套 schema 合一）
# --------------------------------------------------------------------------


def _intraday_payload(*rows: dict) -> str:
    import json

    return json.dumps({"decisions": list(rows)}, ensure_ascii=False)


class TestParseDecisions:
    def test_full_intraday_row(self) -> None:
        batch = parse_decisions(
            _intraday_payload(
                {
                    "action": "watch",
                    "code": "600519.SH",
                    "name": "贵州茅台",
                    "pct": 0.2,
                    "stop_loss": 1500.0,
                    "take_profit": 1650.0,
                    "move_stop": 1520.0,
                    "invalidation": "跌破1500或买入理由失效",
                    "confidence": 0.8,
                    "risk_amount": 2000.0,
                    "reason": "证据",
                }
            ),
            schema=SCHEMA_INTRADAY,
        )
        assert batch.status == STATUS_OK and batch.ok
        (d,) = batch.decisions
        assert isinstance(d, Decision)
        assert d.action == "watch"
        assert d.code == "600519.SH"
        assert (d.stop_loss, d.take_profit, d.move_stop) == (1500.0, 1650.0, 1520.0)
        assert d.invalidation == "跌破1500或买入理由失效"
        assert d.confidence == 0.8
        assert d.risk_amount == 2000.0
        assert d.pct.value == 0.2 and d.pct.state == PCT_GIVEN

    def test_watch_rejected_in_rebalance_schema(self) -> None:
        """调仓轮的 schema 里没有 watch——不能静默当 hold 处理。"""
        batch = parse_decisions(
            _intraday_payload({"action": "watch", "code": "600519.SH"}),
            schema=SCHEMA_REBALANCE,
        )
        assert batch.status == STATUS_PARSE_FAILED
        assert batch.decisions == ()
        assert "watch" in batch.ignored_actions

    def test_watch_accepted_in_intraday_schema(self) -> None:
        batch = parse_decisions(
            _intraday_payload({"action": "watch", "code": "600519.SH"}),
            schema=SCHEMA_INTRADAY,
        )
        assert batch.ok and batch.decisions[0].action == "watch"

    def test_action_and_code_normalized(self) -> None:
        batch = parse_decisions(
            _intraday_payload({"action": " BUY ", "code": "  600519.SH  "}),
            schema=SCHEMA_REBALANCE,
        )
        assert (batch.decisions[0].action, batch.decisions[0].code) == (
            "buy",
            "600519.SH",
        )

    def test_unknown_action_counted_not_silently_dropped(self) -> None:
        """模型产了 3 行、2 行 action 非法 —— 这个数字必须看得见。"""
        batch = parse_decisions(
            _intraday_payload(
                {"action": "hold", "code": "600519.SH"},
                {"action": "学猫叫", "code": "600036.SH"},
                {"action": "", "code": ""},
            ),
            schema=SCHEMA_REBALANCE,
        )
        assert batch.ok
        assert len(batch.decisions) == 1
        assert batch.skipped_rows == 2

    def test_non_dict_rows_skipped(self) -> None:
        import json

        batch = parse_decisions(
            json.dumps(
                {"decisions": ["hold", 42, {"action": "buy", "code": "600036.SH"}]}
            ),
            schema=SCHEMA_REBALANCE,
        )
        assert batch.ok and batch.skipped_rows == 2

    @pytest.mark.parametrize("bad", [0, -1.5, "N/A", None, float("nan"), True])
    def test_bad_price_fields_become_none(self, bad: object) -> None:
        """价位字段坏了按「没给」处理（不炸整轮）；`_num` 只接受 > 0 的有限数。"""
        batch = parse_decisions(
            _intraday_payload(
                {
                    "action": "watch",
                    "code": "600519.SH",
                    "stop_loss": bad,
                    "take_profit": bad,
                }
            ),
            schema=SCHEMA_INTRADAY,
        )
        (d,) = batch.decisions
        assert (d.stop_loss, d.take_profit) == (None, None)

    def test_bad_confidence_defaults_without_failing_round(self) -> None:
        batch = parse_decisions(
            _intraday_payload(
                {"action": "hold", "code": "600519.SH", "confidence": "高"}
            ),
            schema=SCHEMA_INTRADAY,
        )
        assert batch.ok and batch.decisions[0].confidence == 0.0

    def test_missing_optional_fields_have_neutral_defaults(self) -> None:
        (d,) = parse_decisions(
            _intraday_payload({"action": "hold", "code": "600519.SH"}),
            schema=SCHEMA_REBALANCE,
        ).decisions
        assert (d.name, d.invalidation, d.reason) == ("", "", "")
        assert d.pct.state == PCT_MISSING

    def test_dirty_pct_survives_into_decision(self) -> None:
        (d,) = parse_decisions(
            _intraday_payload({"action": "sell", "code": "600519.SH", "pct": "0.3股"}),
            schema=SCHEMA_REBALANCE,
        ).decisions
        assert d.pct.state == PCT_DIRTY and d.pct.raw

    def test_empty_text_is_empty_output(self) -> None:
        batch = parse_decisions("", schema=SCHEMA_REBALANCE)
        assert batch.status == STATUS_EMPTY_OUTPUT and not batch.ok

    def test_unparsable_text_is_parse_failed(self) -> None:
        batch = parse_decisions(
            "模型今天心情不好，什么都没说。", schema=SCHEMA_REBALANCE
        )
        assert batch.status == STATUS_PARSE_FAILED

    def test_failed_parse_keeps_row_diagnostics(self) -> None:
        """解析失败也要留下「失败长什么样」——重试往往原样重演，只报 parse_failed 查不出因。

        「模型压根没吐 JSON」（skipped=0）与「吐了 3 行、action 全不认识」（skipped=3）
        的处置完全不同：前者改提示词/查模型，后者是 schema 串了。
        """
        batch = parse_decisions(
            _intraday_payload(
                {"action": "加仓", "code": "600519.SH"},
                {"action": "减仓", "code": "600036.SH"},
                {"action": "观望", "code": "000001.SZ"},
            ),
            schema=SCHEMA_REBALANCE,
        )
        assert batch.status == STATUS_PARSE_FAILED
        assert batch.skipped_rows == 3
        assert batch.ignored_actions == ("加仓", "减仓", "观望")

    def test_no_json_at_all_has_no_diagnostics(self) -> None:
        batch = parse_decisions("今天大盘不好，我选择观望。", schema=SCHEMA_REBALANCE)
        assert (batch.status, batch.skipped_rows, batch.ignored_actions) == (
            STATUS_PARSE_FAILED,
            0,
            (),
        )

    def test_empty_decisions_array_is_not_success(self) -> None:
        """`{"decisions": []}` 不是「本轮无操作」——它取不出任何合法决策。"""
        assert (
            parse_decisions('{"decisions": []}', schema=SCHEMA_REBALANCE).status
            == STATUS_PARSE_FAILED
        )

    def test_api_failed_status_helper(self) -> None:
        batch = DecisionBatch.api_failed(SCHEMA_REBALANCE)
        assert batch.status == STATUS_API_FAILED
        assert batch.raw == "" and batch.decisions == ()

    def test_raw_text_kept_for_audit(self) -> None:
        text = _intraday_payload({"action": "hold", "code": "600519.SH"})
        assert parse_decisions(text, schema=SCHEMA_REBALANCE).raw == text

    def test_raw_text_truncated(self) -> None:
        """留痕要有限度：模型可能吐几十 KB 散文，审计表不能跟着爆。"""
        text = "x" * 50_000 + '\n{"decisions": [{"action": "hold", "code": "1.SH"}]}'
        assert len(parse_decisions(text, schema=SCHEMA_REBALANCE).raw) <= 8_000

    def test_prose_wrapped_round_trip(self) -> None:
        """09-11 事故的端到端形态：散文 + 围栏 + 合法决策 → 必须解析成功。"""
        text = (
            "我先看了持仓和候选池：\n```json\n"
            '{"decisions": [{"action": "buy", "code": "600036.SH", "pct": 0.3}]}\n'
            "```\n以上是我的判断。"
        )
        batch = parse_decisions(text, schema=SCHEMA_REBALANCE)
        assert batch.ok and batch.decisions[0].pct.value == 0.3


# --------------------------------------------------------------------------
# 4. 执行比例（missing/dirty 在买卖两侧方向不同）
# --------------------------------------------------------------------------


def _sell(pct_raw: object) -> Decision:
    return parse_decisions(
        _intraday_payload({"action": "sell", "code": "600519.SH", "pct": pct_raw}),
        schema=SCHEMA_REBALANCE,
    ).decisions[0]


def _buy(pct_raw: object) -> Decision:
    return parse_decisions(
        _intraday_payload({"action": "buy", "code": "600519.SH", "pct": pct_raw}),
        schema=SCHEMA_REBALANCE,
    ).decisions[0]


class TestFractions:
    def test_sell_missing_pct_means_full_exit(self) -> None:
        """模型明说「卖出」只是漏了比例 → 按清仓执行。

        旧代码 `min(max(pct,0),1)` 在缺失时算出 0 股 → 打一行「无合法可卖量」跳过，
        即模型让卖、系统静默不卖。
        """
        assert _sell(None).sell_intent() == (1.0, FRAC_MISSING)

    def test_sell_dirty_pct_means_stop(self) -> None:
        """脏值停手：把「想减 30%」执行成清仓是不可逆的方向放大。"""
        assert _sell("0.3股").sell_intent() == (0.0, FRAC_DIRTY)

    def test_sell_explicit_zero_means_hold(self) -> None:
        assert _sell(0).sell_intent() == (0.0, FRAC_ZERO)

    def test_sell_value_clamped(self) -> None:
        assert _sell(1.7).sell_intent() == (1.0, FRAC_OK)

    def test_sell_negative_is_zero_not_dirty(self) -> None:
        """负比例是「无意义的给定值」而非脏值：行为＝不动作，但要留痕。

        与 dirty 的区别在**下一步**：dirty 要求调用点停下核对（模型给了值却读不出，
        可能是个真实意图），负数只是写错了符号，按 0 执行即可（原始值仍在审计里）。
        """
        assert _sell(-0.2).sell_intent() == (0.0, FRAC_ZERO)

    def test_buy_missing_pct_is_not_executable(self) -> None:
        """买入相反：没给比例 = 不可执行（不能凭空按 100% 额度买）。"""
        assert _buy(None).buy_intent() == (0.0, FRAC_MISSING)

    def test_buy_explicit_zero_is_not_executable(self) -> None:
        assert _buy(0).buy_intent() == (0.0, FRAC_ZERO)

    def test_buy_dirty_is_not_executable(self) -> None:
        assert _buy("0.3股").buy_intent() == (0.0, FRAC_DIRTY)

    def test_buy_value_clamped(self) -> None:
        assert _buy(0.3).buy_intent() == (0.3, FRAC_OK)
        assert _buy(3).buy_intent() == (1.0, FRAC_OK)

    def test_nan_never_becomes_full_exit(self) -> None:
        """终审口径：读不出的值一律 0（不回退清仓）——NaN 放大会整仓卖出。"""
        assert _sell(float("nan")).sell_intent()[0] == 0.0
        assert _sell(float("inf")).sell_intent()[0] == 0.0

    def test_hold_and_watch_have_no_fraction_semantics(self) -> None:
        assert _buy(None).action == "buy"  # 语义由 action 承载，见 P2.3 执行段
        for action in ("hold", "watch"):
            d = Decision(action=action, code="600519.SH")
            assert d.buy_intent()[0] == 0.0 and d.sell_intent()[0] == 0.0


# --------------------------------------------------------------------------
# 5. 提示词字面量（提示词是契约：改了要有人看见）
# --------------------------------------------------------------------------


class TestSchemaLiterals:
    def test_rebalance_schema_is_four_fields(self) -> None:
        assert REBALANCE_SCHEMA_JSON == (
            '{"decisions": [{"action": "hold|sell|buy", "code": "600519.SH", '
            '"pct": 0.2, "reason": "一句话理由"}]}'
        )

    def test_intraday_schema_carries_the_eight_fields(self) -> None:
        """watch/stop_loss/take_profit/move_stop/invalidation/confidence/risk_amount 全在。

        少一个字段，模型就少一个能力：`move_stop` 缺席时 49.3% 的棘轮意图无处表达，
        `invalidation` 缺席时「什么情况下这个决策就错了」这句话模型不会说。
        """
        for token in (
            '"action": "hold|sell|buy|watch"',
            '"name"',
            '"stop_loss"',
            '"take_profit"',
            '"move_stop"',
            '"invalidation"',
            '"confidence"',
            '"risk_amount"',
        ):
            assert token in INTRADAY_SCHEMA_JSON, token

    def test_json_only_hint_is_the_retry_suffix(self) -> None:
        """解析失败重试时追加的纠正语（同一次 prompt 的**后缀**，不是新 prompt）。"""
        assert "只输出 JSON" in JSON_ONLY_HINT
        assert JSON_ONLY_HINT.startswith("\n\n")

    def test_schema_values_are_the_two_documented_ones(self) -> None:
        assert (SCHEMA_REBALANCE, SCHEMA_INTRADAY) == ("rebalance", "intraday")

    def test_unknown_schema_rejected(self) -> None:
        """拼错 schema 名不能静默按最宽的那个跑（会放行 watch）。"""
        with pytest.raises(ValueError):
            parse_decisions(
                '{"decisions": [{"action": "hold", "code": "1.SH"}]}', schema="intra"
            )

    def test_schema_json_parses_as_json(self) -> None:
        """schema 字面量本身必须是合法 JSON——它是给模型抄的模板。"""
        import json

        for lit in (REBALANCE_SCHEMA_JSON, INTRADAY_SCHEMA_JSON):
            obj = json.loads(lit)
            assert isinstance(obj.get("decisions"), list) and obj["decisions"]

    def test_confidence_accepts_percent_string(self) -> None:
        """confidence 是辅助字段（宽松解析），但百分号也得读得出来。"""
        (d,) = parse_decisions(
            _intraday_payload(
                {"action": "hold", "code": "600519.SH", "confidence": "80%"}
            ),
            schema=SCHEMA_INTRADAY,
        ).decisions
        assert d.confidence == pytest.approx(0.8)

    def test_confidence_never_non_finite(self) -> None:
        (d,) = parse_decisions(
            _intraday_payload(
                {"action": "hold", "code": "600519.SH", "confidence": float("nan")}
            ),
            schema=SCHEMA_INTRADAY,
        ).decisions
        assert math.isfinite(d.confidence)
