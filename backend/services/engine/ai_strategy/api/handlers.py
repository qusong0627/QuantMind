#!/usr/bin/env python3
"""
API路由处理器模块
定义各种API端点的处理逻辑
"""

import json
import logging
from typing import Any, Dict

from fastapi import HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ..services import get_strategy_service

logger = logging.getLogger(__name__)


# 请求模型
class StrategyRequest(BaseModel):
    description: str
    market: str = "CN"
    risk_level: str = "medium"
    style: str = "simple"
    user_id: str = "desktop-user"
    provider: str = None


class ChatRequest(BaseModel):
    message: str
    user_id: str = "desktop-user"
    provider: str = None


async def root_handler() -> dict[str, Any]:
    """服务根端点处理器"""
    from ..provider_registry import get_provider

    try:
        get_provider()
        qwen_available = True
    except Exception:
        qwen_available = False

    return {
        "message": "QuantMind AI Strategy Service",
        "version": "3.1.0",
        "features": [
            "qwen_integration",
            "sse_streaming",
            "strategy_generation",
            "stock_selection",
        ],
        "qwen_available": qwen_available,
    }


async def health_check_handler() -> dict[str, Any]:
    """健康检查端点处理器"""
    from ..provider_registry import get_provider
    from ..services.startup_health import get_startup_health_state

    try:
        get_provider()
        qwen_available = True
    except Exception:
        qwen_available = False

    startup_state = get_startup_health_state().to_dict()
    
    return {
        "status": startup_state["status"],
        "service": "ai-strategy",
        "version": "3.1.0",
        "qwen_available": qwen_available,
        "startup_health": startup_state
    }


async def api_health_handler() -> dict[str, Any]:
    """API健康检查处理器"""
    from ..provider_registry import get_provider
    from ..services.startup_health import get_startup_health_state

    providers = {}
    try:
        get_provider()
        providers["qwen"] = {"is_healthy": True, "active": True}
    except Exception:
        providers["qwen"] = {"is_healthy": False, "active": False}

    startup_state = get_startup_health_state().to_dict()

    return {
        "status": startup_state["status"],
        "service": "ai-strategy",
        "providers": providers,
        "startup_health": startup_state
    }


async def generate_strategy_handler(request: StrategyRequest) -> dict[str, Any]:
    """策略生成端点处理器"""
    if not request.description.strip():
        raise HTTPException(status_code=400, detail="描述不能为空")

    # 记录策略生成请求
    logger.info(
        "Strategy generation request",
        extra={
            "user_id": request.user_id,
            "description": request.description[:100],  # 截取前100字符
            "market": request.market,
            "risk_level": request.risk_level,
            "style": request.style,
            "provider": request.provider,
        },
    )

    service = get_strategy_service()

    try:
        result = await service.generate_strategy(
            description=request.description,
            market=request.market,
            risk_level=request.risk_level,
            style=request.style,
            user_id=request.user_id,
        )
        return result
    except Exception as e:
        logger.error(
            "Strategy generation failed",
            extra={
                "error": str(e),
                "user_id": request.user_id,
                "description": request.description[:100],
            },
        )
        raise HTTPException(status_code=500, detail=f"策略生成失败: {str(e)}")


async def generate_strategy_stream_handler(
    request: StrategyRequest,
) -> StreamingResponse:
    """SSE流式策略生成端点处理器"""
    if not request.description.strip():
        raise HTTPException(status_code=400, detail="描述不能为空")

    async def event_generator():
        try:
            yield f"data: {json.dumps({'type': 'start', 'message': '开始生成策略...'})}\n\n"

            # 构建流式生成提示
            prompt = f"""请基于以下描述生成量化交易策略:

描述: {request.description}
市场: {request.market}
风险级别: {request.risk_level}

请详细生成策略代码和说明。"""

            service = get_strategy_service()
            async for chunk in service.generate_strategy_stream(prompt):
                yield chunk

        except Exception as e:
            logger.error(f"策略生成失败: {e}")
            yield f"data: {json.dumps({'type': 'error', 'error': str(e)})}\n\n"
        finally:
            yield "data: [DONE]\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "Access-Control-Allow-Origin": "*",
        },
    )


async def chat_stream_handler(request: ChatRequest) -> StreamingResponse:
    """SSE流式对话端点处理器"""
    if not request.message.strip():
        raise HTTPException(status_code=400, detail="消息不能为空")

    async def event_generator():
        try:
            yield f"data: {json.dumps({'type': 'text', 'content': '正在处理您的请求...'})}\n\n"

            service = get_strategy_service()
            async for chunk in service.chat_stream(request.message):
                yield chunk

        except Exception as e:
            logger.error(f"对话生成失败: {e}")
            yield f"data: {json.dumps({'type': 'error', 'error': str(e)})}\n\n"
        finally:
            yield "data: [DONE]\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "Access-Control-Allow-Origin": "*",
        },
    )


class RequestLoggingMiddleware:
    """请求日志中间件"""

    def __init__(self, logger: logging.Logger):
        self.logger = logger

    async def __call__(self, request: Request, call_next):
        """中间件处理逻辑"""
        import time
        import uuid

        start_time = time.time()
        request_id = (
            getattr(request.state, "trace_id", None) or request.headers.get("X-Request-Id") or str(uuid.uuid4())[:8]
        )

        # 记录请求开始
        self.logger.info(
            "Request started",
            extra={
                "request_id": request_id,
                "method": request.method,
                "url": str(request.url),
                "client_ip": request.client.host if request.client else "unknown",
                "user_agent": request.headers.get("user-agent", "unknown"),
            },
        )

        try:
            response = await call_next(request)
            process_time = time.time() - start_time

            # 记录请求完成
            self.logger.info(
                "Request completed",
                extra={
                    "request_id": request_id,
                    "method": request.method,
                    "url": str(request.url),
                    "status_code": response.status_code,
                    "process_time": round(process_time, 4),
                },
            )

            # 添加响应头
            response.headers["X-Request-ID"] = request_id
            if getattr(request.state, "trace_id", None):
                response.headers["X-Trace-Id"] = str(request.state.trace_id)
            response.headers["X-Process-Time"] = str(round(process_time, 4))

            return response

        except Exception as e:
            process_time = time.time() - start_time
            self.logger.error(
                "Request failed",
                extra={
                    "request_id": request_id,
                    "method": request.method,
                    "url": str(request.url),
                    "error": str(e),
                    "process_time": round(process_time, 4),
                },
            )
            raise
