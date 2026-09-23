import asyncio
import logging
import os
from typing import Dict, Optional
from collections.abc import Iterable

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response, StreamingResponse
from starlette.background import BackgroundTask

from backend.services.api.routers.proxy_error_mapping import map_upstream_http_error
from backend.shared.trusted_headers import HOP_HEADERS, sanitize_forward_headers
from backend.services.api.user_app.middleware.auth import get_optional_user
from backend.shared.auth import get_internal_call_secret

logger = logging.getLogger(__name__)

router = APIRouter()

# 统一指向 Engine 服务的端口
AI_IDE_SERVICE_URL = os.getenv("AI_IDE_SERVICE_URL", "http://127.0.0.1:8001").rstrip("/")

# 客户端绝不允许自带身份/内部调用 Header — 否则可冒充任意用户。
# 清单已收敛到 `shared/trusted_headers`（C1 续，2026-09-23）。


def _sanitize_request_headers(headers: Iterable, user: dict | None = None) -> dict[str, str]:
    out = sanitize_forward_headers(headers)

    # 添加内部调用凭证
    out["X-Internal-Call"] = get_internal_call_secret()
    if user:
        out["X-User-Id"] = str(user.get("user_id") or "")
        out["X-Tenant-Id"] = str(user.get("tenant_id") or "default")

    return out


def _sanitize_response_headers(headers: httpx.Headers) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in headers.items():
        if k.lower() in HOP_HEADERS:
            continue
        out[k] = v
    return out


async def _forward(request: Request, upstream_path: str, user: dict | None = None) -> Response:
    body = await request.body()
    url = f"{AI_IDE_SERVICE_URL}{upstream_path}"
    headers = _sanitize_request_headers(request.headers.items(), user)

    # LLM 或代码执行可能需要较长时间
    timeout = httpx.Timeout(connect=5.0, read=300.0, write=300.0, pool=10.0)

    max_retries = 3
    last_exc = None

    for attempt in range(max_retries):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                upstream = await client.request(
                    request.method,
                    url,
                    params=request.query_params,
                    content=body if body else None,
                    headers=headers,
                )
            return Response(
                content=upstream.content,
                status_code=upstream.status_code,
                headers=_sanitize_response_headers(upstream.headers),
            )
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            last_exc = exc
            if attempt < max_retries - 1:
                wait_time = (attempt + 1) * 1.0
                logger.warning(f"⚠️ AI-IDE Proxy Attempt {attempt + 1} failed ({exc}). Retrying in {wait_time}s...")
                await asyncio.sleep(wait_time)
                continue
            break
        except httpx.HTTPError as exc:
            last_exc = exc
            break

    raise map_upstream_http_error("ai_ide", last_exc or Exception("Unknown AI-IDE proxy error"))


async def _forward_stream(request: Request, upstream_path: str, user: dict | None = None) -> StreamingResponse:
    body = await request.body()
    url = f"{AI_IDE_SERVICE_URL}{upstream_path}"
    headers = _sanitize_request_headers(request.headers.items(), user)

    max_retries = 3
    last_exc = None

    for attempt in range(max_retries):
        client = httpx.AsyncClient(timeout=None)
        req = client.build_request(
            request.method,
            url,
            params=request.query_params,
            content=body if body else None,
            headers=headers,
        )
        try:
            upstream = await client.send(req, stream=True)

            async def _cleanup() -> None:
                await upstream.aclose()
                await client.aclose()

            return StreamingResponse(
                upstream.aiter_raw(),
                status_code=upstream.status_code,
                headers=_sanitize_response_headers(upstream.headers),
                background=BackgroundTask(_cleanup),
            )
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            last_exc = exc
            await client.aclose()
            if attempt < max_retries - 1:
                wait_time = (attempt + 1) * 1.0
                logger.warning(f"⚠️ AI-IDE Stream Proxy Attempt {attempt + 1} failed ({exc}). Retrying in {wait_time}s...")
                await asyncio.sleep(wait_time)
                continue
            break
        except httpx.HTTPError as exc:
            last_exc = exc
            await client.aclose()
            break

    raise map_upstream_http_error("ai_ide", last_exc or Exception("Unknown AI-IDE stream proxy error"))


# 统一捕获 /api/v1/ai-ide 下的所有请求
@router.api_route(
    "/api/v1/ai-ide/{subpath:path}",
    methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    include_in_schema=False,
)
async def proxy_ai_ide(subpath: str, request: Request, user: dict | None = Depends(get_optional_user)):
    upstream_path = f"/api/v1/ai-ide/{subpath}"

    # 针对流式回复接口使用 StreamingResponse
    is_streaming = False
    if subpath == "ai/chat":
        is_streaming = True
    elif subpath.startswith("execute/logs/"):
        is_streaming = True
    elif subpath.startswith("strategy-lab/run/") and subpath.endswith("/status"):
        is_streaming = True

    if is_streaming:
        return await _forward_stream(request, upstream_path, user)
    return await _forward(request, upstream_path, user)
