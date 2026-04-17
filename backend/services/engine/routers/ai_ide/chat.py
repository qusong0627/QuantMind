import os
import json
import logging
import httpx
from typing import List, Optional, Dict, Any
from collections.abc import AsyncGenerator
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

logger = logging.getLogger(__name__)
router = APIRouter()

# --- Core Logic (Ported from AI-IDE Agent) ---

class KnowledgeBase:
    def __init__(self, project_root: str):
        self.project_root = project_root
        self.doc_paths = [
            "docs/QuantMind_Qlib内部策略开发规范.md",
            "docs/Qlib策略模板说明.md",
            "docs/Qlib基础文档.md",
            "GEMINI.md",
        ]
        self.cached_context = ""

    def get_context_summary(self) -> str:
        if self.cached_context:
            return self.cached_context
        summary = "### QuantMind Project Standards & API Reference:\n"
        for doc_rel_path in self.doc_paths:
            full_path = os.path.join(self.project_root, doc_rel_path)
            if os.path.exists(full_path):
                try:
                    with open(full_path, encoding="utf-8") as f:
                        content = f.read()
                        summary += f"\n-- From {doc_rel_path} --\n{content[:1500]}\n"
                except Exception as e:
                    summary += f"\n-- Error reading {doc_rel_path}: {str(e)} --\n"
        self.cached_context = summary
        return summary

class QuantAgent:
    def __init__(self, api_key: str, base_url: str, model: str, project_root: str):
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self.kb = KnowledgeBase(project_root)
        self.system_prompt = self._load_system_prompt()

    def _load_system_prompt(self):
        kb_context = self.kb.get_context_summary()
        return (
            "你是 QuantMind 的资深量化工程助手，负责帮助用户编写、调试和优化交易策略。\n"
            "请始终使用简体中文回答，优先给出结论，再给出可执行步骤。\n"
            "如果涉及代码修改，优先输出最小改动，并明确说明文件路径；需要替换代码时尽量使用 SEARCH/REPLACE 格式。\n"
            "4. 如果信息不足，请先提问，不要擅自假设。\n"
            "5. 策略回测默认时间跨度为近 1 年。若信号数据无法覆盖回测区间，系统将执行“自适应截断”：即自动将回测终点对齐到预测数据的最后一天，以保证结论的严谨性。\n\n"
            f"{kb_context}\n\n"
            "FORMATTING RULES:\n"
            "1. 使用标准 Markdown 输出。\n"
            "2. 涉及代码修改时，优先使用 SEARCH/REPLACE：\n"
            "<<<< SEARCH\n原始代码\n====\n修改后代码\n>>>>\n"
            "3. 新增代码请使用三引号代码块，例如 ```python ... ```。"
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
                    try:
                        err_body = await response.aread()
                        logger.error(f"LLM API Error: Status={response.status_code}, Body={err_body.decode('utf-8', 'ignore')}")
                    except: pass
                    yield f"**Error:** AI 服务返回状态码 {response.status_code}。请确认“设置”中的 API Key 是否正确有效。"
                    return

                async for line in response.aiter_lines():
                    if not line.strip(): continue
                    if line.startswith("data: [DONE]"): break
                    if line.startswith("data: "):
                        try:
                            data = json.loads(line[6:])
                            delta = data["choices"][0]["delta"]
                            if "content" in delta:
                                yield delta["content"]
                        except: continue

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
        if current_code: prompt += f"\n[Current Code]:\n```python\n{current_code}\n```\n"
        if selection: prompt += f"\n[Selection]:\n```python\n{selection}\n```\n"
        if error_msg: prompt += f"\n[Error]:\n{error_msg}\n"
        return prompt

# --- API Router Logic ---

class ChatMessage(BaseModel):
    role: str
    content: str

class ChatRequest(BaseModel):
    message: str
    current_code: str | None = None
    selection: str | None = None
    error_msg: str | None = None
    file_path: str | None = None
    history: list[ChatMessage] | None = []
    extra_context: dict[str, Any] | None = None

@router.post("/chat")
async def chat_completions(request: Request, item: ChatRequest):
    # 1. 解析基础配置 (支持多种环境变量命名方式以对齐服务器 .env)
    base_url = (
        os.getenv("AI_IDE_LLM_BASE_URL")
        or os.getenv("AI_IDE_BASE_URL")
        or os.getenv("OPENAI_API_BASE")
        or "https://dashscope.aliyuncs.com/compatible-mode/v1"
    )
    model = (
        os.getenv("AI_IDE_LLM_MODEL")
        or os.getenv("AI_IDE_MODEL")
        or "qwen-max"
    )
    api_key = (
        os.getenv("AI_IDE_LLM_API_KEY")
        or os.getenv("AI_IDE_API_KEY")
        or os.getenv("OPENAI_API_KEY", "")
    )

    # 2. 尝试从网关透传的用户身份获取私有 API Key (个人 Key 优先级最高)
    user_context = getattr(request.state, "user", None)
    if user_context:
        user_id = user_context.get("user_id")
        tenant_id = user_context.get("tenant_id", "default")
        # 尝试从 User Service 动态获取当前用户的私有 Key
        try:
            from backend.shared.auth import get_internal_call_secret
            # OSS 单容器模式使用 127.0.0.1
            api_gateway = os.getenv("INTERNAL_API_GATEWAY_URL", "http://127.0.0.1:8000")
            async with httpx.AsyncClient(timeout=3.0) as client:
                headers = {
                    "X-Internal-Call": get_internal_call_secret(),
                    "X-User-Id": user_id,
                    "X-Tenant-Id": tenant_id
                }
                resp = await client.get(f"{api_gateway}/api/v1/profiles/{user_id}", headers=headers)
                if resp.status_code == 200:
                    profile_key = resp.json().get("data", {}).get("ai_ide_api_key")
                    if profile_key:
                        api_key = profile_key
        except Exception as e:
            logger.warning(f"Could not fetch individual API key for user {user_id}: {e}")

    # 获取项目根目录，以便读取文档
    project_root = os.getcwd()

    # 打印非敏感初始化参数以便诊断
    masked_key = f"{api_key[:6]}...{api_key[-4:]}" if len(api_key) > 10 else "***"
    logger.info(f"Initializing QuantAgent: model={model}, base_url={base_url}, api_key={masked_key}")

    # 检测 mock key
    mock_key_patterns = ["mock-api-key", "not-configured", "placeholder"]
    if not api_key or any(pattern in api_key for pattern in mock_key_patterns):
        logger.error("Chat failed: API Key is empty or mock key")
        raise HTTPException(
            status_code=500,
            detail="API Key 未配置。请在个人中心配置您的 API Key。"
        )

    agent = QuantAgent(api_key, base_url, model, project_root)

    context = {
        "current_code": item.current_code,
        "selection": item.selection,
        "error_msg": item.error_msg,
        "file_path": item.file_path,
        "history": [m.model_dump() for m in (item.history or [])],
        **(item.extra_context or {}),
    }

    async def event_generator():
        try:
            async for chunk in agent.chat_stream(item.message, context):
                if chunk:
                    data = json.dumps({"delta": chunk}, ensure_ascii=False)
                    yield f"data: {data}\n\n"
        except Exception as e:
            logger.error(f"Chat stream error: {e}")
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")

@router.post("/stop")
async def stop_chat():
    return {"status": "stopped"}
