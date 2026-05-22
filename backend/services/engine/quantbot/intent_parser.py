"""QuantBot 意图识别 — 使用 LLM 判断用户意图"""

import json
import logging
import os

import httpx

logger = logging.getLogger(__name__)

INTENT_SYSTEM_PROMPT = """\
你是一个量化投资助手的意图识别引擎。分析用户的输入，判断其意图类型。

返回 JSON 格式（不要输出其他任何内容）：
{
  "intent": "chat" | "factor_evolution",
  "factor_type": "价值|动量|波动|质量|成长|技术|综合" (仅 factor_evolution 时需要),
  "description": "用户需求描述" (仅 factor_evolution 时需要),
  "constraints": {"key": "value"} (可选，提取的参数如 stock_pool, min_icir, backtest_years 等)
}

触发 factor_evolution 的典型表达：
- "帮我挖掘XXX因子" / "evolve factors for XXX"
- "找一些低波动高收益的因子"
- "基于 Alpha191 做因子进化"
- "生成一批价值因子"
- "进化出XXX类型的因子"
- "挖掘XXX相关的因子"

其他所有情况返回 intent: "chat"。\
"""


def _get_llm_config() -> tuple[str, str, str]:
    """获取 LLM 配置"""
    base_url = (
        os.getenv("AI_IDE_LLM_BASE_URL")
        or os.getenv("AI_IDE_BASE_URL")
        or "https://api.deepseek.com"
    )
    model = os.getenv("AI_IDE_LLM_MODEL") or os.getenv("AI_IDE_MODEL") or "deepseek-v4-pro"
    api_key = (
        os.getenv("AI_IDE_LLM_API_KEY")
        or os.getenv("AI_IDE_API_KEY")
        or os.getenv("OPENAI_API_KEY", "")
    )
    return base_url, model, api_key


async def parse_intent(message: str, history: list[dict] | None = None) -> dict:
    """调用 LLM 解析用户意图"""
    base_url, model, api_key = _get_llm_config()

    if not api_key or "mock-api-key" in api_key:
        # 未配置 API key 时走规则匹配 fallback
        return _rule_based_intent(message)

    messages = [{"role": "system", "content": INTENT_SYSTEM_PROMPT}]
    if history:
        messages.extend(history[-6:])  # 只取最近 6 条历史消息
    messages.append({"role": "user", "content": message})

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "messages": messages,
                    "temperature": 0.1,
                    "max_tokens": 512,
                },
            )
            resp.raise_for_status()
            body = resp.json()
            content = body["choices"][0]["message"]["content"].strip()

            # 提取 JSON（兼容 ```json ... ``` 包裹）
            if content.startswith("```"):
                content = content.split("```", 2)[1]
                if content.startswith("json"):
                    content = content[4:]
                content = content.strip()

            return json.loads(content)
    except Exception as e:
        logger.warning(f"Intent LLM call failed, falling back to rule-based: {e}")
        return _rule_based_intent(message)


def _rule_based_intent(message: str) -> dict:
    """基于关键词的意图识别 fallback"""
    lower = message.lower()
    evolution_keywords = [
        "挖掘", "evolve", "进化", "生成因子", "factor evolution",
        "因子进化", "找一些", "生成一批", "挖掘因子",
    ]
    for kw in evolution_keywords:
        if kw in lower:
            return {
                "intent": "factor_evolution",
                "factor_type": "综合",
                "description": message,
                "constraints": {},
            }
    return {"intent": "chat"}
