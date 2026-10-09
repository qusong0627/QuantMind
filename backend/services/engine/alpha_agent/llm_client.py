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
from dataclasses import dataclass, field

import httpx

logger = logging.getLogger(__name__)

_PLACEHOLDER_KEYS = {
    "your-deepseek-api-key",
    "mock-api-key",
    "mock-api-key-not-configured",
}


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


def normalize_embedding_base_url(raw: str | None) -> str:
    """规整向量检索端点：去尾斜杠，缺 ``/v1`` 时补上。

    与 chat 通道同一套规则。embedding 端点必然是 OpenAI 兼容形态
    （``resolve_embedding_channel`` 会强制补 ``openai/`` 前缀，litellm 按
    ``{base}/embeddings`` 拼接），用户按习惯只填主机名
    （``https://api.siliconflow.cn``）时缺 ``/v1`` 会直接 404。

    留空返回空串 —— 调用方据此走「容器级 EMBEDDING_* 兜底」分支。
    """
    base = (raw or "").strip().rstrip("/")
    if base and not base.endswith("/v1"):
        base += "/v1"
    return base


def env_extra_headers() -> dict[str, str]:
    """全局兜底：从 LLM_EXTRA_HEADERS 环境变量读取自定义请求头。"""
    return parse_extra_headers(os.getenv("LLM_EXTRA_HEADERS", ""))


def openai_chat_url(base_url: str) -> str:
    """OpenAI 兼容 chat 端点：统一补齐 /v1 后拼 /chat/completions。

    与实际调用、测试连接共用同一套规则，避免「测试通过、实际 404」。
    """
    base = (base_url or "").strip().rstrip("/")
    if not base.endswith("/v1"):
        base += "/v1"
    return f"{base}/chat/completions"


@dataclass(frozen=True)
class LLMConfig:
    api_key: str
    base_url: str
    model: str
    protocol: str  # "openai" | "anthropic"
    headers: dict[str, str] = field(default_factory=dict)
    # 向量检索（embedding）通道，与 chat 独立。见 rd_agent/llm_env.py 与
    # rdagent/oai/utils/embedding.py:resolve_embedding_channel。
    embedding_model: str = ""
    embedding_base_url: str = ""
    embedding_api_key: str = ""

    def llm_env_overrides(self) -> dict[str, str]:
        """生成 RD-Agent 子进程的 LLM 环境变量覆盖。

        覆盖 LITELLM_*/OPENAI_*/CHAT_MODEL 全套，确保 build_llm_env 的
        优先级链（占位符过滤后）最终选中本配置。

        EMBEDDING_* 只在用户显式配置时才写出：未配置的项留给容器级 .env 兜底
        （子进程继承了 os.environ），这样「只在个人中心换 embedding 模型、沿用
        容器里的 key/base」也能生效。
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
        if self.embedding_model:
            env["EMBEDDING_MODEL"] = self.embedding_model
        if self.embedding_base_url:
            env["EMBEDDING_BASE_URL"] = self.embedding_base_url
        if self.embedding_api_key:
            env["EMBEDDING_API_KEY"] = self.embedding_api_key
        return env

    def embedding_env_overrides(self) -> dict[str, str]:
        """仅 embedding 三个变量（供不重建整套 chat env 的调用方使用）。"""
        return {
            k: v
            for k, v in self.llm_env_overrides().items()
            if k in ("EMBEDDING_MODEL", "EMBEDDING_BASE_URL", "EMBEDDING_API_KEY")
        }


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
            return LLMConfig(
                api_key=deepseek_key,
                base_url=base,
                model=model,
                protocol="anthropic",
                headers=env_extra_headers(),
            )
        if not base.endswith("/v1"):
            base += "/v1"
        return LLMConfig(
            api_key=deepseek_key,
            base_url=base,
            model=model,
            protocol="openai",
            headers=env_extra_headers(),
        )

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
    return LLMConfig(
        api_key=key,
        base_url=base,
        model=model,
        protocol=protocol,
        headers=env_extra_headers(),
    )


async def _chat_impl(
    messages: list[dict[str, str]],
    *,
    max_tokens: int,
    temperature: float,
    timeout: float,
    config: LLMConfig | None,
    extra_body: dict | None = None,
) -> tuple[str, dict]:
    cfg = config or resolve_llm_config()
    if cfg is None:
        raise RuntimeError(
            "未配置可用的 LLM API Key（DEEPSEEK_API_KEY / AI_IDE_LLM_API_KEY / OPENAI_API_KEY 均为空或占位符）"
        )

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
            if extra_body:
                payload.update(extra_body)
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
            if extra_body:
                payload.update(extra_body)
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
            text = "".join(b.get("text", "") for b in data.get("content", []))
            meta = {
                "model": cfg.model,
                "finish_reason": data.get("stop_reason"),
                "has_reasoning": False,
            }
        else:
            choice = data["choices"][0]
            message = choice.get("message") or {}
            text = message.get("content") or ""
            meta = {
                "model": cfg.model,
                "finish_reason": choice.get("finish_reason"),
                "has_reasoning": bool(message.get("reasoning_content")),
            }
        return text, meta


async def chat(
    messages: list[dict[str, str]],
    *,
    max_tokens: int = 500,
    temperature: float = 0.3,
    timeout: float = 30,
    config: LLMConfig | None = None,
    extra_body: dict | None = None,
) -> str:
    """调用 LLM 返回纯文本。messages 为 [{role, content}, ...]。

    config 不传时从环境变量解析；调用方可显式传入（如用户 Profile 中的 Key）。
    extra_body 合并进请求体（如 ``{"thinking": {"type": "disabled"}}`` 关推理
    模型的思考）——严格网关不认未知参数会 400，调用方自理回退。
    """
    text, _meta = await _chat_impl(
        messages,
        max_tokens=max_tokens,
        temperature=temperature,
        timeout=timeout,
        config=config,
        extra_body=extra_body,
    )
    return text


async def chat_with_meta(
    messages: list[dict[str, str]],
    *,
    max_tokens: int = 500,
    temperature: float = 0.3,
    timeout: float = 30,
    config: LLMConfig | None = None,
    extra_body: dict | None = None,
) -> tuple[str, dict]:
    """同 ``chat``，附带元信息 ``{model, finish_reason, has_reasoning}``。

    2026-10-09 起：推理模型（网关上的 deepseek-v4-flash 一类）的
    ``reasoning_content`` 也计入 max_tokens——预算被思考耗尽时可见输出会在
    JSON 中途被截断（``finish_reason="length"``）甚至为空（正文为空、
    ``has_reasoning=True``）。结构化输出调用方（doc_organize）据此把「静默的
    半截 JSON」升级为可操作报错；``chat`` 契约不变，元信息不改变返回值语义。
    """
    return await _chat_impl(
        messages,
        max_tokens=max_tokens,
        temperature=temperature,
        timeout=timeout,
        config=config,
        extra_body=extra_body,
    )
