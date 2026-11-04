"""轻量 LLM 调用工具 — 供 alpha-agent 因子解释等一次性调用使用。

支持两种 provider 协议：
  - OpenAI 兼容（DeepSeek / 阿里百炼 / 自建网关）：POST {base}/chat/completions
  - Anthropic 兼容（讯飞 MaaS astron 等网关）：POST {base}/v1/messages

凭证优先级（与 rd_agent/llm_env.build_llm_env 对齐）：
  key:   DEEPSEEK_API_KEY > AI_IDE_LLM_API_KEY > AI_IDE_API_KEY > OPENAI_API_KEY
  base:  DEEPSEEK_BASE_URL > AI_IDE_LLM_BASE_URL > OPENAI_BASE_URL > OPENAI_API_BASE
  model: DEEPSEEK_MODEL > AI_IDE_LLM_MODEL > CHAT_MODEL

协议识别：base_url 含 /anthropic 或 model 以 astron 开头 → Anthropic 协议；否则 OpenAI。
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field

import httpx

logger = logging.getLogger(__name__)

_PLACEHOLDER_KEYS = {"your-deepseek-api-key", "mock-api-key", "mock-api-key-not-configured"}


def _is_placeholder(key: str) -> bool:
    k = (key or "").strip().lower()
    return (not k) or any(p in k for p in _PLACEHOLDER_KEYS) or k.startswith("sk-在此")


def parse_extra_headers(raw: str | dict | None) -> dict[str, str]:
    """把自定义请求头（JSON 文本或 dict）解析成 {name: value}。

    非法/空输入返回空 dict（不抛异常，避免因一个头的格式问题阻断调用）。
    """
    if not raw:
        return {}
    data: dict | None = None
    if isinstance(raw, dict):
        data = raw
    elif isinstance(raw, str):
        text = raw.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                data = parsed
        except Exception:
            logger.warning("LLM extra headers is not valid JSON, ignored")
            return {}
    if not data:
        return {}
    return {str(k): str(v) for k, v in data.items() if k and v is not None}


def env_extra_headers() -> dict[str, str]:
    """全局兜底：从 LLM_EXTRA_HEADERS 环境变量读取自定义请求头。"""
    return parse_extra_headers(os.getenv("LLM_EXTRA_HEADERS", ""))


def openai_chat_url(base_url: str) -> str:
    """OpenAI 兼容 chat 端点：路径未带版本段时补 /v1，再拼 /chat/completions。

    已带版本段（/v1、/v4 等，如智谱 open.bigmodel.cn/api/paas/v4）直接使用，
    不再强补 /v1（曾导致 /v4/v1/chat/completions 404）。
    与实际调用、测试连接共用同一套规则，避免「测试通过、实际 404」。
    """
    base = (base_url or "").strip().rstrip("/")
    if not re.search(r"/v\d+$", base):
        base += "/v1"
    return f"{base}/chat/completions"


@dataclass(frozen=True)
class LLMConfig:
    api_key: str
    base_url: str
    model: str
    protocol: str  # "openai" | "anthropic"
    headers: dict[str, str] = field(default_factory=dict)

    def llm_env_overrides(self) -> dict[str, str]:
        """生成 RD-Agent 子进程的 LLM 环境变量覆盖。

        覆盖 LITELLM_*/OPENAI_*/CHAT_MODEL 全套，确保 build_llm_env 的
        优先级链（占位符过滤后）最终选中本配置。
        """
        env = {
            "LITELLM_OPENAI_API_KEY": self.api_key,
            "LITELLM_OPENAI_API_BASE": self.base_url,
            "OPENAI_API_KEY": self.api_key,
            "OPENAI_BASE_URL": self.base_url,
            "CHAT_MODEL": self.model,
            "REASONING_MODEL": self.model,
        }
        if self.headers:
            env["LLM_EXTRA_HEADERS"] = json.dumps(self.headers, ensure_ascii=False)
        return env


def resolve_llm_config() -> LLMConfig | None:
    """解析当前环境可用的 LLM 配置。无可用 key 返回 None。"""
    deepseek_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if _is_placeholder(deepseek_key):
        deepseek_key = ""

    if deepseek_key:
        base = os.getenv("DEEPSEEK_BASE_URL", "").strip() or "https://api.deepseek.com"
        base = base.rstrip("/")
        model = os.getenv("DEEPSEEK_MODEL", "").strip() or "deepseek-chat"
        # Anthropic 兼容端点（.../anthropic）：chat() 会再拼 /v1/messages，不能再补 /v1
        if "/anthropic" in base:
            return LLMConfig(api_key=deepseek_key, base_url=base, model=model, protocol="anthropic", headers=env_extra_headers())
        if not base.endswith("/v1"):
            base += "/v1"
        return LLMConfig(api_key=deepseek_key, base_url=base, model=model, protocol="openai", headers=env_extra_headers())

    key = (
        os.getenv("AI_IDE_LLM_API_KEY", "").strip()
        or os.getenv("AI_IDE_API_KEY", "").strip()
        or os.getenv("OPENAI_API_KEY", "").strip()
    )
    if _is_placeholder(key):
        return None

    base = (
        os.getenv("AI_IDE_LLM_BASE_URL", "").strip()
        or os.getenv("OPENAI_BASE_URL", "").strip()
        or os.getenv("OPENAI_API_BASE", "").strip()
        or "https://dashscope.aliyuncs.com/compatible-mode/v1"
    )
    model = (
        os.getenv("AI_IDE_LLM_MODEL", "").strip()
        or os.getenv("CHAT_MODEL", "").strip()
        or "deepseek-v3"
    )

    if "/anthropic" in base or model.lower().startswith("astron"):
        protocol = "anthropic"
    else:
        protocol = "openai"
    return LLMConfig(api_key=key, base_url=base, model=model, protocol=protocol, headers=env_extra_headers())


async def chat(
    messages: list[dict[str, str]],
    *,
    max_tokens: int = 500,
    temperature: float = 0.3,
    timeout: float = 30,
    config: LLMConfig | None = None,
) -> str:
    """调用 LLM 返回纯文本。messages 为 [{role, content}, ...]。

    config 不传时从环境变量解析；调用方可显式传入（如用户 Profile 中的 Key）。
    """
    cfg = config or resolve_llm_config()
    if cfg is None:
        raise RuntimeError("未配置可用的 LLM API Key（DEEPSEEK_API_KEY / AI_IDE_LLM_API_KEY / OPENAI_API_KEY 均为空或占位符）")

    async with httpx.AsyncClient(timeout=timeout) as client:
        if cfg.protocol == "anthropic":
            # Anthropic messages 格式：system 拆出，其余 role 仅 user/assistant
            sys_msgs = [m["content"] for m in messages if m["role"] == "system"]
            conv = [m for m in messages if m["role"] != "system"]
            payload: dict = {
                "model": cfg.model,
                "max_tokens": max_tokens,
                "messages": conv,
            }
            if sys_msgs:
                payload["system"] = "\n\n".join(sys_msgs)
            resp = await client.post(
                f"{cfg.base_url.rstrip('/')}/v1/messages",
                headers={
                    "x-api-key": cfg.api_key,
                    "Authorization": f"Bearer {cfg.api_key}",
                    "anthropic-version": "2023-06-01",
                    "Content-Type": "application/json",
                    **cfg.headers,
                },
                json=payload,
            )
        else:
            payload = {
                "model": cfg.model,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
            }
            resp = await client.post(
                openai_chat_url(cfg.base_url),
                headers={
                    "Authorization": f"Bearer {cfg.api_key}",
                    "Content-Type": "application/json",
                    **cfg.headers,
                },
                json=payload,
            )

        resp.raise_for_status()
        data = resp.json()
        if cfg.protocol == "anthropic":
            return "".join(b.get("text", "") for b in data.get("content", []))
        return data["choices"][0]["message"]["content"]
