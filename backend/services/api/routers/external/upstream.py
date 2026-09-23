"""对外面调用**内部服务**（engine / trade）的唯一客户端。

为什么需要这一层，而不是让各面自己 `httpx.get()`
------------------------------------------------
因为「谁的身份去调上游」这件事只有一个正确答案，写第二遍就会写错：

* 调用方手里是**对外会话令牌**（`qmx1.…`）。上游 engine/trade 不认它——
  `trade_shared.deps.get_auth_context` 走的是 `decode_jwt_token`，看到
  `qmx1.` 直接 401。所以必须先把它换成上游认的东西。
* 换法只有一条：api 服务**以该凭据绑定的用户身份**签一枚用户 JWT 转发过去。
  这就是「代理」，不是「提权」：令牌的 `sub` 完全来自**已验签**的
  `ExternalPrincipal`，外部调用方碰不到 claims。

⚠️ **本模块只返回结构化结果，绝不把上游响应原样回吐。** 见 `fetch_json`。

## 一个必须写下来的决定：默认不给 admin

**默认**签出来的委托令牌 `roles` 恒为 `["user"]`，不带 admin。即使这枚对外凭据
挂在管理员名下（现网两枚都是 `user_id=10000001`）也一样。

理由是权限面必须比人窄。现网 `10000001` 在 trade 侧能过 `require_admin`，
而对外凭据的授权模型是 `api_keys.permissions` 里那几个字符串——让一枚
`["trade.read"]` 的凭据顺带拿到 trade 的**管理面**通行证，等于权限模型被绕过。

`is_admin=False` 不影响模拟盘：`require_sim_user_id` 认的是 **sub 的值**
（`10000001` 属管理员族 → 收口到模拟账户 10000001，与人工 UI 同一个账户），
不是 JWT 里的 admin 标志。这两件事常被混为一谈，`test_external_api_trading.py`
把「进得了模拟盘、进不了 trade 控制面」钉成一对断言。

### 例外：`admin=True` 是**调用点**的属性，不是凭据的属性

任务面里「触发市场数据同步」这一个动作在上游是管理员端点
（`admin/data-platform/sync-schedule/{market}/run`，路由器级 `require_admin`）。
它必须能被触发——用户要的「数据更新」就是它——所以 `fetch_json(admin=True)`
按调用点提权。

这**不**削弱上面那条决定，因为提权不落在调用方手里：

* 令牌每次调用现签，只活在这一次 `httpx` 请求里；**从不返回给调用方**，
  外部节点拿不到一枚带 admin 的令牌，也无从要求一枚（`admin` 是模块内参数，
  不是请求字段——没有一个外部可写的值能把它变成 True）。
* 泄露面因此与「不给 admin」时完全一样：能泄露的只有它自己那次调用的效果。
  一次数据同步的效果就是一次数据同步——而那正是被授权的动作。

两个服务认 admin 的 claim 形状**不一样**：api 侧
`user_app/middleware/auth.py::require_admin` 先看 `is_admin` claim、再回落 RBAC；
trade 侧 `trade_shared/deps.py::require_admin` 看 `is_admin or "admin" in roles`
且**没有** RBAC 回落。这里两个都设，理由是一枚为某次调用签出的令牌应当能被它
可能落到的任一服务认出来——而不是让调用点去猜自己会落到哪。仅此一次例外，
其余的调用点一个都不许加。

## 与 `proxy_error_mapping` / `trade_proxy` 的关系

不复用它们。那两个是**转发浏览器请求**：把原请求头带过去（剔除少数信任头），
原样回吐响应体。对外面相反——**请求头从零构造**（客户端一个头都进不来），
响应体必须解析成有类型的模型。共用会把两边的安全方向都掰弯。
"""

from __future__ import annotations

import logging
import os
from typing import Any, TypeVar

import httpx
from fastapi import HTTPException, status
from pydantic import BaseModel, ValidationError

from backend.services.api.routers.external.auth import ExternalPrincipal

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

#: 上游地址。默认值与 `trade_proxy` / `engine_proxy` 同源（OSS 单容器里
#: 四个服务同容器不同端口，`127.0.0.1` 就是对的；compose 也显式注入了
#: `TRADE_SERVICE_URL`，两处读同一个环境变量）。
_TRADE_BASE_URL = os.getenv("TRADE_SERVICE_URL", "http://127.0.0.1:8002").rstrip("/")
_ENGINE_BASE_URL = os.getenv("ENGINE_SERVICE_URL", "http://127.0.0.1:8001").rstrip("/")
#: ⚠️ 这是 **api 服务自己**。任务面里训练与市场同步的端点在 api 进程内，
#: 我们仍然走一次回环 HTTP 而不是直接 import 那个 handler 函数——
#: 见 `task.py` 模块 docstring 的「为什么走回环而不是直接调函数」。
_API_BASE_URL = os.getenv("API_SERVICE_URL", "http://127.0.0.1:8000").rstrip("/")

_SERVICE_BASE_URLS: dict[str, str] = {
    "trade": _TRADE_BASE_URL,
    "engine": _ENGINE_BASE_URL,
    "api": _API_BASE_URL,
}

#: 读接口的超时。与 `trade_proxy` 的 10s 同量级——模拟盘账户/委托是本地
#: PG + Redis，不该慢；慢就说明上游有事，早报 504 好过把 api 的事件循环
#: 挂在一堆半死的连接上。
DEFAULT_TIMEOUT_SECONDS = 10.0

#: 写接口（下单）单独放宽。`POST /simulation/orders` 要拿账户撮合锁
#: （`SimulationAccountManager.locked_execution`），持锁排队时 10s 不够，
#: 而「超时」在下单这条路上的含义是**调用方不知道单成没成**——那是要避免的
#: 状态，宁可多等。
WRITE_TIMEOUT_SECONDS = 30.0

#: 打给上游的委托令牌的 `roles`。**恒定，不接受调用方覆盖。** 见模块 docstring。
_DELEGATED_ROLES = ("user",)


def _base_url(service: str) -> str:
    url = _SERVICE_BASE_URLS.get(service)
    if url is None:  # pragma: no cover - 编程错误
        raise ValueError(f"未知的上游服务：{service}")
    return url


_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    """共享 AsyncClient。

    `trust_env=False` 与 `trade_proxy` 同款：进程环境里的 `HTTP_PROXY` 不该
    影响容器内回环调用（踩过一次：容器带了 proxy 变量，回环请求被送去代理，
    表现为「上游偶发连不上」）。
    """
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(DEFAULT_TIMEOUT_SECONDS, connect=3.0),
            trust_env=False,
            limits=httpx.Limits(max_keepalive_connections=20, max_connections=50),
        )
    return _client


def _delegated_token(principal: ExternalPrincipal, *, admin: bool = False) -> str:
    """签一枚「以该凭据绑定用户身份」的短期用户 JWT，供上游校验。

    用 `shared.auth.auth_manager`——**不是**第二份 JWT 实现。密钥解析
    （runtime.env 热换 / 未配置即 503）那套只在 `shared/auth` 里有一份。

    ⚠️ 令牌只活在这一次请求里：不返回给调用方、不落盘、不进日志。

    `admin=True` 只由模块内的管理员调用点传（见模块 docstring「例外」一节）。
    """
    from backend.shared.auth import auth_manager

    claims: dict[str, Any] = {
        "sub": principal.user_id,
        "tenant_id": principal.tenant_id,
        "roles": list(_DELEGATED_ROLES),
    }
    if admin:
        # 两个服务认的形状不同（api 认 claim、trade 认 roles），都设上。
        claims["roles"] = ["admin"]
        claims["is_admin"] = True

    return auth_manager.create_access_token(claims)


def build_headers(
    principal: ExternalPrincipal, *, admin: bool = False
) -> dict[str, str]:
    """构造打给上游的请求头。**从零构造**，不从原请求继承任何头。

    这是本模块与 `trade_proxy` 最本质的区别：那边是「转发一个我不完全信任的
    请求」（所以用 `sanitize_forward_headers` 做减法），这边是「我自己造一个
    请求」——外部客户端连一个头都不该进得来。减法需要维护剥离名单，加法不需要。
    """
    from backend.shared.auth import get_internal_call_secret

    headers = {
        "Authorization": f"Bearer {_delegated_token(principal, admin=admin)}",
        "X-User-Id": str(principal.user_id),
        "X-Tenant-Id": str(principal.tenant_id),
        "Accept": "application/json",
    }
    # X-Internal-Call 只用于容器网络内的服务间直连（C1 加固后的约定）。
    secret = get_internal_call_secret()
    if secret:
        headers["X-Internal-Call"] = secret
    return headers


async def fetch_json(
    service: str,
    method: str,
    path: str,
    *,
    principal: ExternalPrincipal,
    model: type[T] | None = None,
    list_model: type[BaseModel] | None = None,
    json_body: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    timeout: float | None = None,
    admin: bool = False,
) -> Any:
    """调用上游并（可选）把响应解析成有类型的模型。

    `model` 与 `list_model` 二选一：前者用于对象响应，后者用于数组响应
    （上游 `GET /simulation/orders` 返回的是 `list[SimOrderResponse]`）。

    上游的 4xx **原样透传**（那是调用方的问题，比如标的代码不对、余额不足），
    5xx 与连接失败**翻译成 502/504**（那是我们这边的问题）。这条分界的意义：
    外部节点的重试/告警逻辑按状态码分类，把上游 500 原样吐出去会让它以为
    自己该退避重试，而上游 500 往往重试也没用、需要人去看。

    `admin` 见模块 docstring「例外」：只有上游**确实**是管理员端点的调用点才传，
    且必须是硬编码的字面量 True，不许由请求字段派生。
    """
    url = f"{_base_url(service)}{path}"
    effective_timeout = timeout or DEFAULT_TIMEOUT_SECONDS

    try:
        response = await _get_client().request(
            method=method,
            url=url,
            headers=build_headers(principal, admin=admin),
            json=json_body,
            params=params,
            timeout=httpx.Timeout(effective_timeout, connect=3.0),
        )
    except httpx.TimeoutException as exc:
        logger.error("[ExtUpstream] %s %s 超时（%.1fs）：%s", service, path, effective_timeout, exc)
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="upstream_timeout",
        ) from exc
    except httpx.HTTPError as exc:
        logger.error("[ExtUpstream] %s %s 不可达：%s", service, path, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="upstream_unavailable",
        ) from exc

    if response.status_code >= 500:
        logger.error(
            "[ExtUpstream] %s %s 上游 5xx（%d）：%s",
            service,
            path,
            response.status_code,
            response.text[:500],
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="upstream_error",
        )

    if response.status_code >= 400:
        # 上游 4xx 透传 detail，并**保留状态码**。上面 `_client` 没开
        # raise_for_status，这里手工分派就是为了把 4xx/5xx 分开处理。
        detail = _extract_detail(response)
        raise HTTPException(status_code=response.status_code, detail=detail)

    if model is None and list_model is None:
        # 调用方只要「成了」，不关心响应体（`cancel` 之类）。
        return None

    try:
        payload = response.json()
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="upstream_malformed_response",
        ) from exc

    try:
        if list_model is not None:
            if not isinstance(payload, list):
                raise TypeError(f"期望数组，拿到 {type(payload).__name__}")
            return [list_model.model_validate(item) for item in payload]
        return model.model_validate(payload)  # type: ignore[union-attr]
    except ValidationError as exc:
        # 只记字段路径，不记整份响应体：里面可能有账户余额/持仓。
        logger.error(
            "[ExtUpstream] %s %s 响应与对外契约不符：%s",
            service,
            path,
            exc.errors()[:5],
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="upstream_contract_changed",
        ) from exc
    except TypeError as exc:
        logger.error("[ExtUpstream] %s %s 响应形状不符：%s", service, path, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="upstream_contract_changed",
        ) from exc


def _extract_detail(response: httpx.Response) -> str:
    """从上游错误体里抠一个字符串 detail。

    上游的 detail 形状不止一种（有的服务是 `detail: str`，有的把它做成
    校验错误数组）。这里取不出字符串就退化成一句通用话——**绝不**把上游
    原始 body 塞进 detail：那是把内部实现细节（表名、栈、SQL）透给外部。
    """
    try:
        body = response.json()
    except ValueError:
        return "upstream_rejected"
    if isinstance(body, dict):
        detail = body.get("detail")
        if isinstance(detail, str) and detail:
            return detail
    return "upstream_rejected"


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "WRITE_TIMEOUT_SECONDS",
    "build_headers",
    "fetch_json",
]
