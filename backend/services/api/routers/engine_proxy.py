"""
Engine 服务代理路由 (V6 终极兜底版)

捕获所有未被具体路由匹配的 /api/v1 流量并转发至 Engine。
"""

import asyncio
import logging
import os
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, Request, Response

from backend.services.api.routers.proxy_error_mapping import map_upstream_http_error
from backend.services.api.user_app.middleware.auth import get_optional_user
from backend.shared.auth import get_internal_call_secret
from backend.shared.trusted_headers import sanitize_forward_headers

logger = logging.getLogger(__name__)

ENGINE_BASE_URL = os.getenv("ENGINE_SERVICE_URL", "http://127.0.0.1:8001").rstrip("/")
ENGINE_PROXY_TIMEOUT_SECONDS = float(os.getenv("ENGINE_PROXY_TIMEOUT_SECONDS", "120"))
ENGINE_PROXY_LLM_TIMEOUT_SECONDS = float(os.getenv("ENGINE_PROXY_LLM_TIMEOUT_SECONDS", "600"))

# 客户端绝不允许自带身份/内部调用 Header — 否则可冒充任意用户。
# 清单已收敛到 `shared/trusted_headers`（C1 续，2026-09-23）：不再本地手抄，
# 新增信任头只改那一处。转发时统一走 `sanitize_forward_headers()`。

# 注意：这里不设 prefix，在 main.py 挂载
router = APIRouter()


def _resolve_timeout_seconds(path: str) -> float:
    # LLM 生成类接口常超过普通代理时长，单独使用更长超时。
    if path.startswith("/api/v1/strategy/generate"):
        return ENGINE_PROXY_LLM_TIMEOUT_SECONDS
    # 回测历史/结果查询单独设置超时，避免大数据量时 504
    if path.startswith("/api/v1/backtest/") or path.startswith("/api/v1/qlib/"):
        return max(ENGINE_PROXY_TIMEOUT_SECONDS, 300.0)
    # 策略/模板/健康检查类接口使用更短超时，避免前端长时间等待
    if path.startswith("/api/v1/strategies/") or path.endswith("/health"):
        return 15.0
    return ENGINE_PROXY_TIMEOUT_SECONDS


async def _proxy(request: Request, user: dict | None = None) -> Response:
    path = request.url.path
    url = f"{ENGINE_BASE_URL}{path}"
    if request.url.query:
        url = f"{url}?{request.url.query}"

    headers = sanitize_forward_headers(request.headers.items())
    headers["X-Internal-Call"] = get_internal_call_secret()

    if user:
        headers["X-User-Id"] = str(user.get("user_id") or "")
        headers["X-Tenant-Id"] = str(user.get("tenant_id") or "default")

    body = await request.body()

    logger.debug(f"Engine Proxying: {request.method} {url}")

    timeout_seconds = _resolve_timeout_seconds(path)

    # 针对主机名解析失败增加更激进的重试机制
    max_retries = 3
    last_exc = None

    for attempt in range(max_retries):
        try:
            # 简化 client 创建，移除自定义 transport 实验，回归标准模式
            async with httpx.AsyncClient(timeout=timeout_seconds, trust_env=False) as client:
                resp = await client.request(
                    method=request.method,
                    url=url,
                    headers=headers,
                    content=body if body else None,
                    follow_redirects=True,
                )
            return Response(
                content=resp.content,
                status_code=resp.status_code,
                headers=dict(resp.headers),
                media_type=resp.headers.get("content-type"),
            )
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            last_exc = exc
            # 记录详细错误，方便调试
            logger.warning(
                f"⚠️ Engine Proxy Attempt {attempt + 1} failed: {exc}. Target: {url}"
            )
            if attempt < max_retries - 1:
                wait_time = (attempt + 1) * 1.5
                await asyncio.sleep(wait_time)
                continue
            break
        except Exception as exc:
            last_exc = exc
            break

    logger.error(
        f"❌ ENGINE PROXY FINAL FAILURE: {request.method} {url} -> {type(last_exc).__name__}: {last_exc}"
    )
    raise map_upstream_http_error("engine", last_exc or Exception("Unknown proxy error"))


# 终极捕获规则：匹配所有策略、回测、推理相关的已知路径
@router.api_route(
    "/api/v1/strategies/{p:path}", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"], include_in_schema=False
)
@router.api_route("/api/v1/strategies", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"], include_in_schema=False)
@router.api_route(
    "/api/v1/strategy/{p:path}", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"], include_in_schema=False
)
@router.api_route("/api/v1/backtest/{p:path}", methods=["GET", "POST", "DELETE", "OPTIONS"], include_in_schema=False)
@router.api_route("/api/v1/qlib/{p:path}", methods=["GET", "POST", "DELETE", "OPTIONS"], include_in_schema=False)
@router.api_route("/api/v1/analysis/{p:path}", methods=["GET", "POST", "OPTIONS"], include_in_schema=False)
@router.api_route("/api/v1/inference/{p:path}", methods=["GET", "POST", "OPTIONS"], include_in_schema=False)
@router.api_route("/api/v1/selection/{p:path}", methods=["GET", "POST", "OPTIONS"], include_in_schema=False)
@router.api_route(
    "/api/v1/stocks/{p:path}", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"], include_in_schema=False
)
@router.api_route("/api/v1/stocks", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"], include_in_schema=False)
@router.api_route("/api/v1/stock-pools", methods=["GET", "OPTIONS"], include_in_schema=False)
@router.api_route(
    "/api/v1/stock-pools/{p:path}", methods=["GET", "OPTIONS"], include_in_schema=False
)
@router.api_route(
    "/api/v1/rd-agent/{p:path}", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"], include_in_schema=False
)
@router.api_route("/api/v1/rd-agent", methods=["GET", "POST", "OPTIONS"], include_in_schema=False)
@router.api_route(
    "/api/v1/alpha-agent/{p:path}", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"], include_in_schema=False
)
@router.api_route("/api/v1/alpha-agent", methods=["GET", "POST", "OPTIONS"], include_in_schema=False)
@router.api_route(
    "/api/v1/trading-agents/{p:path}", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"], include_in_schema=False
)
@router.api_route("/api/v1/trading-agents", methods=["GET", "POST", "OPTIONS"], include_in_schema=False)
@router.api_route(
    "/api/v1/quantbot/{p:path}", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"], include_in_schema=False
)
@router.api_route(
    "/api/v1/strategy-lab/{p:path}", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"], include_in_schema=False
)
@router.api_route(
    "/api/v1/strategy-lab", methods=["GET", "POST", "OPTIONS"], include_in_schema=False
)
@router.api_route(
    "/api/v1/factor-report/{p:path}", methods=["GET", "POST", "OPTIONS"], include_in_schema=False
)
@router.api_route(
    "/api/v1/factor-report", methods=["GET", "POST", "OPTIONS"], include_in_schema=False
)
@router.api_route(
    "/api/v1/factor-research/{p:path}", methods=["GET", "POST", "OPTIONS"], include_in_schema=False
)
@router.api_route(
    "/api/v1/factor-research", methods=["GET", "POST", "OPTIONS"], include_in_schema=False
)
async def engine_catch_all(request: Request, user: dict | None = Depends(get_optional_user)):
    return await _proxy(request, user)
