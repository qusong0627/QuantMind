"""决策 LLM 调用链（纯状态机）：重试分支、失败分类、usage 合并。

caller 是注入的假函数 → **全部分支都能在无网络下断言**，包括真线上很难等到的
「第一次吐散文、第二次吐 JSON」「第二次调用也炸」这些路径。真 HTTP 在
``test_decision_llm_client.py``。
"""

from __future__ import annotations

import pytest

from backend.shared.decision.contract import (
    JSON_ONLY_HINT,
    SCHEMA_INTRADAY,
    SCHEMA_REBALANCE,
    STATUS_API_FAILED,
    STATUS_EMPTY_OUTPUT,
    STATUS_NOT_CONFIGURED,
    STATUS_OK,
    STATUS_PARSE_FAILED,
    DecisionBatch,
)
from backend.shared.decision.llm_call import (
    PARSE_RETRY_ATTEMPTS,
    LLMNotConfigured,
    decide_with_retry,
    merge_usage,
    normalize_usage,
)

OK_JSON = (
    '{"decisions": [{"action": "hold", "code": "600036.SH", "reason": "趋势未破"}]}'
)
#: 有原文、取不出决策（既没有 decisions 数组，也没有任何认识的 action）
JUNK = "今天大盘偏弱，我建议继续观察，暂时不做调整。"
#: 有 JSON 块，但 action 不认识（提示词/schema 串了的典型形态）
IGNORED_ACTION = '{"decisions": [{"action": "moonshot", "code": "600036.SH"}]}'


class _Caller:
    """按脚本依次返回的假 caller；记录每次收到的提示词。"""

    def __init__(self, *outs: object) -> None:
        self._outs = list(outs)
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> tuple[str, dict | None]:
        self.prompts.append(prompt)
        out = self._outs.pop(0) if self._outs else ""
        if isinstance(out, BaseException):
            raise out
        if isinstance(out, tuple):
            return out[0], out[1]  # type: ignore[return-value]
        return out, None  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# usage：缺席的键不许变成 0
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            {"prompt_tokens": 11, "completion_tokens": 22, "total_tokens": 33},
            {"prompt_tokens": 11, "completion_tokens": 22, "total_tokens": 33},
        ),
        # 只报了一键 → 另两键**不出现**（不是 0）
        ({"prompt_tokens": 5}, {"prompt_tokens": 5}),
        # provider 明说 0 → 保留（「报了 0」与「没报」是两件事）
        ({"completion_tokens": 0}, {"completion_tokens": 0}),
        # 网关把数字字符串化是常态，string→int 拿到的是真值
        ({"prompt_tokens": "12"}, {"prompt_tokens": 12}),
        ({"prompt_tokens": "12.0"}, {"prompt_tokens": 12}),
        ({"prompt_tokens": 12.0}, {"prompt_tokens": 12}),
        # 假值与未知一律不收
        ({"prompt_tokens": True}, None),
        ({"prompt_tokens": None}, None),
        ({"prompt_tokens": "12abc"}, None),
        ({"prompt_tokens": float("nan")}, None),
        ({"prompt_tokens": float("inf")}, None),
        ({"prompt_tokens": [12]}, None),
        ({}, None),
        (None, None),
    ],
)
def test_normalize_usage(raw: object, expected: dict | None) -> None:
    assert normalize_usage(raw) == expected  # type: ignore[arg-type]


def test_normalize_usage_ignores_unknown_keys() -> None:
    """协议外的键不进结果：键集合固定才能跨轮求和。"""
    assert normalize_usage({"prompt_tokens": 1, "cached_tokens": 9}) == {
        "prompt_tokens": 1
    }


def test_bool_is_not_a_token_count() -> None:
    """``True`` 是 ``int`` 的子类——不挡掉就会把 True 记成 1 个 token。"""
    assert normalize_usage({"total_tokens": True}) is None


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        (None, None, None),
        ({"prompt_tokens": 1}, None, {"prompt_tokens": 1}),
        (None, {"prompt_tokens": 2}, {"prompt_tokens": 2}),
        (
            {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
            {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
            {"prompt_tokens": 11, "completion_tokens": 22, "total_tokens": 33},
        ),
        # 单侧缺席的键保留单侧值（那次调用了、这次没报该明细）
        (
            {"prompt_tokens": 1},
            {"completion_tokens": 2},
            {"prompt_tokens": 1, "completion_tokens": 2},
        ),
        # 两侧都脏 → 结果里连这个键都没有
        ({"prompt_tokens": "x"}, {"prompt_tokens": None}, None),
    ],
)
def test_merge_usage(a: object, b: object, expected: dict | None) -> None:
    assert merge_usage(a, b) == expected  # type: ignore[arg-type]


def test_merge_usage_counts_retry_cost() -> None:
    """解析重试也是真实开销：两次调用的 token 要相加，不是取最后一次。"""
    merged = merge_usage({"total_tokens": 1000}, {"total_tokens": 400})
    assert merged == {"total_tokens": 1400}


# ---------------------------------------------------------------------------
# 成功路径
# ---------------------------------------------------------------------------


def test_ok_first_try_calls_once() -> None:
    call = _Caller((OK_JSON, {"prompt_tokens": 7}))
    attempt = decide_with_retry(call, "提示词", schema=SCHEMA_REBALANCE)
    assert attempt.ok and attempt.status == STATUS_OK
    assert attempt.calls == 1 and not attempt.retried
    assert attempt.usage == {"prompt_tokens": 7}
    assert len(call.prompts) == 1
    assert attempt.decisions[0].code == "600036.SH"


def test_empty_output_keeps_usage_and_does_not_retry() -> None:
    """空响应多为接口/限流问题——没有对象可纠正，重发只是把账单翻倍。"""
    call = _Caller(("", {"total_tokens": 5}))
    attempt = decide_with_retry(call, "提示词", schema=SCHEMA_REBALANCE)
    assert attempt.status == STATUS_EMPTY_OUTPUT
    assert attempt.calls == 1
    assert attempt.usage == {"total_tokens": 5}  # 调用是成功的，token 真花了
    assert attempt.raw == ""


def test_whitespace_only_output_is_empty_not_parse_failed() -> None:
    call = _Caller("   \n  ")
    attempt = decide_with_retry(call, "提示词", schema=SCHEMA_REBALANCE)
    assert attempt.status == STATUS_EMPTY_OUTPUT and attempt.calls == 1


# ---------------------------------------------------------------------------
# 失败分类：api_failed / not_configured / parse_failed
# ---------------------------------------------------------------------------


def test_api_failure_is_not_retried_here() -> None:
    """调用链异常归 ``api_failed`` 且**不在本层重发**：供应商级重试是 IO 适配层的事。"""
    call = _Caller(TimeoutError("桥超时"))
    attempt = decide_with_retry(call, "提示词", schema=SCHEMA_REBALANCE)
    assert attempt.status == STATUS_API_FAILED
    assert attempt.calls == 1 and len(call.prompts) == 1
    assert "TimeoutError" in attempt.error_text() and "桥超时" in attempt.error_text()
    assert attempt.usage is None


def test_not_configured_is_its_own_status_not_empty_output() -> None:
    """缺 key 是「去配配置」，空响应是「去查接口」——合成一态会把排查引偏。"""
    call = _Caller(LLMNotConfigured("决策 LLM 未配置：QM_DECISION_LLM_API_KEY 为空"))
    attempt = decide_with_retry(call, "提示词", schema=SCHEMA_REBALANCE)
    assert attempt.status == STATUS_NOT_CONFIGURED
    assert attempt.status != STATUS_EMPTY_OUTPUT
    assert attempt.calls == 1
    assert "QM_DECISION_LLM_API_KEY" in attempt.error_text()  # 点名缺哪个，不打印值


def test_non_str_content_is_a_caller_contract_violation() -> None:
    """caller 返回非 str：报成解析失败会把「契约坏了」记成「模型抽风」。"""
    call = _Caller(({"decisions": []}, None))
    attempt = decide_with_retry(call, "提示词", schema=SCHEMA_REBALANCE)
    assert attempt.status == STATUS_API_FAILED
    assert "不是 str" in attempt.error_text()


def test_keyboard_interrupt_is_not_swallowed() -> None:
    """只收 ``Exception``：Ctrl-C / SystemExit 照旧上抛（别把中断变成 api_failed）。"""
    call = _Caller(KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        decide_with_retry(call, "提示词", schema=SCHEMA_REBALANCE)


# ---------------------------------------------------------------------------
# 解析重试
# ---------------------------------------------------------------------------


def test_parse_retry_succeeds_and_merges_usage() -> None:
    call = _Caller((JUNK, {"total_tokens": 900}), (OK_JSON, {"total_tokens": 300}))
    attempt = decide_with_retry(call, "提示词", schema=SCHEMA_REBALANCE)
    assert attempt.ok and attempt.calls == 2 and attempt.retried
    assert attempt.usage == {"total_tokens": 1200}
    # 第二次是**同一段对话继续**：追加纠正语，不是新提示词
    assert call.prompts[1] == "提示词" + JSON_ONLY_HINT
    assert call.prompts[0] == "提示词"


def test_parse_retry_failure_reports_last_raw_and_diagnostics() -> None:
    """两次都取不出 → ``parse_failed``；原文取第二次的，诊断取**最后一次**解析的。"""
    call = _Caller(IGNORED_ACTION, (JUNK, None))
    attempt = decide_with_retry(call, "提示词", schema=SCHEMA_REBALANCE)
    assert attempt.status == STATUS_PARSE_FAILED
    assert attempt.calls == 2
    assert attempt.raw == JUNK  # 第二次的原文更接近模型最终想说的
    assert attempt.batch.ignored_actions == ()  # 不是第一次那个 moonshot
    assert attempt.errors == ()


def test_first_attempt_diagnostics_survive_when_retry_call_fails() -> None:
    """重试调用炸了 → 原文退回第一次，诊断也退回第一次（那次才是唯一被解析过的）。"""
    call = _Caller(IGNORED_ACTION, TimeoutError("重试也超时"))
    attempt = decide_with_retry(call, "提示词", schema=SCHEMA_REBALANCE)
    assert attempt.status == STATUS_PARSE_FAILED
    assert attempt.calls == 2
    assert attempt.raw == IGNORED_ACTION
    assert attempt.batch.ignored_actions == ("moonshot",)
    assert "TimeoutError" in attempt.error_text()


def test_second_attempt_empty_output_stays_parse_failed() -> None:
    """第二轮空响应不改判成 ``empty_output``：这一轮**确实**产生过输出，坏在解析。"""
    call = _Caller(JUNK, "")
    attempt = decide_with_retry(call, "提示词", schema=SCHEMA_REBALANCE)
    assert attempt.status == STATUS_PARSE_FAILED
    assert attempt.raw == JUNK
    assert attempt.usage is None


def test_retry_happens_at_most_parse_retry_attempts_times() -> None:
    """无限重试把账单吃掉：只重问 ``PARSE_RETRY_ATTEMPTS`` 次，第三次输出根本不发。"""
    call = _Caller(JUNK, JUNK, OK_JSON)
    attempt = decide_with_retry(call, "提示词", schema=SCHEMA_REBALANCE)
    assert attempt.status == STATUS_PARSE_FAILED
    assert attempt.calls == 1 + PARSE_RETRY_ATTEMPTS
    assert len(call.prompts) == 1 + PARSE_RETRY_ATTEMPTS


# ---------------------------------------------------------------------------
# 契约边界
# ---------------------------------------------------------------------------


def test_schema_must_be_explicit() -> None:
    """schema 决定 action 白名单：默认值会让「盘中轮误用调仓 schema」静默丢 watch。"""
    with pytest.raises(TypeError):
        decide_with_retry(_Caller(OK_JSON), "提示词")  # type: ignore[call-arg]


def test_schema_whitelist_is_enforced() -> None:
    """调仓 schema 下 ``watch`` 不合法 → 只有那一条被忽略；盘中 schema 照收。"""
    watch = (
        '{"decisions": [{"action": "watch", "code": "600036.SH", "stop_loss": 30.0}]}'
    )
    rebalance = decide_with_retry(
        _Caller(watch, watch), "提示词", schema=SCHEMA_REBALANCE
    )
    assert rebalance.status == STATUS_PARSE_FAILED
    assert rebalance.batch.ignored_actions == ("watch",)
    intraday = decide_with_retry(_Caller(watch), "提示词", schema=SCHEMA_INTRADAY)
    assert intraday.ok and intraday.decisions[0].is_watch


def test_attempt_exposes_batch_fields() -> None:
    attempt = decide_with_retry(_Caller(OK_JSON), "提示词", schema=SCHEMA_REBALANCE)
    assert attempt.batch.codes() == ("600036.SH",)
    assert attempt.batch.failed is False


def test_failed_batch_flags_the_failure() -> None:
    """``DecisionBatch.failed`` 是 ``ok`` 的反面，四种失败态都算。"""
    assert DecisionBatch.api_failed(SCHEMA_REBALANCE).failed
    assert DecisionBatch.not_configured(SCHEMA_REBALANCE).failed
    assert not DecisionBatch(status=STATUS_OK, schema=SCHEMA_REBALANCE).failed
