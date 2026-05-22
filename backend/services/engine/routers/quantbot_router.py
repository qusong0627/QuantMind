"""QuantBot 智能对话路由 — 替换 qwenpaw proxy"""

import json
import logging
import os
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from backend.services.engine.quantbot.intent_parser import parse_intent
from backend.services.engine.quantbot.rd_agent_launcher import RDAgentLauncher
from backend.services.engine.quantbot.task_store import QuantBotTaskStore

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/quantbot", tags=["QuantBot"])

task_store = QuantBotTaskStore()
launcher = RDAgentLauncher()


class ChatRequest(BaseModel):
    message: str
    history: list[dict[str, str]] | None = []


class ChatResponse(BaseModel):
    intent: str
    answer: str | None = None
    task_id: str | None = None


@router.post("/chat")
async def chat(request: Request, item: ChatRequest):
    """QuantBot 统一聊天接口

    - 一般对话：SSE 流式返回 LLM 回答
    - 因子挖掘：异步启动 RD-Agent 演化，返回 task_id
    """
    user_context = getattr(request.state, "user", None)
    user_id = user_context.get("user_id", "anonymous") if user_context else "anonymous"

    # 1. 意图识别
    history = item.history or []
    intent = await parse_intent(item.message, history)

    if intent.get("intent") == "factor_evolution":
        return await _handle_factor_evolution(item, user_id, intent)
    else:
        return await _handle_chat_stream(item, history)


async def _handle_factor_evolution(
    item: ChatRequest,
    user_id: str,
    intent: dict,
) -> dict:
    """处理因子挖掘请求"""
    # 检查 LLM API key 是否可用
    api_key = (
        os.getenv("AI_IDE_LLM_API_KEY")
        or os.getenv("AI_IDE_API_KEY")
        or os.getenv("OPENAI_API_KEY", "")
    )
    if not api_key or "mock-api-key" in api_key:
        raise HTTPException(
            status_code=500,
            detail="API Key 未配置。请先在个人中心配置 DeepSeek API Key 后再使用因子演化功能。",
        )

    # 创建任务
    task_request = {
        "message": item.message,
        "intent": intent,
    }
    task_id = await task_store.create_task(user_id, task_request)

    # 异步启动演化
    await launcher.launch_evolution(task_id, user_id, intent)

    return {
        "intent": "factor_evolution",
        "task_id": task_id,
        "answer": f"已启动因子演化任务：{intent.get('description', item.message)}",
    }


async def _handle_chat_stream(
    item: ChatRequest,
    history: list[dict],
) -> StreamingResponse:
    """一般对话 — SSE 流式"""
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

    if not api_key or "mock-api-key" in api_key:
        raise HTTPException(
            status_code=500,
            detail="API Key 未配置。请在个人中心配置您的 API Key。",
        )

    system_prompt = (
        "你是 QuantMind 的智能量化助手。用简洁友好的中文回答。"
        "不要主动生成策略代码，除非用户明确要求。"
    )

    messages = [{"role": "system", "content": system_prompt}]
    for msg in history[-10:]:
        messages.append({"role": msg.get("role", "user"), "content": msg.get("content", "")})
    messages.append({"role": "user", "content": item.message})

    async def event_generator():
        try:
            async with httpx.AsyncClient(timeout=300.0) as client:
                async with client.stream(
                    "POST",
                    f"{base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": model,
                        "messages": messages,
                        "stream": True,
                        "temperature": 0.7,
                    },
                ) as response:
                    if response.status_code != 200:
                        err_body = await response.aread()
                        logger.error(
                            f"QuantBot LLM Error: {response.status_code} {err_body.decode('utf-8', 'ignore')}"
                        )
                        yield f"data: {json.dumps({'error': f'AI 服务返回 {response.status_code}'})}\n\n"
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
                                    yield f"data: {json.dumps({'delta': delta['content']}, ensure_ascii=False)}\n\n"
                            except Exception:
                                continue
        except Exception as e:
            logger.error(f"QuantBot chat stream error: {e}")
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

        yield "data: [DONE]\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@router.get("/task/{task_id}")
async def get_task(task_id: str, request: Request):
    """查询任务状态和结果"""
    user_context = getattr(request.state, "user", None)
    user_id = user_context.get("user_id") if user_context else None

    task = await task_store.get_task(task_id, user_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    return {
        "task_id": task["task_id"],
        "status": task["status"],
        "progress": task.get("progress"),
        "result": task.get("result"),
        "error_message": task.get("error_message"),
        "factor_ids": task.get("factor_ids"),
        "created_at": task.get("created_at"),
        "updated_at": task.get("updated_at"),
        "completed_at": task.get("completed_at"),
    }


@router.get("/tasks")
async def list_tasks(request: Request):
    """列出当前用户的所有任务"""
    user_context = getattr(request.state, "user", None)
    if not user_context:
        raise HTTPException(status_code=401, detail="需要登录")

    user_id = user_context.get("user_id")
    tasks = await task_store.list_tasks(user_id)
    return {"tasks": tasks}
