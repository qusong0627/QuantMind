"""arena 数据面 · 族 10 路由层：`/quantmind/*` 受限透传（上游纯路径透传的收窄版）。

上游 baymax `quantmind_proxy.py` 把 `/api/quantmind/{path}` **原样透传**到 QM
`/api/v1/{path}`（管理台登录 token 由代理注入）。迁移后不再需要「代理登录」：
调用者就是 QM 本人的会话，我们把**调用者自己的 Bearer 原样带上**，权限判定
全部留给对端（require_admin 自然把关，比上游注入 admin 更小权限）。

两条分支（白名单，其余 404）：

- `tdx/*`          → trade 服务（实盘配置/滚动信号面：rolling-config、sltp-config、
  push-signals、rolling-signals、config、bridge-restart 等一并透传——上游也是整面透传，
  这里只收窄到 tdx 前缀）。
- `admin/data-platform/{market}/*` → 回环本进程 api（数据管理台 admin 端点：
  quantdb info/config/preview/remote-meta、各市场 data-sources / sync-datasets /
  sync-jobs / cancel）。market 白名单 {quantdb,quanthk,quantus,quantfutures}。

方法/请求体/查询串原样透传；响应（含 4xx/5xx 状态码与 `{success,data}` 信封）原样
透传。转发头纪律与 A4 相同：`sanitize_forward_headers` 剥离客户端信任头 +
X-User-Id/X-Tenant-Id 按鉴权上下文重建。
"""

from __future__ import annotations

import logging
import os

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response

from backend.services.api.routers.proxy_error_mapping import map_upstream_http_error
from backend.services.api.routers.trade_proxy import TRADE_BASE_URL
from backend.services.api.user_app.middleware.auth import get_current_user
from backend.shared.trusted_headers import sanitize_forward_headers

logger = logging.getLogger(__name__)

router = APIRouter()

#: 回环本进程 api 的基址（api 与 arena 同容器同进程栈；env 可覆盖便于测试）。
SELF_BASE_URL = os.getenv("ARENA_SELF_BASE_URL", "http://127.0.0.1:8000").rstrip("/")

_ADMIN_MARKETS = {"quantdb", "quanthk", "quantus", "quantfutures"}

#: 读超时：push-signals 服务端要拉信号 + 推桥，remote 预览可达 ~2 分钟（前端
#: axios 侧 120s）；给 130s 留余量，连接 3s 保持快速失败。
_FORWARD_TIMEOUT = httpx.Timeout(130.0, connect=3.0)


def _forward_headers(request: Request, user: dict) -> dict[str, str]:
    headers = sanitize_forward_headers(request.headers.items())
    # 身份头由鉴权上下文重建（客户端自带的已被 sanitize 剥离）——与 trade_proxy 同款
    headers["X-User-Id"] = str(user.get("user_id") or "")
    headers["X-Tenant-Id"] = str(user.get("tenant_id") or "default")
    return headers


def _passthrough(resp: httpx.Response) -> Response:
    return Response(
        content=resp.content,
        status_code=resp.status_code,
        media_type=resp.headers.get("content-type"),
    )


async def _forward_trade(request: Request, upstream_path: str, user: dict) -> Response:
    """转发 trade（复用 trade_proxy 连接池；Authorization 原样带上）。"""
    from backend.services.api.routers import trade_proxy

    url = f"{TRADE_BASE_URL}{upstream_path}"
    if request.url.query:
        url = f"{url}?{request.url.query}"
    body = await request.body()
    try:
        return await trade_proxy._get_proxy_client().request(
            request.method,
            url,
            headers=_forward_headers(request, user),
            content=body,
            timeout=_FORWARD_TIMEOUT,
        )
    except Exception as exc:  # noqa: BLE001 与 trade_proxy 同一失败面（503/504）
        raise map_upstream_http_error("trade", exc) from exc


async def _forward_self(request: Request, upstream_path: str, user: dict) -> Response:
    """回环本进程 api：调用者自己的 Bearer 走完整鉴权（admin 面 require_admin 把关）。"""
    url = f"{SELF_BASE_URL}{upstream_path}"
    if request.url.query:
        url = f"{url}?{request.url.query}"
    body = await request.body()
    try:
        async with httpx.AsyncClient(
            timeout=_FORWARD_TIMEOUT, trust_env=False
        ) as client:
            return await client.request(
                request.method,
                url,
                headers=_forward_headers(request, user),
                content=body,
            )
    except Exception as exc:  # noqa: BLE001
        raise map_upstream_http_error("api", exc) from exc


@router.api_route("/quantmind/{path:path}", methods=["GET", "POST", "PUT"])
async def quantmind_passthrough(
    path: str, request: Request, user: dict = Depends(get_current_user)
):
    if path == "tdx" or path.startswith("tdx/"):
        return _passthrough(await _forward_trade(request, f"/api/v1/{path}", user))
    if path.startswith("admin/data-platform/"):
        parts = path.split("/")
        if len(parts) < 4 or parts[2] not in _ADMIN_MARKETS:
            raise HTTPException(
                status_code=404,
                detail=f"未知市场: {parts[2] if len(parts) > 2 else ''}",
            )
        return _passthrough(await _forward_self(request, f"/api/v1/{path}", user))
    raise HTTPException(status_code=404, detail=f"未知 quantmind 路径: {path}")
