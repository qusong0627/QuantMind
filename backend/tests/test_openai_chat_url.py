"""openai_chat_url 拼接规则回归测试。

背景：智谱（GLM）base_url 为 https://open.bigmodel.cn/api/paas/v4，
原逻辑「不以 /v1 结尾就补 /v1」会拼出 /v4/v1/chat/completions → 404。

规则：路径已以版本段（/v1、/v4 …）结尾时直接拼 /chat/completions，
否则补 /v1（兼容 https://api.openai.com 这类不带版本的写法）。
"""

from backend.services.engine.alpha_agent.llm_client import openai_chat_url


def test_deepseek_v1_passthrough():
    assert (
        openai_chat_url("https://api.deepseek.com/v1")
        == "https://api.deepseek.com/v1/chat/completions"
    )


def test_qwen_compatible_mode():
    assert (
        openai_chat_url("https://dashscope.aliyuncs.com/compatible-mode/v1")
        == "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
    )


def test_zhipu_v4_passthrough():
    assert (
        openai_chat_url("https://open.bigmodel.cn/api/paas/v4")
        == "https://open.bigmodel.cn/api/paas/v4/chat/completions"
    )


def test_bare_host_appends_v1():
    assert (
        openai_chat_url("https://api.openai.com")
        == "https://api.openai.com/v1/chat/completions"
    )


def test_trailing_slash_and_empty():
    assert openai_chat_url("https://api.deepseek.com/v1/") == (
        "https://api.deepseek.com/v1/chat/completions"
    )
