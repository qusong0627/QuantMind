"""向量检索（embedding）配置通道测试。

背景：本仓库的 rd-agent 副本曾把 ``create_embedding`` 替换成伪随机向量
（sha256 播种 → 高斯噪声），使知识库语义检索静默跑在噪声上。修复后 embedding
变成一条真实、可独立配置的通道，与 chat 供应商解耦（DeepSeek 无 embedding 接口）。

这里锁定三层契约：
  1. LLMConfig.llm_env_overrides 只写出显式配置的 EMBEDDING_*（留空由 .env 兜底）
  2. _build_profile_payload 的 set / clear / no-touch 三态语义
  3. rd-agent 侧 resolve_embedding_channel 的优先级与 openai/ 前缀补齐
"""

from __future__ import annotations

import os

import pytest

from backend.services.engine.alpha_agent.llm_client import LLMConfig
from backend.services.engine.routers.ai_ide.config import LLMConfig as RouterLLMConfig
from backend.services.engine.routers.ai_ide.config import (
    _build_profile_payload,
    _validation_fields,
)

EMBEDDING_KEYS = ("EMBEDDING_MODEL", "EMBEDDING_BASE_URL", "EMBEDDING_API_KEY")


def _chat_only() -> LLMConfig:
    return LLMConfig(
        api_key="sk-chat",
        base_url="https://api.deepseek.com/v1",
        model="deepseek-chat",
        protocol="openai",
    )


def _embedding_env(cfg: LLMConfig) -> dict[str, str]:
    return {k: v for k, v in cfg.llm_env_overrides().items() if k in EMBEDDING_KEYS}


# --------------------------------------------------------------------------
# 1. LLMConfig.llm_env_overrides
# --------------------------------------------------------------------------


def test_no_embedding_config_emits_nothing() -> None:
    """未配置 embedding 时不写任何 EMBEDDING_*，留给容器级 .env 兜底。"""
    assert _embedding_env(_chat_only()) == {}


def test_model_only_falls_back_to_container_env() -> None:
    """只填模型时，base_url / api_key 不写出，由 .env 的 EMBEDDING_BASE_URL/KEY 兜底。"""
    cfg = LLMConfig(
        api_key="sk-chat",
        base_url="https://api.deepseek.com/v1",
        model="deepseek-chat",
        protocol="openai",
        embedding_model="BAAI/bge-m3",
    )
    assert _embedding_env(cfg) == {"EMBEDDING_MODEL": "BAAI/bge-m3"}


def test_independent_provider_emits_all_three() -> None:
    """embedding 指向与 chat 完全不同的供应商时，三个变量齐全。"""
    cfg = LLMConfig(
        api_key="sk-chat",
        base_url="https://api.deepseek.com/v1",
        model="deepseek-chat",
        protocol="openai",
        embedding_model="BAAI/bge-m3",
        embedding_base_url="https://api.siliconflow.cn/v1",
        embedding_api_key="sk-emb",
    )
    assert _embedding_env(cfg) == {
        "EMBEDDING_MODEL": "BAAI/bge-m3",
        "EMBEDDING_BASE_URL": "https://api.siliconflow.cn/v1",
        "EMBEDDING_API_KEY": "sk-emb",
    }


def test_embedding_does_not_leak_into_chat_credentials() -> None:
    """embedding 的 key 绝不能覆盖 chat 的 key/base。"""
    cfg = LLMConfig(
        api_key="sk-chat",
        base_url="https://api.deepseek.com/v1",
        model="deepseek-chat",
        protocol="openai",
        embedding_base_url="https://api.siliconflow.cn/v1",
        embedding_api_key="sk-emb",
    )
    env = cfg.llm_env_overrides()
    assert env["OPENAI_API_KEY"] == "sk-chat"
    assert env["OPENAI_BASE_URL"] == "https://api.deepseek.com/v1"
    assert env["LITELLM_OPENAI_API_KEY"] == "sk-chat"


def test_embedding_env_overrides_is_a_subset() -> None:
    cfg = LLMConfig(
        api_key="sk-chat",
        base_url="https://api.deepseek.com/v1",
        model="deepseek-chat",
        protocol="openai",
        embedding_model="BAAI/bge-m3",
        embedding_api_key="sk-emb",
    )
    assert cfg.embedding_env_overrides() == {
        "EMBEDDING_MODEL": "BAAI/bge-m3",
        "EMBEDDING_API_KEY": "sk-emb",
    }


# --------------------------------------------------------------------------
# 2. _build_profile_payload 的 set / clear / no-touch 语义
# --------------------------------------------------------------------------


def test_router_embedding_set() -> None:
    cfg = RouterLLMConfig(
        embedding_model="BAAI/bge-m3",
        embedding_base_url="https://api.siliconflow.cn/v1",
        embedding_api_key="sk-emb",
    )
    assert _build_profile_payload(cfg, {}) == {
        "embedding_model": "BAAI/bge-m3",
        "embedding_base_url": "https://api.siliconflow.cn/v1",
        "embedding_api_key": "sk-emb",
    }


def test_router_embedding_untouched_when_absent() -> None:
    """未传的字段不出现在 payload（no-touch），不会被 null 覆盖掉已存值。"""
    assert _build_profile_payload(RouterLLMConfig(embedding_model="bge-m3"), {}) == {
        "embedding_model": "bge-m3"
    }


def test_router_embedding_clear_uses_empty_string() -> None:
    """空串是显式清除，必须保留在 payload 里（None 与 '' 语义不同）。"""
    assert _build_profile_payload(RouterLLMConfig(embedding_api_key=""), {}) == {
        "embedding_api_key": ""
    }


def test_router_chat_path_unchanged() -> None:
    """回归：chat 字段组装不受 embedding 改动影响。"""
    cfg = RouterLLMConfig(
        qwen_api_key="sk-chat",
        model="deepseek-chat",
        base_url="https://api.deepseek.com/v1",
        provider="deepseek",
    )
    assert _build_profile_payload(cfg, {}) == {
        "api_key": "sk-chat",
        "llm_model": "deepseek-chat",
        "llm_base_url": "https://api.deepseek.com/v1",
        "llm_provider": "deepseek",
    }


def test_router_embedding_only_payload_is_non_empty() -> None:
    """只保存 embedding 时 payload 必须非空，否则路由会 400。"""
    cfg = RouterLLMConfig(embedding_model="BAAI/bge-m3")
    assert _build_profile_payload(cfg, {}) != {}


# --------------------------------------------------------------------------
# 3. rd-agent 侧 resolve_embedding_channel（需 rdagent 已安装）
# --------------------------------------------------------------------------


rdagent_embedding = pytest.importorskip(
    "rdagent.oai.utils.embedding",
    reason="rdagent 未安装（CI 外环境跳过）",
)


@pytest.fixture
def clean_embedding_env(monkeypatch: pytest.MonkeyPatch):
    """隔离宿主环境变量，只保留用例显式设置的值。"""
    for key in EMBEDDING_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(
        rdagent_embedding.LLM_SETTINGS, "embedding_model", "", raising=False
    )
    monkeypatch.setattr(
        rdagent_embedding.LLM_SETTINGS, "embedding_openai_base_url", "", raising=False
    )
    monkeypatch.setattr(
        rdagent_embedding.LLM_SETTINGS, "embedding_openai_api_key", "", raising=False
    )
    return monkeypatch


def test_resolve_returns_empty_when_unconfigured(clean_embedding_env) -> None:
    """未配置返回空 model —— 调用方据此响亮报错，绝不退回伪向量。"""
    assert rdagent_embedding.resolve_embedding_channel() == ("", "", "")


def test_resolve_reads_standard_env_names(clean_embedding_env) -> None:
    """EMBEDDING_* 是容器 .env 与上游 health_check 共用的名字。"""
    clean_embedding_env.setenv("EMBEDDING_MODEL", "BAAI/bge-m3")
    clean_embedding_env.setenv("EMBEDDING_BASE_URL", "https://api.siliconflow.cn/v1")
    clean_embedding_env.setenv("EMBEDDING_API_KEY", "sk-emb")
    model, api_base, api_key = rdagent_embedding.resolve_embedding_channel()
    assert api_base == "https://api.siliconflow.cn/v1"
    assert api_key == "sk-emb"
    assert model == "openai/BAAI/bge-m3"


def test_resolve_prefixes_openai_for_custom_endpoint(clean_embedding_env) -> None:
    """自定义 base_url 时补 openai/ 前缀。

    litellm 会把 "BAAI/bge-m3" 解析成 provider=BAAI 并抛
    "LLM Provider NOT provided"，必须补成 openai/BAAI/bge-m3。
    """
    clean_embedding_env.setenv("EMBEDDING_MODEL", "BAAI/bge-m3")
    clean_embedding_env.setenv("EMBEDDING_BASE_URL", "https://api.siliconflow.cn/v1")
    model, _, _ = rdagent_embedding.resolve_embedding_channel()
    assert model == "openai/BAAI/bge-m3"


def test_resolve_keeps_existing_provider_prefix(clean_embedding_env) -> None:
    """已带 litellm 认识的 provider 前缀时不重复补。"""
    clean_embedding_env.setenv("EMBEDDING_MODEL", "openai/bge-m3")
    clean_embedding_env.setenv("EMBEDDING_BASE_URL", "https://api.siliconflow.cn/v1")
    model, _, _ = rdagent_embedding.resolve_embedding_channel()
    assert model == "openai/bge-m3"


def test_resolve_env_beats_settings(clean_embedding_env) -> None:
    """EMBEDDING_* 优先于 LITELLM_ 前缀的 settings（容器与 CLI 两条路都得通）。"""
    clean_embedding_env.setattr(
        rdagent_embedding.LLM_SETTINGS, "embedding_model", "text-embedding-3-small"
    )
    clean_embedding_env.setenv("EMBEDDING_MODEL", "bge-m3")
    model, _, _ = rdagent_embedding.resolve_embedding_channel()
    assert model == "bge-m3"


def test_resolve_falls_back_to_settings(clean_embedding_env) -> None:
    """未设环境变量时回落到 settings（LITELLM_ 前缀路径保持可用）。"""
    clean_embedding_env.setattr(
        rdagent_embedding.LLM_SETTINGS, "embedding_model", "text-embedding-3-small"
    )
    model, _, _ = rdagent_embedding.resolve_embedding_channel()
    assert model == "text-embedding-3-small"


def test_resolve_live_siliconflow_endpoint() -> None:
    """真实连通性冒烟：仅在宿主已配置 EMBEDDING_* 时执行。

    这是唯一能捕捉「配置齐全但通道其实不通」的用例——纯 mock 测不出这种失效，
    而这类静默失效正是本次要根除的 bug 模式。
    """
    model = (os.environ.get("EMBEDDING_MODEL") or "").strip()
    base = (os.environ.get("EMBEDDING_BASE_URL") or "").strip()
    key = (os.environ.get("EMBEDDING_API_KEY") or "").strip()
    if not (model and base and key):
        pytest.skip("未配置 EMBEDDING_*，跳过真实连通性冒烟")

    from rdagent.oai.backend.litellm import LiteLLMAPIBackend

    vectors = LiteLLMAPIBackend().create_embedding(["动量因子", "波动率因子"])
    assert len(vectors) == 2
    assert len(vectors[0]) > 0
    # 真向量：同域中文短语余弦相似度显著为正；伪随机向量的相似度≈0
    import numpy as np

    arr = np.array(vectors)
    arr = arr / np.linalg.norm(arr, axis=1, keepdims=True)
    assert float(arr[0] @ arr[1]) > 0.1, "相似度过低，疑似仍返回伪随机向量"


# --------------------------------------------------------------------------
# 4. 用户级配置 → 挖掘子进程 env 的透传
#
# 这一节补的是代码审查发现的 HIGH：`LLMConfig.llm_env_overrides()` 写出
# EMBEDDING_*，而唯一消费者（launcher）只读 OPENAI_*/CHAT_MODEL/LITELLM_OPENAI_*，
# 于是「个人中心配好向量检索」整条链路静默空转——界面显示已保存，挖掘始终用
# 容器级 .env。原测试只断言到 llm_env_overrides() 为止，差的就是这一跳。
# --------------------------------------------------------------------------


def test_embedding_overrides_passes_through_non_empty() -> None:
    """非空的 embedding 三项必须原样进子进程 env。"""
    from backend.services.engine.rd_agent.llm_env import embedding_overrides

    assert embedding_overrides(
        {
            "EMBEDDING_MODEL": "BAAI/bge-m3",
            "EMBEDDING_BASE_URL": "https://api.siliconflow.cn/v1",
            "EMBEDDING_API_KEY": "sk-emb",
            "OPENAI_API_KEY": "sk-chat",
        }
    ) == {
        "EMBEDDING_MODEL": "BAAI/bge-m3",
        "EMBEDDING_BASE_URL": "https://api.siliconflow.cn/v1",
        "EMBEDDING_API_KEY": "sk-emb",
    }


def test_embedding_overrides_skips_blank_and_none() -> None:
    """留空 = 沿用容器级 .env，不要用空串覆盖掉容器已有的配置。"""
    from backend.services.engine.rd_agent.llm_env import embedding_overrides

    assert embedding_overrides({"EMBEDDING_MODEL": "  ", "EMBEDDING_API_KEY": None}) == {}
    assert embedding_overrides(None) == {}
    assert embedding_overrides({}) == {}


def test_llm_env_overrides_output_is_consumable_by_passthrough() -> None:
    """端到端一环：LLMConfig 产出的键必须能被透传函数原样接住。

    这条锁死审查发现的 HIGH——生产者与消费者之间曾经没有任何测试连接。
    """
    from backend.services.engine.rd_agent.llm_env import embedding_overrides

    cfg = LLMConfig(
        api_key="sk-chat",
        base_url="https://api.deepseek.com/v1",
        model="deepseek-chat",
        protocol="openai",
        embedding_model="BAAI/bge-m3",
        embedding_base_url="https://api.siliconflow.cn/v1",
        embedding_api_key="sk-emb",
    )
    assert embedding_overrides(cfg.llm_env_overrides()) == {
        "EMBEDDING_MODEL": "BAAI/bge-m3",
        "EMBEDDING_BASE_URL": "https://api.siliconflow.cn/v1",
        "EMBEDDING_API_KEY": "sk-emb",
    }


# --------------------------------------------------------------------------
# 5. 「未配置必须响亮失败」的守卫
#
# 审查发现的另一处：`LLM_SETTINGS.embedding_model` 默认值是非空的
# "text-embedding-3-small"，所以「model 非空」恒真，守卫从不触发——
# 未配置时会静默回落到 chat 供应商凭证，或 150s 后才报一个误导性的错。
# --------------------------------------------------------------------------

_FULLY_ISOLATED = EMBEDDING_KEYS + ("EMBEDDING_OPENAI_BASE_URL", "EMBEDDING_OPENAI_API_KEY")


@pytest.fixture
def upstream_defaults_env(monkeypatch: pytest.MonkeyPatch):
    """模拟「完全没配」：清空环境变量，并把 LLM_SETTINGS 还原成上游真实默认值。

    不能沿用 `clean_embedding_env`——它把 embedding_model 手工改成 ""，
    那恰好绕开了本节的 bug（默认值非空）。
    """
    for key in _FULLY_ISOLATED:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(
        rdagent_embedding.LLM_SETTINGS, "embedding_model", "text-embedding-3-small", raising=False
    )
    monkeypatch.setattr(
        rdagent_embedding.LLM_SETTINGS, "embedding_openai_base_url", "", raising=False
    )
    monkeypatch.setattr(
        rdagent_embedding.LLM_SETTINGS, "embedding_openai_api_key", "", raising=False
    )
    return monkeypatch


def test_non_empty_default_model_is_not_treated_as_configured(upstream_defaults_env) -> None:
    """默认模型非空 ≠ 有人配置过。"""
    assert rdagent_embedding.resolve_embedding_channel()[0] == "text-embedding-3-small"
    assert rdagent_embedding.embedding_channel_is_explicit() is False


def test_unconfigured_channel_reports_not_ready(upstream_defaults_env) -> None:
    """守卫必须真的触发，否则错误落进 30×5s 重试循环。"""
    from rdagent.oai.backend.litellm import LiteLLMAPIBackend

    ready, reason = LiteLLMAPIBackend()._embedding_channel_ready()
    assert ready is False
    assert "EMBEDDING_MODEL" in reason
    assert "text-embedding-3-small" in reason, "应点明会静默用哪个默认模型"


def test_explicit_model_env_counts_as_configured(upstream_defaults_env) -> None:
    """显式设了 EMBEDDING_MODEL 就算主动选择（哪怕走 OpenAI 官方端点）。"""
    upstream_defaults_env.setenv("EMBEDDING_MODEL", "text-embedding-3-small")
    assert rdagent_embedding.embedding_channel_is_explicit() is True


# --------------------------------------------------------------------------
# 6. 端点规整与错误日志脱敏
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://api.siliconflow.cn", "https://api.siliconflow.cn/v1"),
        ("https://api.siliconflow.cn/", "https://api.siliconflow.cn/v1"),
        ("https://api.siliconflow.cn/v1", "https://api.siliconflow.cn/v1"),
        ("https://api.siliconflow.cn/v1/", "https://api.siliconflow.cn/v1"),
        ("  http://127.0.0.1:11434  ", "http://127.0.0.1:11434/v1"),
        ("", ""),
        (None, ""),
    ],
)
def test_normalize_embedding_base_url(raw: str | None, expected: str) -> None:
    """与 chat 同规则：litellm 按 {base}/embeddings 拼，缺 /v1 直接 404。"""
    from backend.services.engine.alpha_agent.llm_client import normalize_embedding_base_url

    assert normalize_embedding_base_url(raw) == expected


class _FakeResponse:
    """只实现 _validation_fields 用到的 .json()。"""

    def __init__(self, payload) -> None:
        self._payload = payload

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def test_validation_fields_extracts_locs_without_values() -> None:
    """FastAPI 422 的 detail[].input 就是明文密钥，只能取 loc 路径。"""
    resp = _FakeResponse(
        {
            "detail": [
                {"loc": ["body", "embedding_api_key"], "input": "sk-real-key", "msg": "..."},
                {"loc": ["body", "embedding_base_url"], "input": "x", "msg": "..."},
            ]
        }
    )
    fields = _validation_fields(resp)
    assert fields == ["body.embedding_api_key", "body.embedding_base_url"]
    assert "sk-real-key" not in str(fields)


def test_validation_fields_survives_non_422_body() -> None:
    """错误处理本身不能抛异常——非 JSON 回包、detail 非 list 都要安全退化。"""
    assert _validation_fields(_FakeResponse({"detail": "Not Found"})) == []
    assert _validation_fields(_FakeResponse(ValueError("not json"))) == []
    assert _validation_fields(_FakeResponse({"detail": [{}]})) == []


# --------------------------------------------------------------------------
# 7. rd-agent 配置快照脱敏（上游把整个 LITELLM_SETTINGS 打日志）
# --------------------------------------------------------------------------


litellm_backend = pytest.importorskip(
    "rdagent.oai.backend.litellm",
    reason="rdagent 未安装（CI 外环境跳过）",
)


def test_redact_settings_masks_keys_only() -> None:
    """密钥字段脱敏，非密钥字段（含名字里有 token 的）原样保留。"""
    assert litellm_backend._redact_settings(
        {
            "openai_api_key": "sk-real",
            "chat_openai_api_key": "sk-chat",
            "embedding_openai_api_key": "sk-emb",
            "llama2_70b_endpoint_key": "sk-llama",
            "chat_token_limit": 100000,  # 不是密钥，别误伤
            "chat_model": "astron-code-latest",
        }
    ) == {
        "openai_api_key": "***",
        "chat_openai_api_key": "***",
        "embedding_openai_api_key": "***",
        "llama2_70b_endpoint_key": "***",
        "chat_token_limit": 100000,
        "chat_model": "astron-code-latest",
    }


def test_backend_init_never_logs_plaintext_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """端到端：__init__ 实际交给 logger 的内容里不能出现明文密钥。

    上游这里是 `logger.info(f"{LITELLM_SETTINGS}")`，实测确认讯飞 MaaS 的 key
    会明文出现在容器 stdout，`log_object` 还会把它写进落盘 artifact。
    """
    captured: list[str] = []
    monkeypatch.setattr(litellm_backend.logger, "info", lambda msg, **kw: captured.append(str(msg)))
    monkeypatch.setattr(
        litellm_backend.logger, "log_object", lambda obj, **kw: captured.append(str(obj))
    )
    monkeypatch.setattr(
        litellm_backend.LITELLM_SETTINGS, "openai_api_key", "sk-super-secret", raising=False
    )
    monkeypatch.setattr(litellm_backend.LiteLLMAPIBackend, "_has_logged_settings", False)

    litellm_backend.LiteLLMAPIBackend()

    assert captured, "__init__ 应打印一次配置快照"
    blob = "\n".join(captured)
    assert "sk-super-secret" not in blob
    assert "***" in blob
