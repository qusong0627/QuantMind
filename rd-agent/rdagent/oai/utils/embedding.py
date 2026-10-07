"""
Embedding utilities for handling token limits and text truncation.
"""

import os
from typing import Optional

from litellm import decode, encode, get_max_tokens, token_counter

from rdagent.log import rdagent_logger as logger
from rdagent.oai.llm_conf import LLM_SETTINGS

# Common embedding model token limits
EMBEDDING_MODEL_LIMITS = {
    "text-embedding-ada-002": 8191,
    "text-embedding-3-small": 8191,
    "text-embedding-3-large": 8191,
    "Qwen3-Embedding-8B": 32000,
    "Qwen3-Embedding-4B": 32000,
    "Qwen3-Embedding-0.6B": 32000,
    "bge-m3": 8191,
    "bce-embedding-base_v1": 511,
    "bge-large-zh-v1.5": 511,
    "bge-large-en-v1.5": 511,
}


def _has_known_provider_prefix(model: str) -> bool:
    """判断 model 是否已带 litellm 认识的 provider 前缀（如 "openai/xxx"、"jina_ai/xxx"）。

    用 litellm 自己的 provider_list 判定，避免硬编码列表随上游漂移。
    """
    head, sep, _ = model.partition("/")
    if not sep:
        return False
    try:
        from litellm import provider_list

        return head.lower() in {p.lower() for p in provider_list}
    except Exception:
        # litellm 结构变化时退化为「有斜杠即视为已带前缀」，不阻断主流程
        logger.warning("Could not read litellm.provider_list; assuming model carries a provider prefix")
        return True


def resolve_embedding_channel() -> tuple[str, str, str]:
    """解析 embedding 通道配置，返回 (model, api_base, api_key)。

    优先级（每项独立判断）：
      1. 标准环境变量 EMBEDDING_MODEL / EMBEDDING_BASE_URL / EMBEDDING_API_KEY
      2. ``LLM_SETTINGS`` 的字段默认值：``embedding_model`` /
         ``embedding_openai_base_url`` / ``embedding_openai_api_key``。
         ``LLMSettings`` 没有 ``env_prefix``（``oai/llm_conf.py:11``），所以它读的是
         **字段名大写**那一组：``EMBEDDING_MODEL`` / ``EMBEDDING_OPENAI_BASE_URL`` /
         ``EMBEDDING_OPENAI_API_KEY``。注意 ``LITELLM_`` 前缀是 ``LiteLLMSettings``
         用的，对这组字段**无效**（已实测：``LITELLM_EMBEDDING_MODEL`` 被忽略）。

    环境变量优先是刻意的：QuantMind 的 .env 与上游 health_check.py 用的是同一组
    ``EMBEDDING_*`` 名字（见 rdagent/app/utils/health_check.py），而 CLI / skill 路径
    不经过 QuantMind 的 env 构造，只有直读 env 才能让容器与 CLI 两条路都通。

    ⚠️ ``LLM_SETTINGS.embedding_model`` 的默认值是**非空**的
    ``"text-embedding-3-small"``，所以本函数的返回值非空**不代表有人配过** ——
    判断「是否真的配置了专用通道」要用 :func:`embedding_channel_is_explicit`。

    ``api_base`` 非空时会把没有 provider 前缀的 model 补成 ``openai/<model>``：
    自定义 base_url 意味着 OpenAI 兼容端点，而 litellm 要求显式 provider，
    否则 ``BAAI/bge-m3`` 会被解析成 provider=BAAI 并抛 "LLM Provider NOT provided"。
    """
    model = (os.environ.get("EMBEDDING_MODEL") or LLM_SETTINGS.embedding_model or "").strip()
    api_base = (os.environ.get("EMBEDDING_BASE_URL") or LLM_SETTINGS.embedding_openai_base_url or "").strip()
    api_key = (os.environ.get("EMBEDDING_API_KEY") or LLM_SETTINGS.embedding_openai_api_key or "").strip()

    if api_base and model and not _has_known_provider_prefix(model):
        model = f"openai/{model}"

    return model, api_base, api_key


def embedding_channel_is_explicit() -> bool:
    """专用 embedding 通道是否被**显式配置**过。

    ``LLM_SETTINGS.embedding_model`` 有非空默认值 ``text-embedding-3-small``
    （``oai/llm_conf.py:16``），所以 :func:`resolve_embedding_channel` 返回非空 model
    **不代表**有人配过。不区分这一点的话，完全未配置时会静默回落到全局
    ``OPENAI_API_KEY`` / ``OPENAI_BASE_URL``——那通常是 **chat 供应商**，多半没有
    ``/embeddings`` 端点，于是要么报一个与 embedding 无关的错，要么用错模型出向量。

    只要任一显式来源存在即视为已配置：显式设了 ``EMBEDDING_MODEL``（即便用的是
    OpenAI 官方端点 + ``OPENAI_API_KEY``）也算主动选择。
    """
    for key in ("EMBEDDING_MODEL", "EMBEDDING_BASE_URL", "EMBEDDING_API_KEY"):
        if (os.environ.get(key) or "").strip():
            return True
    return bool(
        (LLM_SETTINGS.embedding_openai_base_url or "").strip()
        or (LLM_SETTINGS.embedding_openai_api_key or "").strip()
    )


def get_embedding_max_tokens(model: str) -> int:
    """
    Get maximum token limit for embedding model.

    Three-level fallback strategy:
    1. Use litellm.get_max_tokens()
    2. Query EMBEDDING_MODEL_LIMITS mapping
    3. Use default value 8192

    Args:
        model: Model name

    Returns:
        Maximum token limit
    """
    # Remove prefix (e.g., "provider/model" -> "model")
    model_name = model.split("/")[-1] if "/" in model else model

    # Level 1: Try litellm
    try:
        max_tokens = get_max_tokens(model_name)
        if max_tokens and max_tokens > 0:
            return max_tokens
    except Exception as e:
        logger.warning(f"Failed to get max tokens for {model_name}: {e}")

    # Level 2: Query mapping table
    if model_name in EMBEDDING_MODEL_LIMITS:
        return EMBEDDING_MODEL_LIMITS[model_name]

    # Level 3: fallback to LLM_SETTINGS.embedding_max_length
    default_max_tokens = LLM_SETTINGS.embedding_max_length
    logger.warning(f"Unknown embedding model {model}, using default max_tokens={default_max_tokens}")
    return default_max_tokens


def trim_text_for_embedding(text: str, model: str, max_tokens: Optional[int] = None) -> str:
    """
    Truncate text for embedding model using encode/decode approach.

    Args:
        text: Input text
        model: Model name
        max_tokens: Maximum token limit, auto-detected if None. If still exceeds limit,
                   raises error directing user to set LLM_SETTINGS.embedding_max_length

    Returns:
        Truncated text
    """
    if not text:
        return ""

    # Get model's maximum token limit
    if max_tokens is None:
        max_tokens = get_embedding_max_tokens(model)

    # Apply safety margin
    safe_max_tokens = int(max_tokens * 0.9)

    # Calculate current token count
    current_tokens = token_counter(model=model, text=text)

    if current_tokens <= safe_max_tokens:
        return text

    logger.warning(
        f"Text too long for embedding model {model}: "
        f"{current_tokens} tokens > {safe_max_tokens} limit (with safety margin). "
        f"Truncating using encode/decode approach."
    )

    try:
        # Use encode/decode approach for precise truncation
        enc_ids = encode(model=model, text=text)
        enc_ids_trunc = enc_ids[:safe_max_tokens]
        text_trunc = decode(model=model, tokens=enc_ids_trunc)
        # Ensure we return a string type (mypy type safety)
        text_trunc = str(text_trunc) if text_trunc is not None else ""

        final_tokens = token_counter(model=model, text=text_trunc)
        logger.warning(f"Truncation completed: {current_tokens} -> {final_tokens} tokens")

        return text_trunc
    except Exception as e:
        raise RuntimeError(
            f"Failed to truncate text for embedding model {model}. "
            f"Please set LLM_SETTINGS.embedding_max_length to a smaller value. "
            f"Original error: {e}"
        ) from e


def truncate_content_list(content_list: list[str], model: str) -> list[str]:
    """
    Truncate a list of content strings.

    Args:
        content_list: List of content strings to truncate
        model: Model name

    Returns:
        List of truncated content strings
    """
    truncated_list = []
    for content in content_list:
        truncated_content = trim_text_for_embedding(content, model)
        truncated_list.append(truncated_content)

    return truncated_list
