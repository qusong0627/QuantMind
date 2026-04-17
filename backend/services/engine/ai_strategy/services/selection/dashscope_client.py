import os
from typing import Any, Dict, Optional

from openai import OpenAI

# 延迟加载配置，避免模块导入时触发验证
_ai_strategy_config = None


def _get_ai_strategy_config():
    """延迟获取配置，只在需要时才加载"""
    global _ai_strategy_config
    if _ai_strategy_config is None:
        try:
            from ...ai_strategy_config import get_config as _get_config
        except ImportError:
            from backend.services.engine.ai_strategy.ai_strategy_config import get_config as _get_config
        _ai_strategy_config = _get_config()
    return _ai_strategy_config


class DashScopeClient:
    """Minimal wrapper around DashScope (OpenAI-compatible) APIs."""

    MOCK_KEY_PATTERNS = ["mock-api-key", "not-configured", "placeholder"]

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
    ):
        self.api_key = api_key or os.getenv("DASHSCOPE_API_KEY")
        if not self.api_key:
            raise RuntimeError("DASHSCOPE_API_KEY is not configured")
        # 检测 mock key
        if any(pattern in self.api_key for pattern in self.MOCK_KEY_PATTERNS):
            raise RuntimeError(
                "DASHSCOPE_API_KEY 未配置真实密钥。请在个人中心配置您的 API Key。"
            )
        self.base_url = base_url or os.getenv("DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
        self.client = OpenAI(api_key=self.api_key, base_url=self.base_url)

    def get_embedding(
        self,
        text: str,
        model: str | None = None,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        config = _get_ai_strategy_config()
        model = model or config.DASHSCOPE_EMBEDDING_MODEL
        resp = self.client.embeddings.create(
            model=model,
            input=text,
            timeout=timeout or config.DASHSCOPE_EMBEDDING_TIMEOUT,
        )
        return {
            "model": resp.model,
            "vector": resp.data[0].embedding,
            "metadata": resp.to_dict(),
        }

    def health(self) -> str:
        """Lightweight check that the configured endpoint responds."""
        config = _get_ai_strategy_config()
        try:
            resp = self.client.embeddings.create(
                model=config.DASHSCOPE_EMBEDDING_MODEL,
                input="health check",
                timeout=config.DASHSCOPE_EMBEDDING_TIMEOUT,
            )
            return resp.model
        except Exception as exc:
            raise RuntimeError("DashScope health check failed") from exc


__all__ = ["DashScopeClient"]
