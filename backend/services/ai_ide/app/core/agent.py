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
        self.kb = KnowledgeBase(project_root)
        self.system_prompt = self._load_system_prompt()

    def _load_system_prompt(self):
        kb_context = self.kb.get_context_summary()
        return (
            "You are an expert quantitative researcher and Python developer for QuantMind.\n"
            "Your task is to help users develop, debug, and optimize trading strategies.\n\n"
            f"{kb_context}\n\n"
            "FORMATTING RULES:\n"
            "1. Use standard Markdown for all text. Use headers (##), bold, and lists to make it readable.\n"
            "2. When explaining issues, use a structured format: '## Analysis', '## Issues Found', etc.\n"
            "3. For code modifications, strictly use the SEARCH/REPLACE format:\n"
            "<<<< SEARCH\nOriginal code snippet\n====\nUpdated code snippet\n>>>>\n"
            "4. Ensure each SEARCH/REPLACE block is preceded by a brief explanation of what is being changed.\n"
            "5. Always add double newlines between paragraphs to ensure clear separation.\n"
            "6. IMPORTANT: For any new code, scripts, or examples (that are not Diff blocks), YOU MUST enclose them in standard Markdown code fences (e.g. ```python ... ```). NEVER output raw code without fences."
        )

    async def chat_stream(self, prompt: str, context: dict) -> AsyncGenerator[str, None]:
        messages = [{"role": "system", "content": self.system_prompt}]
        history = context.get("history", [])
        for msg in history:
            messages.append(msg)
        messages.append({"role": "user", "content": self._format_user_prompt(prompt, context)})

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
                                detail = obj.get("error", {}).get("message") or obj.get("message") or text
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
