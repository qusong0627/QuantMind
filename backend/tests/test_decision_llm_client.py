"""决策 LLM IO 适配：请求体、供应商级重试、usage 提取、缺配置分类。

金样 ``fixtures/decision_prompt_golden.json`` 的 ``llm_request`` 段是**实测**出来的
（生成器把隔壁 ``requests.post`` 换掉、真调一次 ``live_hourly_analysis.call_llm``，
把发出去的 payload 与返回值原样存下），不是读隔壁代码抄的常量——所以这里断言的是
「与隔壁实际发出去的东西一致」，包括它**重发了几次**。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from backend.shared.decision.contract import SCHEMA_REBALANCE
from backend.shared.decision.llm_call import LLMNotConfigured, decide_with_retry
from backend.shared.decision_llm_client import (
    AGENT_LIMIT,
    DEFAULT_MAX_TOKENS,
    DEFAULT_TEMPERATURE,
    DEFAULT_TIMEOUT_S,
    PROVIDER_RETRY_ATTEMPTS,
    DecisionLLMConfig,
    _extract,
    call_with_usage,
    make_caller,
    resolve_config,
    resolve_roster,
)

GOLDEN = Path(__file__).parent / "fixtures" / "decision_prompt_golden.json"
OK_JSON = (
    '{"decisions": [{"action": "hold", "code": "600036.SH", "reason": "趋势未破"}]}'
)
_ENV_NAMES = (
    "QM_DECISION_LLM_BASE_URL",
    "QM_DECISION_LLM_API_KEY",
    "QM_DECISION_LLM_MODEL",
    "QM_DECISION_LLM_TIMEOUT",
    "QM_DECISION_LLM_MAX_TOKENS",
    "QM_DECISION_LLM_TEMPERATURE",
    "QM_DECISION_LLM_ROSTER",
)

ENV = {
    "QM_DECISION_LLM_BASE_URL": "https://api.deepseek.com/v1",
    "QM_DECISION_LLM_API_KEY": "sk-not-a-real-key",
    "QM_DECISION_LLM_MODEL": "deepseek-v4-pro",
}


@pytest.fixture(scope="module")
def request_golden() -> dict:
    doc = json.loads(GOLDEN.read_text(encoding="utf-8"))
    return doc["llm_request"]


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """清掉本模块读的全部 env——金样断言不能受宿主机上真配的 key 影响。"""
    for name in _ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def _cfg(**kw: object) -> DecisionLLMConfig:
    base: dict = {
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "sk-not-a-real-key",
        "model": "deepseek-v4-pro",
    }
    base.update(kw)
    return DecisionLLMConfig(**base)  # type: ignore[arg-type]


class _Post:
    """假 ``_post``：按脚本返回 body 或抛异常，记录每次收到的实参。"""

    def __init__(self, *outs: object) -> None:
        self._outs = list(outs)
        self.calls: list[dict] = []

    def __call__(self, url, payload, headers, timeout) -> dict:
        self.calls.append(
            {"url": url, "payload": payload, "headers": headers, "timeout": timeout}
        )
        out = self._outs.pop(0) if self._outs else {}
        if isinstance(out, BaseException):
            raise out
        return out  # type: ignore[return-value]


def _body(content: str = "{}", usage: dict | None = None, **msg: object) -> dict:
    message: dict = {"content": content, **msg}
    out: dict = {"choices": [{"message": message}]}
    if usage is not None:
        out["usage"] = usage
    return out


# ---------------------------------------------------------------------------
# 金样：与隔壁实际发出去的东西一致
# ---------------------------------------------------------------------------


def test_system_prompt_matches_the_sibling_implementation(request_golden: dict) -> None:
    """系统提示词也是一段提示词——逐字节复现，含模型名插值。"""
    assert _cfg().system_prompt() == request_golden["system_prompt"]
    assert "deepseek-v4-pro" in _cfg().system_prompt()


def test_payload_shape_matches_golden(request_golden: dict, monkeypatch) -> None:
    from backend.shared import decision_llm_client as mod

    post = _Post(_body(OK_JSON))
    monkeypatch.setattr(mod, "_post", post)
    call_with_usage("用户提示词", config=_cfg())
    (sent,) = post.calls
    golden_payload = request_golden["payload"]
    assert sent["payload"]["model"] == golden_payload["model"]
    assert sent["payload"]["temperature"] == golden_payload["temperature"]
    assert sent["payload"]["max_tokens"] == golden_payload["max_tokens"]
    assert [m["role"] for m in sent["payload"]["messages"]] == golden_payload["roles"]
    assert sent["payload"]["messages"][1]["content"] == "用户提示词"
    assert sent["timeout"] == request_golden["timeout_s"]
    # 端点形状：base（含 /v1）+ /chat/completions，与隔壁实测的 URL 同形
    assert sent["url"] == request_golden["url_shape"]


def test_provider_retry_attempts_matches_measured_golden(request_golden: dict) -> None:
    """重试次数是**实测**的（生成器让第一次 post 抛异常、数它到底发了几次）。"""
    assert PROVIDER_RETRY_ATTEMPTS == request_golden["post_calls_with_one_failure"] == 2


def test_absent_usage_keys_diverge_from_golden(request_golden: dict) -> None:
    """隔壁把 provider 没报的键记成 0；本仓让它们**不出现**（缺失 ≠ 0）。

    金样里 `reasoning_content_usage` 是隔壁的原样输出（completion/total 都是 0，
    而那次响应只报了 prompt_tokens）——这正是要钉住的那条分叉。
    """
    golden_usage = request_golden["reasoning_content_usage"]
    assert golden_usage == {
        "prompt_tokens": 5,
        "completion_tokens": 0,
        "total_tokens": 0,
    }
    content, usage = _extract(
        {"choices": [{"message": {"content": ""}}], "usage": {"prompt_tokens": 5}}
    )
    assert usage == {"prompt_tokens": 5}
    assert content == ""


def test_reasoning_content_fallback_matches_golden(request_golden: dict) -> None:
    """推理模型的 content 可能是空的，话在 ``reasoning_content`` 里。"""
    content, _ = _extract(
        {"choices": [{"message": {"content": "", "reasoning_content": "兜底文本"}}]}
    )
    assert content == request_golden["reasoning_content_fallback"] == "兜底文本"


# ---------------------------------------------------------------------------
# 配置解析
# ---------------------------------------------------------------------------


def test_resolve_config_reads_the_trio(clean_env, monkeypatch) -> None:
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    cfg = resolve_config()
    assert (cfg.base_url, cfg.api_key, cfg.model) == (
        ENV["QM_DECISION_LLM_BASE_URL"],
        ENV["QM_DECISION_LLM_API_KEY"],
        ENV["QM_DECISION_LLM_MODEL"],
    )
    assert (cfg.temperature, cfg.max_tokens, cfg.timeout) == (
        DEFAULT_TEMPERATURE,
        DEFAULT_MAX_TOKENS,
        DEFAULT_TIMEOUT_S,
    )


def test_resolve_config_accepts_tuning_overrides(clean_env, monkeypatch) -> None:
    for k, v in {
        **ENV,
        "QM_DECISION_LLM_TIMEOUT": "30",
        "QM_DECISION_LLM_MAX_TOKENS": "800",
    }.items():
        monkeypatch.setenv(k, v)
    cfg = resolve_config()
    assert (cfg.timeout, cfg.max_tokens) == (30.0, 800)


@pytest.mark.parametrize("missing", sorted(ENV))
def test_resolve_config_names_the_missing_variable(
    clean_env, monkeypatch, missing: str
) -> None:
    """缺哪个点名哪个——排查成本全在这句话上。"""
    for k, v in ENV.items():
        if k != missing:
            monkeypatch.setenv(k, v)
    with pytest.raises(LLMNotConfigured) as exc:
        resolve_config()
    assert missing in str(exc.value)
    assert ENV["QM_DECISION_LLM_API_KEY"] not in str(exc.value)  # 不打印 key 本身


@pytest.mark.parametrize(
    "placeholder",
    ["", "   ", "your-deepseek-api-key", "sk-在此填写", "mock-api-key", "CHANGEME"],
)
def test_placeholders_count_as_not_configured(
    clean_env, monkeypatch, placeholder: str
) -> None:
    """占位符 = 没配：拿假 key 打真端点会换来 401，把「没配」报成「模型坏了」。"""
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("QM_DECISION_LLM_API_KEY", placeholder)
    with pytest.raises(LLMNotConfigured):
        resolve_config()


def test_config_without_env_is_not_configured(clean_env) -> None:
    with pytest.raises(LLMNotConfigured):
        resolve_config()


# ---------------------------------------------------------------------------
# 名册（P2.9）：一个账户几家模型
# ---------------------------------------------------------------------------
def _roster(monkeypatch, *entries: dict, env: dict | None = None) -> None:
    """名册进 env（与生产同一条路：``QM_DECISION_LLM_ROSTER`` 是一段 JSON）。"""
    for k, v in (env or ENV).items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv(
        "QM_DECISION_LLM_ROSTER", json.dumps(list(entries), ensure_ascii=False)
    )


def test_roster_absent_is_exactly_the_single_global_config(
    clean_env, monkeypatch
) -> None:
    """没配名册 = 一家（今天的行为）：单家路径与名册路径**走同一份配置**。"""
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    roster = resolve_roster()
    assert roster == (resolve_config(),)


def test_roster_absent_and_unconfigured_says_what_single_config_says(
    clean_env, monkeypatch
) -> None:
    """三件套没配时的报错**逐字**与单家一致——否则同一故障有两套排查话术。"""
    with pytest.raises(LLMNotConfigured) as single:
        resolve_config()
    with pytest.raises(LLMNotConfigured) as roster:
        resolve_roster()
    assert str(roster.value) == str(single.value)


def test_roster_shares_the_global_endpoint_and_varies_the_model(
    clean_env, monkeypatch
) -> None:
    """隔壁的形态：一个端点 + 一个 key，几家模型各跑各的（deepseek flash/pro）。"""
    _roster(
        monkeypatch,
        {"model": "deepseek-v4-flash"},
        {"model": "deepseek-v4-pro"},
        {"model": "deepseek-v4-mini"},
    )
    roster = resolve_roster()
    assert [c.model for c in roster] == [
        "deepseek-v4-flash",
        "deepseek-v4-pro",
        "deepseek-v4-mini",
    ]
    assert {c.base_url for c in roster} == {ENV["QM_DECISION_LLM_BASE_URL"]}
    assert {c.api_key for c in roster} == {ENV["QM_DECISION_LLM_API_KEY"]}


def test_roster_entry_points_at_its_own_env_vars(clean_env, monkeypatch) -> None:
    """换供应商的形态（隔壁的 GLM 那家）：端点与 key 各自指一个变量名。

    名册串里**没有 key 材料**——它会被打进日志、docker inspect 与工单，
    所以 key 只能以变量名的形式出现（``api_key_env``）。
    """
    _roster(
        monkeypatch,
        {"model": "deepseek-v4-pro"},
        {
            "model": "glm-5.3-flash",
            "base_url_env": "GLM_API_BASE",
            "api_key_env": "GLM_API_KEY",
        },
    )
    monkeypatch.setenv("GLM_API_BASE", "https://open.bigmodel.cn/api/paas/v4")
    monkeypatch.setenv("GLM_API_KEY", "glm-not-a-real-key")
    roster = resolve_roster()
    flash = roster[1]
    assert flash.base_url == "https://open.bigmodel.cn/api/paas/v4"
    assert flash.api_key == "glm-not-a-real-key"
    # 名字指对了 ≠ 值进过名册串
    assert "glm-not-a-real-key" not in os.environ["QM_DECISION_LLM_ROSTER"]


def test_roster_entry_overrides_call_parameters(clean_env, monkeypatch) -> None:
    """逐家调参：便宜的那家可以更小的 max_tokens / 更低的温度。"""
    _roster(
        monkeypatch,
        {"model": "deepseek-v4-pro"},
        {
            "model": "deepseek-v4-mini",
            "timeout": 30,
            "max_tokens": 800,
            "temperature": 0,
        },
    )
    pro, mini = resolve_roster()
    assert (pro.timeout, pro.max_tokens, pro.temperature) == (
        DEFAULT_TIMEOUT_S,
        DEFAULT_MAX_TOKENS,
        DEFAULT_TEMPERATURE,
    )
    assert (mini.timeout, mini.max_tokens, mini.temperature) == (30.0, 800, 0.0)


@pytest.mark.parametrize(
    "models",
    [
        ("deepseek-v4-pro", "deepseek-v4-pro"),  # 一模一样
        ("  deepseek-v4-pro  ", "deepseek-v4-pro"),  # 去空白后撞上
        # 归一化截断到 64 字符之后才撞上：**必须先归一后比较**
        ("m" * 64 + "-alpha", "m" * 64 + "-beta"),
    ],
)
def test_roster_refuses_duplicate_agents_after_normalization(
    clean_env, monkeypatch, models: tuple[str, str]
) -> None:
    """重名 = 两家并成一本账（agent 是分账账本/幂等键/审计行的同一个键）。

    这是**拒绝**而不是「去重保留先到」：静默去掉一家，运营看到的是「写了三家、
    跑了一家」，而账单和账本上分不出是哪一家。
    """
    _roster(monkeypatch, *({"model": m} for m in models))
    with pytest.raises(LLMNotConfigured) as exc:
        resolve_roster()
    assert "重名" in str(exc.value) or "重复" in str(exc.value), exc.value


def test_roster_absent_single_agent_is_not_a_duplicate(clean_env, monkeypatch) -> None:
    """单家路径不该被自己那条重名检查误伤（走的是另一条分支）。"""
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    assert len(resolve_roster()) == 1


def test_roster_refuses_an_empty_list(clean_env, monkeypatch) -> None:
    """空名册 = 一轮都不跑。要停就关 ``QM_DECISION_ROUND_ENABLED``——
    空名册停摆是一条**没有痕迹**的停机（心跳照写、状态键照写空结果）。"""
    _roster(monkeypatch)
    with pytest.raises(LLMNotConfigured) as exc:
        resolve_roster()
    assert "空" in str(exc.value)
    assert "QM_DECISION_ROUND_ENABLED" in str(exc.value)


@pytest.mark.parametrize(
    "raw",
    [
        '{"model": "x"}',  # 不是数组
        '[{"model": "x"}, "deepseek-v4-pro"]',  # 项不是对象
        "[{}]",  # 缺 model
        '[{"model": "   "}]',  # 空 model
        '[{"model": "your-model-here"}]',  # 占位符
        '[{"model": "x", "api_key": "your-key"}]',  # 写死的 key 是占位符
        "not json at all",
    ],
)
def test_roster_refuses_malformed_entries_by_index(
    clean_env, monkeypatch, raw: str
) -> None:
    """坏名册**点名第几项**：三家里有一家写错时，重试成本全在这句话上。"""
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("QM_DECISION_LLM_ROSTER", raw)
    with pytest.raises(LLMNotConfigured) as exc:
        resolve_roster()
    assert "QM_DECISION_LLM_ROSTER" in str(exc.value)
    assert ENV["QM_DECISION_LLM_API_KEY"] not in str(exc.value)


def test_roster_names_the_entry_that_misses_its_own_env_var(
    clean_env, monkeypatch
) -> None:
    """指了 ``GLM_API_KEY`` 但那个变量没配：报错必须说得出是哪一家、哪个变量。"""
    _roster(
        monkeypatch,
        {"model": "deepseek-v4-pro"},
        {"model": "glm-5.3-flash", "api_key_env": "GLM_API_KEY"},
    )
    monkeypatch.delenv("GLM_API_KEY", raising=False)
    with pytest.raises(LLMNotConfigured) as exc:
        resolve_roster()
    msg = str(exc.value)
    assert "GLM_API_KEY" in msg and "glm-5.3-flash" in msg


def test_roster_is_capped(clean_env, monkeypatch) -> None:
    """上限不是洁癖：一轮里 N 家串行跑，每家最长两分钟级——名册无上限时
    写错一行就变成「宽限窗里跑不完」，而跑不完的表现是**后面的家当天不决策**。"""
    _roster(monkeypatch, *({"model": f"m{i}"} for i in range(AGENT_LIMIT + 1)))
    with pytest.raises(LLMNotConfigured) as exc:
        resolve_roster()
    assert str(AGENT_LIMIT) in str(exc.value)


def test_roster_never_echoes_key_material(clean_env, monkeypatch) -> None:
    """坏名册的报错里不许出现 key：异常会进日志、状态键与审计表。"""
    secret = "sk-" + "F" * 32
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("QM_DECISION_LLM_API_KEY", secret)
    monkeypatch.setenv(
        "QM_DECISION_LLM_ROSTER", f'[{{"model": "a", "api_key": "{secret}"}}, 7]'
    )
    with pytest.raises(LLMNotConfigured) as exc:
        resolve_roster()
    assert secret not in str(exc.value)


def test_roster_entry_may_write_a_literal_key_for_local_debugging(
    clean_env, monkeypatch
) -> None:
    """写死 key 是**允许**的（本机调试），但得知道它会进名册串——故不推荐。"""
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv(
        "QM_DECISION_LLM_ROSTER",
        '[{"model": "local", "base_url": "http://127.0.0.1:8001/v1", '
        '"api_key": "local-dev-key"}]',
    )
    (cfg,) = resolve_roster()
    assert cfg.base_url == "http://127.0.0.1:8001/v1"
    assert cfg.api_key == "local-dev-key"


def test_chat_url_appends_verbatim() -> None:
    """base 里**含** ``/v1``（与隔壁实测的 URL 同形）：我们只补 ``/chat/completions``。

    写成 `https://api.deepseek.com` 会得到 404——那是配置错，不是代码错；把 /v1
    也自动补上就会掩盖「用户配了别家网关的自定义路径」这类情况。
    """
    assert _cfg().chat_url == "https://api.deepseek.com/v1/chat/completions"
    assert _cfg(base_url="https://gw.example.com/v1/").chat_url == (
        "https://gw.example.com/v1/chat/completions"
    )


# ---------------------------------------------------------------------------
# 调用：供应商级重试
# ---------------------------------------------------------------------------


def test_success_returns_content_and_usage(monkeypatch) -> None:
    from backend.shared import decision_llm_client as mod

    post = _Post(
        _body(
            OK_JSON, {"prompt_tokens": 11, "completion_tokens": 22, "total_tokens": 33}
        )
    )
    monkeypatch.setattr(mod, "_post", post)
    content, usage = call_with_usage("提示词", config=_cfg())
    assert content == OK_JSON
    assert usage == {"prompt_tokens": 11, "completion_tokens": 22, "total_tokens": 33}
    assert len(post.calls) == 1
    assert post.calls[0]["headers"]["Authorization"] == "Bearer sk-not-a-real-key"


def test_provider_retry_resends_after_transport_failure(monkeypatch) -> None:
    """供应商抖动（智谱曾 90s 超时）→ 同一提示词再发一次。"""
    from backend.shared import decision_llm_client as mod

    post = _Post(TimeoutError("90s 超时"), _body(OK_JSON))
    monkeypatch.setattr(mod, "_post", post)
    content, _ = call_with_usage("提示词", config=_cfg())
    assert content == OK_JSON and len(post.calls) == 2
    assert [c["payload"] for c in post.calls] == [post.calls[0]["payload"]] * 2


def test_all_attempts_failing_raises_the_last_error(monkeypatch) -> None:
    """两次都炸 → 抛（由 ``decision.llm_call`` 收成 ``api_failed``，不在本层吞）。"""
    from backend.shared import decision_llm_client as mod

    post = _Post(TimeoutError("第一次"), ConnectionError("第二次"))
    monkeypatch.setattr(mod, "_post", post)
    with pytest.raises(ConnectionError):
        call_with_usage("提示词", config=_cfg())
    assert len(post.calls) == PROVIDER_RETRY_ATTEMPTS


@pytest.mark.parametrize(
    "body",
    [
        {"choices": []},
        {"choices": "不是列表"},
        {"choices": [{"message": "不是对象"}]},
        {"no_choices": True},
    ],
)
def test_malformed_bodies_raise_with_evidence(body: dict) -> None:
    """网关 200 + 坏形状是常态：报错要带截断的响应体，否则只能靠猜。"""
    with pytest.raises(RuntimeError) as exc:
        _extract(body)
    assert "choices" in str(exc.value)


def test_unconfigured_env_raises_not_configured_not_empty_output(clean_env) -> None:
    with pytest.raises(LLMNotConfigured):
        call_with_usage("提示词")


# ---------------------------------------------------------------------------
# caller 与纯状态机的接线
# ---------------------------------------------------------------------------


def test_make_caller_binds_config_and_model(monkeypatch) -> None:
    from backend.shared import decision_llm_client as mod

    post = _Post(_body(OK_JSON))
    monkeypatch.setattr(mod, "_post", post)
    make_caller(config=_cfg(), model="glm-5.3-flash")("提示词")
    (sent,) = post.calls
    assert sent["payload"]["model"] == "glm-5.3-flash"
    # 换模型要连带换系统提示词里的自述（同一端点跑多模型时的可辨识性）
    assert "glm-5.3-flash" in sent["payload"]["messages"][0]["content"]


def test_end_to_end_decide_with_retry_over_fake_http(monkeypatch) -> None:
    """两个模块拼起来：散文 → 追加纠正语重问 → 取到决策 + usage 合并。"""
    from backend.shared import decision_llm_client as mod

    post = _Post(
        _body("我先看看大盘……", {"total_tokens": 900}),
        _body(OK_JSON, {"total_tokens": 300}),
    )
    monkeypatch.setattr(mod, "_post", post)
    attempt = decide_with_retry(
        make_caller(config=_cfg()), "提示词", schema=SCHEMA_REBALANCE
    )
    assert attempt.ok and attempt.calls == 2
    assert attempt.usage == {"total_tokens": 1200}
    assert attempt.decisions[0].code == "600036.SH"
    assert post.calls[1]["payload"]["messages"][1]["content"].endswith(
        "格式同上面的 schema。"
    )


def test_end_to_end_unconfigured_classified_as_not_configured(
    clean_env, monkeypatch
) -> None:
    """没配 key → ``not_configured``（而不是「模型返回空响应」）。"""
    from backend.shared import decision_llm_client as mod

    post = _Post(_body(OK_JSON))
    monkeypatch.setattr(mod, "_post", post)
    attempt = decide_with_retry(make_caller(), "提示词", schema=SCHEMA_REBALANCE)
    assert not attempt.ok and attempt.status == "not_configured"
    assert post.calls == []  # 压根没发出去——没配就不该打
