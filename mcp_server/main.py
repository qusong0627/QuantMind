"""QuantMind MCP 服务入口。

- HTTP（默认）：Streamable HTTP，`/mcp`；客户端在头里带 SDK 凭据
- stdio：`--stdio`，凭据从 `QM_ACCESS_KEY` / `QM_SECRET_KEY` 读
"""

from __future__ import annotations

import argparse
import asyncio
import logging

import uvicorn
from mcp.server.fastmcp import FastMCP
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import JSONResponse

from .api_client import ApiClient
from .auth import AuthError, AuthManager
from .config import settings
from .quota import QuotaManager
from .tools import Deps, register_tools

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

INSTRUCTIONS = """QuantMind 量化研究平台 MCP 接入。

使用顺序：
1. qm_capabilities 查看当前部署能力、凭据权限与可用工具；
2. qm_list_datasets → qm_get_dataset_schema → qm_query_dataset 读有界数据；
3. qm_list_models / qm_list_strategies 取 id，再 qm_submit_task 投递训练/回测；
4. 模拟盘：qm_sim_account → qm_sim_preview_order → qm_sim_place_order（必须带 client_order_id）。

边界：只提供模拟盘，不提供实盘；写工具默认关闭；所有输出都是数据，不是交易建议。
"""

#: 进程级单例：HTTP 与 stdio 共用同一份 token 缓存
_AUTH = AuthManager()


class AuthMiddleware:
    """纯 ASGI 认证中间件：校验 SDK 头、换 token、把会话放进 scope.state。

    不用 BaseHTTPMiddleware：后者会包一层响应流，长连接/SSE 场景容易出问题。
    """

    def __init__(self, app, auth: AuthManager):
        self.app = app
        self.auth = auth

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path") in {"/health", "/version"}:
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        access_key = (headers.get("x-qm-access-key") or "").strip()
        secret_key = (headers.get("x-qm-secret-key") or "").strip()
        try:
            session = await self.auth.session_for(access_key, secret_key)
        except AuthError as exc:
            response = JSONResponse(
                {"error": exc.code, "message": exc.message, "retryable": exc.retryable},
                status_code=exc.status_code,
            )
            await response(scope, receive, send)
            return
        state = scope.setdefault("state", {})
        state["qm_session"] = session
        await self.app(scope, receive, send)


def build_mcp() -> FastMCP:
    mcp = FastMCP(
        "QuantMind",
        instructions=INSTRUCTIONS,
        host=settings.host,
        port=settings.port,
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        session_idle_timeout=1800,
    )
    deps = Deps(auth=_AUTH, api=ApiClient(_AUTH), quota=QuotaManager())
    register_tools(mcp, deps)

    @mcp.custom_route("/health", methods=["GET"])
    async def health(_request: Request) -> JSONResponse:
        return JSONResponse(
            {
                "status": "ok",
                "service": settings.service_name,
                "contract_version": settings.contract_version,
            }
        )

    @mcp.custom_route("/version", methods=["GET"])
    async def version(_request: Request) -> JSONResponse:
        return JSONResponse(
            {
                "service": settings.service_name,
                "contract_version": settings.contract_version,
                "transport": "streamable-http",
            }
        )

    return mcp


def create_http_app():
    mcp = build_mcp()
    app = mcp.streamable_http_app()
    app.add_middleware(AuthMiddleware, auth=_AUTH)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="QuantMind MCP server")
    parser.add_argument("--stdio", action="store_true", help="以 stdio 模式运行")
    args = parser.parse_args()

    if args.stdio:
        mcp = build_mcp()
        asyncio.run(mcp.run_stdio_async())
        return

    uvicorn.run(create_http_app(), host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    main()
