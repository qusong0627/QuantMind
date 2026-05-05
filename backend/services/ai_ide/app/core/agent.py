import json
from collections.abc import AsyncGenerator

import httpx

from backend.services.ai_ide.app.core.knowledge_base import KnowledgeBase


class QuantAgent:
    """
    负责量化代码生成的智能体。
    """

    def __init__(self, api_key: str, base_url: str, model: str, project_root: str):
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self.project_root = project_root
        self.kb = KnowledgeBase(project_root)
        self._kb_context_cached = None
        self._system_prompt_base = self._build_system_prompt_base()

    def _build_system_prompt_base(self):
        """构建不包含知识库的 system prompt 基础部分"""
        return (
            "You are an expert quantitative researcher and Python developer for QuantMind.\n"
            "Your task is to help users develop, debug, and optimize trading strategies.\n\n"
            "IMPORTANT RULES:\n"
            "1. Do NOT generate any strategy code unless the user explicitly asks for it.\n"
            "2. For greetings, small talk, thanks, or non-technical messages, respond briefly and friendly (1-2 sentences). Do NOT provide code examples or start technical discussions.\n"
            "3. Only provide professional advice and executable code when the user asks specific technical questions about strategy development, code writing, or backtesting analysis.\n"
            "4. Always confirm user intent before taking action. Never assume the user wants to generate a strategy.\n\n"
            "FORMATTING RULES:\n"
            "1. Use standard Markdown for all text. Use headers (##), bold, and lists to make it readable.\n"
            "2. When explaining issues, use a structured format: '## Analysis', '## Issues Found', etc.\n"
            "3. For code modifications, strictly use the SEARCH/REPLACE format:\n"
            "<<<< SEARCH\nOriginal code snippet\n====\nUpdated code snippet\n>>>>\n"
            "4. Ensure each SEARCH/REPLACE block is preceded by a brief explanation of what is being changed.\n"
            "5. Always add double newlines between paragraphs to ensure clear separation.\n"
            "6. IMPORTANT: For any new code, scripts, or examples (that are not Diff blocks), YOU MUST enclose them in standard Markdown code fences (e.g. ```python ... ```). NEVER output raw code without fences."
        )

    def _get_system_prompt(self, user_input: str, context: dict) -> str:
        """根据用户输入动态构建 system prompt

        仅在用户提出技术问题时才注入知识库文档。
        """
        is_technical = self._is_technical_query(user_input, context)

        if is_technical:
            if self._kb_context_cached is None:
                self._kb_context_cached = self.kb.get_context_summary()
            return f"{self._system_prompt_base}\n\n### QuantMind Project Standards & API Reference:\n{self._kb_context_cached}"
        else:
            return self._system_prompt_base

    def _is_technical_query(self, user_input: str, context: dict) -> bool:
        """判断用户输入是否是技术性查询"""
        if (
            context.get("current_code")
            or context.get("error_msg")
            or context.get("selection")
        ):
            return True

        history = context.get("history", [])
        for msg in history:
            if msg.get("role") in ["user", "assistant"] and any(
                kw in msg.get("content", "").lower()
                for kw in [
                    "策略",
                    "代码",
                    "回测",
                    "指标",
                    "macd",
                    "kdj",
                    "rsi",
                    "boll",
                    "strategy",
                    "code",
                    "backtest",
                    "indicator",
                    "买入",
                    "卖出",
                    "因子",
                    "模型",
                    "预测",
                    "factor",
                    "model",
                    "predict",
                ]
            ):
                return True

        user_lower = user_input.lower()
        technical_keywords = [
            "策略",
            "代码",
            "回测",
            "指标",
            "macd",
            "kdj",
            "rsi",
            "boll",
            "布林",
            "均线",
            "ma",
            "ema",
            "sma",
            "技术指标",
            "backtest",
            "信号",
            "signal",
            "买入",
            "卖出",
            "buy",
            "sell",
            "cross",
            "金叉",
            "死叉",
            "突破",
            "支撑",
            "压力",
            "模型",
            "预测",
            "机器学习",
            "ml",
            "ai",
            "深度学习",
            "神经网络",
            "strategy",
            "code",
            "indicator",
            "factor",
            "alpha",
            "score",
            "config",
            "修改",
            "优化",
            "调试",
            "error",
            "bug",
            "报错",
            "异常",
            "fix",
            "修复",
            "debug",
        ]
        return any(kw in user_lower for kw in technical_keywords)

    async def chat_stream(
        self, prompt: str, context: dict
    ) -> AsyncGenerator[str, None]:
        # 动态生成 system prompt（根据用户输入决定是否注入知识库）
        system_prompt = self._get_system_prompt(prompt, context)
        messages = [{"role": "system", "content": system_prompt}]
        history = context.get("history", [])
        for msg in history:
            messages.append(msg)
        messages.append(
            {"role": "user", "content": self._format_user_prompt(prompt, context)}
        )

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        payload = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "temperature": 0.1,
        }

        async with httpx.AsyncClient(timeout=300.0) as client:
            async with client.stream(
                "POST",
                f"{self.base_url}/chat/completions",
                headers=headers,
                json=payload,
            ) as response:
                if response.status_code != 200:
                    detail = ""
                    try:
                        raw = await response.aread()
                        text = raw.decode("utf-8", errors="ignore").strip()
                        if text:
                            try:
                                obj = json.loads(text)
                                detail = (
                                    obj.get("error", {}).get("message")
                                    or obj.get("message")
                                    or text
                                )
                            except Exception:
                                detail = text
                    except Exception:
                        detail = ""
                    msg = f"**Error:** API returned status {response.status_code}"
                    if detail:
                        msg += f" - {detail[:300]}"
                    yield msg
                    return

                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    if line.startswith("data: [DONE]"):
                        break
                    if line.startswith("data: "):
                        try:
                            data = json.loads(line[6:])
                            delta = data["choices"][0]["delta"]
                            if "content" in delta:
                                # 保持原始文本输出，不做额外包装
                                yield delta["content"]
                        except Exception:
                            continue

    def _format_user_prompt(self, user_input: str, context: dict) -> str:
        assistant_rules = str(context.get("assistant_rules", "") or "").strip()
        current_code = context.get("current_code", "")
        error_msg = context.get("error_msg", "")
        selection = context.get("selection", "")
        file_path = str(context.get("file_path", "") or "").strip()

        prompt = f"User Request: {user_input}\n"
        if assistant_rules:
            prompt += f"\n[Development Rules]:\n{assistant_rules}\n"
        if file_path:
            prompt += f"\n[File Path]:\n{file_path}\n"
        if current_code:
            prompt += f"\n[Current Code Context]:\n```python\n{current_code}\n```\n"
        if selection:
            prompt += f"\n[User Selection]:\n```python\n{selection}\n```\n"
        if error_msg:
            prompt += f"\n[Error Message]:\n{error_msg}\n"
        return prompt
