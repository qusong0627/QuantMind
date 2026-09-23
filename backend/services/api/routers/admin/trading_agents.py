"""Admin proxy for TradingAgents engine API."""

from __future__ import annotations

import os

import httpx
from fastapi import APIRouter, Depends, Request, Response

from backend.services.api.user_app.middleware.auth import require_admin
from backend.shared.trusted_headers import sanitize_forward_headers

router = APIRouter(
    dependencies=[Depends(require_admin)],  # 路由器级认证兜底，新增端点默认受保护
)

ENGINE_BASE_URL = os.getenv("ENGINE_SERVICE_URL", "http://127.0.0.1:8001").rstrip("/")
PROXY_TIMEOUT = 300.0  # 5 min for long-running analysis


@router.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
    summary="Proxy TradingAgents requests to engine service",
)
async def proxy_to_engine(path: str, request: Request) -> Response:
    """Forward all /admin/trading-agents/* requests to engine service."""
    url = f"{ENGINE_BASE_URL}/api/v1/trading-agents/{path}"
    if request.url.query:
        url = f"{url}?{request.url.query}"

    # 唯一出处见 shared/trusted_headers.py。此前这里是手抄的一份清单且**漏了信任头**，
    # 客户端自带的 X-Internal-Call / X-User-Id 会原样递到 engine——下游
    # `shared/auth.get_current_user` 采信 X-Internal-Call 密钥匹配后的 X-User-Id。
    # （本 router 有 require_admin 兜底，所以危害限于「管理员可借这枚头冒充他人」，
    # 但它是同一个类：漏改一处就是一个洞。）
    headers = sanitize_forward_headers(request.headers.items())

    body = await request.body()

    async with httpx.AsyncClient(timeout=PROXY_TIMEOUT) as client:
        resp = await client.request(
            method=request.method,
            url=url,
            headers=headers,
            content=body,
        )

    return Response(
        content=resp.content,
        status_code=resp.status_code,
        headers=dict(resp.headers),
    )
