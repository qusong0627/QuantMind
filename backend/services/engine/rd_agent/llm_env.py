"""因子挖掘 LLM 环境配置 — 统一打通 RD-Agent litellm 后端。

RD-Agent 因子挖掘用 litellm 作为 LLM 后端（rdagent/oai/backend/litellm.py）：
  - LiteLLMSettings env_prefix="LITELLM_" → 读 LITELLM_CHAT_MODEL / LITELLM_OPENAI_API_KEY / LITELLM_OPENAI_API_BASE
  - litellm.completion() 内部还从标准 OPENAI_API_KEY / OPENAI_API_BASE 读 HTTP 认证

但 launcher / rd_loop_wrapper / market_adapters 只设置无前缀变量（OPENAI_API_KEY 等），
且 DEEPSEEK_API_KEY 未接入，导致因子挖掘子进程无法调用任何 LLM（factor 表 0 条）。

本模块统一构建两套变量：
  LITELLM_*（RD-Agent settings 对象用）+ OPENAI_*（litellm HTTP 层用）
优先 DeepSeek（DEEPSEEK_API_KEY），回退现有 AI_IDE/讯飞 MaaS 配置。

向量检索（embedding）是**独立通道**，不在这里构造：
  - 变量名 EMBEDDING_MODEL / EMBEDDING_BASE_URL / EMBEDDING_API_KEY（容器级 .env 已有
    SiliconFlow 默认值），由 rdagent/oai/utils/embedding.py:resolve_embedding_channel 直读。
  - 用户级覆盖来自个人中心「AI 服务配置」的 embedding_* 字段，经
    alpha_agent.llm_client.LLMConfig.llm_env_overrides 写成同名变量；未配置的项
    留给容器级 .env 兜底（子进程继承 os.environ，见 launcher 的 env 构造）。
  chat 与 embedding 必须能分开选：DeepSeek 不提供 embedding 接口，而 bge-m3 这类
  中文向量模型通常来自 SiliconFlow 或本地服务。
"""

from __future__ import annotations

import os
from typing import Any

# docker-compose 占位符，视为未配置
_PLACEHOLDER_KEYS = {"your-deepseek-api-key", "mock-api-key", "mock-api-key-not-configured"}


def _is_placeholder(key: str) -> bool:
    k = (key or "").strip().lower()
    return (not k) or any(p in k for p in _PLACEHOLDER_KEYS) or k.startswith("sk-在此")


def build_llm_env(env: dict[str, Any]) -> dict[str, Any]:
    """在现有 env 基础上补齐 litellm 需要的 LITELLM_ + OPENAI_ 变量。

    API key 优先级：DEEPSEEK_API_KEY（非占位） > LITELLM_OPENAI_API_KEY > AI_IDE_LLM_API_KEY > OPENAI_API_KEY
    模型优先级：   DEEPSEEK（若 deepseek key 生效） > CHAT_MODEL
    """
    deepseek_key = (
        os.getenv("DEEPSEEK_API_KEY", "").strip()
        or env.get("DEEPSEEK_API_KEY", "").strip()
    )
    if _is_placeholder(deepseek_key):
        deepseek_key = ""

    deepseek_base = (
        os.getenv("DEEPSEEK_BASE_URL", "").strip()
        or env.get("DEEPSEEK_BASE_URL", "").strip()
        or "https://api.deepseek.com/v1"
    )
    # DeepSeek base URL 必须带 /v1 后缀，否则 /chat/completions 会 404
    if deepseek_base and not deepseek_base.rstrip("/").endswith("/v1"):
        deepseek_base = deepseek_base.rstrip("/") + "/v1"

    # ---- 决定最终 key / base / model ----
    litellm_key = (
        env.get("LITELLM_OPENAI_API_KEY", "").strip()
        or deepseek_key
        or os.getenv("AI_IDE_LLM_API_KEY", "").strip()
        or os.getenv("AI_IDE_API_KEY", "").strip()
        or os.getenv("OPENAI_API_KEY", "").strip()
    )
    if _is_placeholder(litellm_key):
        litellm_key = ""

    # Base URL：deepseek 优先
    litellm_base = (
        env.get("LITELLM_OPENAI_API_BASE", "").strip()
        or (deepseek_base if deepseek_key else "")
        or os.getenv("OPENAI_BASE_URL", "").strip()
        or os.getenv("OPENAI_API_BASE", "").strip()
    )

    # Chat model：deepseek 优先
    if deepseek_key:
        deepseek_model = (
            env.get("DEEPSEEK_MODEL", "").strip()
            or os.getenv("DEEPSEEK_MODEL", "").strip()
            or "deepseek-chat"
        )
        if not deepseek_model.startswith(("deepseek/", "openai/")):
            deepseek_model = f"deepseek/{deepseek_model}"
        chat_model = deepseek_model
    else:
        chat_model = (
            env.get("CHAT_MODEL", "").strip()
            or os.getenv("CHAT_MODEL", "").strip()
            or "gpt-4-turbo"
        )
        if chat_model and not chat_model.startswith(("openai/", "azure/", "anthropic/", "huggingface/", "deepseek/")):
            chat_model = f"openai/{chat_model}"

    # ---- 写入 LITELLM_（RD-Agent settings）----
    if litellm_key:
        env["LITELLM_OPENAI_API_KEY"] = litellm_key
    if litellm_base:
        env["LITELLM_OPENAI_API_BASE"] = litellm_base
    if chat_model:
        env["LITELLM_CHAT_MODEL"] = chat_model

    # ---- 写入标准 OPENAI_（litellm HTTP 层 + 兼容旧路径）----
    # 当用 deepseek 时，必须覆盖 OPENAI_API_KEY/BASE，否则 litellm 会用讯飞的 key 打 deepseek
    if deepseek_key:
        env["OPENAI_API_KEY"] = deepseek_key
        env["OPENAI_BASE_URL"] = deepseek_base
        env["CHAT_MODEL"] = chat_model
    elif litellm_key and not env.get("OPENAI_API_KEY"):
        env["OPENAI_API_KEY"] = litellm_key
        if litellm_base and not env.get("OPENAI_BASE_URL"):
            env["OPENAI_BASE_URL"] = litellm_base

    return env


EMBEDDING_ENV_KEYS = ("EMBEDDING_MODEL", "EMBEDDING_BASE_URL", "EMBEDDING_API_KEY")
"""用户级向量检索配置要透传给挖掘子进程的键。

chat 与 embedding 是**独立通道**（DeepSeek 这类 chat 供应商没有 /embeddings 端点），
所以这几个键必须和 LITELLM_/OPENAI_ 那组一起进子进程 env。漏掉任一环，
「个人中心配了向量检索」就整条空转：界面显示已保存，挖掘却始终用容器级 .env。
"""


def embedding_overrides(llm_overrides: dict[str, Any] | None) -> dict[str, str]:
    """从用户级配置里挑出**非空**的 embedding 项。

    留空即沿用容器级 .env —— 与 `LLMConfig.llm_env_overrides` 的语义一致，
    也让「只换模型、沿用容器 key」这种半配置成立。
    """
    if not llm_overrides:
        return {}
    return {
        key: str(llm_overrides[key]).strip()
        for key in EMBEDDING_ENV_KEYS
        if str(llm_overrides.get(key) or "").strip()
    }
