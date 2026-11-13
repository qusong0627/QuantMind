"""arena 数据面 · 族 6 路由层：桥与券商读取面（6 GET + 3 写面）。

上游 `api_server.py` L727–860（`/api/qmt/*`、`/api/tdx/*`、`/api/broker-config/{broker}`、
`/api/real-trading/status`）。两类实现：

- `tdx/config`、`tdx/overview`、`real-trading/status`、`broker-config/{broker}`：
  转发 QM trade 原生端点（api 服务 trade_proxy 同范式：透传用户 Bearer +
  过滤信任头 + 注入 X-User-Id/X-Tenant-Id）。上游曾是 baymax 进程内直读桥，
  迁移后数据源换成 QM trade 的同形实现；`broker-config` 做 qmt→qmt_exec 名映射
  （见服务层 `backend/services/agent_arena/bridge_family.py` 注头）。
- `qmt/account`、`qmt/status`：进程内直连 `QmtExecClient`（不经过 trade）。

错误语义照抄上游：

- `qmt/*` 永不以 HTTP 错误回应桥失败——200 + `{success:false,error}`（前端据此
  把面板降级为提示，见 client.ts `fetchQmtAccount`/`fetchQmtStatus`）；
- tdx/broker/real-trading 转发失败走 trade_proxy 同款映射（503/504/透传上游码）。

写面（A5 收口，TradingSettings 保存按钮）：POST `/tdx/config`（桥地址/token）、
PUT `/broker-config/{broker}`（保存 values）、POST `/broker-config/{broker}/test`
（连通性测试）。请求体原样透传，响应状态码透传（trade 侧 require_admin / 校验
错误原样可见）。
"""

from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response

from backend.services.api.routers.proxy_error_mapping import map_upstream_http_error
from backend.services.api.routers.trade_proxy import TRADE_BASE_URL
from backend.services.api.user_app.middleware.auth import get_current_user
from backend.shared.trusted_headers import sanitize_forward_headers

logger = logging.getLogger(__name__)

router = APIRouter()


def _bf():
    from backend.services.agent_arena import bridge_family as bf

    return bf


async def _forward_get(
    request: Request, upstream_path: str, user: dict
) -> httpx.Response:
    """GET 转发到 trade（复用 trade_proxy 的连接池与信任头纪律）。"""
    return await _forward_any(request, upstream_path, user)


async def _forward_any(
    request: Request, upstream_path: str, user: dict
) -> httpx.Response:
    """按请求方法转发到 trade（GET 走全局 10s 超时；写面 60s——broker test 要打桥）。"""
    from backend.services.api.routers import trade_proxy

    url = f"{TRADE_BASE_URL}{upstream_path}"
    if request.url.query:
        url = f"{url}?{request.url.query}"
    headers = sanitize_forward_headers(request.headers.items())
    # 身份头由鉴权上下文重建（客户端自带的已被 sanitize 剥离）——与 trade_proxy 同款
    headers["X-User-Id"] = str(user.get("user_id") or "")
    headers["X-Tenant-Id"] = str(user.get("tenant_id") or "default")
    kwargs: dict = {"headers": headers}
    if request.method != "GET":
        kwargs["content"] = await request.body()
        kwargs["timeout"] = httpx.Timeout(60.0, connect=3.0)
    try:
        return await trade_proxy._get_proxy_client().request(
            request.method, url, **kwargs
        )
    except Exception as exc:  # noqa: BLE001 与 trade_proxy 同一失败面（503/504）
        raise map_upstream_http_error("trade", exc) from exc


async def _forward_write(
    request: Request, upstream_path: str, user: dict
) -> httpx.Response:
    """写面转发（POST/PUT）到 trade；请求体原样透传。"""
    return await _forward_any(request, upstream_path, user)


def _passthrough(resp: httpx.Response) -> Response:
    return Response(
        content=resp.content,
        status_code=resp.status_code,
        media_type=resp.headers.get("content-type"),
    )


# ---------- 通达信桥 / 实盘状态 / 券商配置（转发 trade，裸 JSON 无信封） ----------


@router.get("/tdx/config")
async def arena_tdx_config(request: Request, user: dict = Depends(get_current_user)):
    """桥配置状态（不返回 token 明文）+ 桥健康检查。前端 getJson 直取裸 JSON。"""
    return _passthrough(await _forward_get(request, "/api/v1/tdx/config", user))


@router.get("/tdx/overview")
async def arena_tdx_overview(request: Request, user: dict = Depends(get_current_user)):
    """桥总览：stats + 账户/持仓 + 当日委托（桥不可达时 available=false）。"""
    return _passthrough(await _forward_get(request, "/api/v1/tdx/overview", user))


@router.get("/real-trading/status")
async def arena_real_trading_status(
    request: Request, user: dict = Depends(get_current_user)
):
    """实盘执行状态（trade 生命周期端点，形状含 status/mode/strategy/execution_config）。"""
    return _passthrough(
        await _forward_get(request, "/api/v1/real-trading/status", user)
    )


@router.get("/broker-config/{broker}")
async def arena_broker_config(
    broker: str, request: Request, user: dict = Depends(get_current_user)
):
    """券商接入配置（敏感字段脱敏为 *_configured）。qmt→trade 的 qmt_exec。"""
    bf = _bf()
    native = bf.map_broker(broker)
    if native is None:
        raise HTTPException(status_code=404, detail=f"未知券商: {broker}")
    resp = await _forward_get(request, f"/api/v1/broker-config/{native}", user)
    if resp.status_code != 200:
        return _passthrough(resp)
    try:
        body = resp.json()
    except ValueError:  # noqa: BLE001 非 JSON 响应原样透传
        return _passthrough(resp)
    if isinstance(body, dict) and native != broker:
        # 响应里的 broker 名换回请求名（前端拿它当 key），label 换回上游文案
        label = bf.BROKER_LABEL_OVERRIDES.get(broker) or body.get("label")
        body = {**body, "broker": broker, "label": label}
    return body


# ---------- 迅投 QMT（进程内直连执行端；上游失败=200+success:false） ----------


@router.get("/qmt/account")
async def arena_qmt_account(_current_user: dict = Depends(get_current_user)):
    """QMT 账户资产+持仓（只读）。未启用/桥不通 → success:false，面板降级。"""
    try:
        data = await _bf().qmt_account()
    except Exception as exc:  # noqa: BLE001 上游原话：降级返回错误，不炸接口
        logger.warning("arena qmt account 查询失败: %s", exc)
        return {"success": False, "error": f"QMT 查询失败: {exc}"}
    return {"success": True, "data": data}


@router.get("/qmt/status")
async def arena_qmt_status(_current_user: dict = Depends(get_current_user)):
    """QMT 桥自述（只读 ping）：两处下单总闸、RPC 版本、账号类型。失败同上。"""
    try:
        data = await _bf().qmt_status()
    except Exception as exc:  # noqa: BLE001
        logger.warning("arena qmt status 查询失败: %s", exc)
        return {"success": False, "error": f"QMT 状态查询失败: {exc}"}
    return {"success": True, "data": data}


# ---------- 写面（A5 收口，TradingSettings 保存按钮） ----------


@router.post("/tdx/config")
async def arena_tdx_config_save(
    request: Request, user: dict = Depends(get_current_user)
):
    """保存桥地址/token（trade 侧不回显明文）。前端 postJson 只看 200，裸 JSON。"""
    return _passthrough(await _forward_write(request, "/api/v1/tdx/config", user))


@router.put("/broker-config/{broker}")
async def arena_broker_config_save(
    broker: str, request: Request, user: dict = Depends(get_current_user)
):
    """保存券商接入配置（body {values}）。qmt→trade 的 qmt_exec；未知券商 404 自产。"""
    bf = _bf()
    native = bf.map_broker(broker)
    if native is None:
        raise HTTPException(status_code=404, detail=f"未知券商: {broker}")
    return _passthrough(
        await _forward_write(request, f"/api/v1/broker-config/{native}", user)
    )


@router.post("/broker-config/{broker}/test")
async def arena_broker_config_test(
    broker: str, request: Request, user: dict = Depends(get_current_user)
):
    """券商配置连通性测试（trade 侧实打）。未知券商 404 自产。"""
    bf = _bf()
    native = bf.map_broker(broker)
    if native is None:
        raise HTTPException(status_code=404, detail=f"未知券商: {broker}")
    return _passthrough(
        await _forward_write(request, f"/api/v1/broker-config/{native}/test", user)
    )
