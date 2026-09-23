"""对外 API 命名空间 `/api/ext/v1` —— 外部系统/智能体的接入面。

挂载前缀是 `live_trading_gate.EXT_API`（唯一出处）。那里的默认拒绝策略要求
**每个对外端点都去 `_ALLOWED_EXT_ENDPOINTS` 登记一行**，否则实盘关闭的部署上
它一律 403。加端点时别忘了；`test_external_api_gate_coverage.py` 会盯着。

当前只有两件事：换令牌（握手）和问「你这儿有什么」。真正的数据/任务/交易面
在后续批次里加——先把手握做扎实，否则后面每个端点都要自己想办法鉴权。
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from backend.services.api.routers.external.auth import (
    DEFAULT_TTL_SECONDS,
    AuthBackendUnavailable,
    ExternalAuthError,
    ExternalPrincipal,
    _reject,
    load_active_key,
    mint_external_token,
    require_external_principal,
    touch_last_used,
)
from backend.shared.live_trading_gate import is_real_trading_enabled

logger = logging.getLogger(__name__)

API_VERSION = "v1"

router = APIRouter(tags=["External API"])

# ---------------------------------------------------------------------------
# 握手节流
# ---------------------------------------------------------------------------
#
# 握手端点是**匿名可达**的，且每次尝试要跑一次 bcrypt。没有节流的话，
# 它既是暴力破解的入口，也是一个廉价的 CPU 烧穿点。用固定窗口计数
# （INCR + EXPIRE）——与社区侧 `community/middleware/rate_limit.py` 同款手法，
# 不引入新依赖。

#: 窗口内允许的握手尝试次数，按 **(对端, 凭据)** 计。
HANDSHAKE_MAX_ATTEMPTS = 30
#: 窗口内允许的握手尝试次数，按 **对端** 计——**与凭据无关**。
#:
#: 两个桶都要，因为凭据维度是**攻击者控制的**：`access_key` 是请求体里的自由字段，
#: 每换一个随机值就是一个新桶、count=1，永不超过 30。只挂凭据维等于没限流，
#: 攻击者可以无限次触发 bcrypt（每次约 0.23s，且同步跑在事件循环上）。
#: 对端维度攻击者换不掉（`request.client.host` 是 TCP 对端）。
HANDSHAKE_MAX_ATTEMPTS_PER_PEER = 60
#: 窗口长度（秒）。给节点重连留足余量，同时把暴力破解压到无意义。
HANDSHAKE_WINDOW_SECONDS = 300

#: Redis 客户端（**模块级共享**，与 `community/middleware/rate_limit.py` 同款）。
#: 此前每个请求新建一条连接：既浪费，又让「无超时」的后果放大——挂死时连接不释放。
_redis_client: Any = None


async def _get_redis() -> Any:
    """共享的异步 Redis 客户端；不可用时返回 None（调用方 fail-open）。"""
    global _redis_client
    if _redis_client is not None:
        return _redis_client
    try:
        from redis.asyncio import Redis
    except Exception:  # pragma: no cover - 依赖缺失
        return None
    _redis_client = Redis(
        host=os.getenv("REDIS_HOST", "localhost"),
        port=int(os.getenv("REDIS_PORT", "6379")),
        password=os.getenv("REDIS_PASSWORD", "").strip() or None,
        # DB 1 = 认证（见 CLAUDE.md「Redis 库分配」）
        db=int(os.getenv("REDIS_AUTH_DB", "1")),
        decode_responses=True,
        # ⚠️ 必须显式设超时。redis-py 默认两者都是 None = 无限等。
        # Redis「TCP 可连但不响应」（网络丢包 / AOF fsync 卡顿 / 被 DROP）时，
        # 没有超时就不是「fail-open 放行」，而是**握手全部挂死**——
        # 本仓有过盘满导致 Redis 拒写的真实事故，不是理论场景。
        socket_connect_timeout=1.0,
        socket_timeout=1.0,
    )
    return _redis_client


async def _bump(redis: Any, key: str, window: int) -> int:
    """计数 +1 并保证键**一定**有 TTL，返回计数。

    `INCR` 与 `EXPIRE` 是两次往返，中间可能失败（连接被重置/超时）。此前只在
    `count == 1` 时设 TTL，一旦那次 EXPIRE 失败，这个键就**永远不过期**且计数
    一直 ≥ 阈值——该组合被永久 429，且无自愈路径，只能人工 DEL。
    这里对「已存在但没有 TTL」的键补一次 EXPIRE（TTL 返回 -1 = 无过期）。
    """
    count = await redis.incr(key)
    if count == 1:
        await redis.expire(key, window)
    elif await redis.ttl(key) < 0:
        await redis.expire(key, window)  # 自愈
    return count

#: 凭据不存在时也要跑一次 bcrypt 比对的占位哈希。
#: 不这么做的话，「不存在的 access_key」会比「存在的但 secret 错」快一个数量级，
#: 计时差就是一个可用的**枚举 oracle**——能问出哪些 access_key 是真的。
#:
#: 这是一次性随机串的 bcrypt 结果，明文已丢弃，**不是任何真实凭据**。
#: 用真实生成的哈希而不是手搓 `"$2b$12$" + "X"*53`：后者会让 passlib 报
#: padding bits 警告，并在其 2.0 下变成异常——枚举防线不能建在会随依赖升级
#: 变成 500 的东西上（`test_external_api_handshake.py` 钉住了「不警告、只返回 False」）。
_DUMMY_BCRYPT_HASH = "$2b$12$5UjgRKzhKJDAWY.oD0KKi.yWL8QWsrdauWRyBrx.YiwFhK7f2N6Bu"


def _throttle_key(request: Request, access_key: str) -> str:
    """节流桶的键：**套接字对端 IP** + 凭据指纹。

    两侧都进键，是为了同时拦住「一个 IP 试很多凭据」和「很多 IP 试同一个凭据」

    ⚠️ **故意不读 `X-Real-IP` / `X-Forwarded-For`**。第一版读过，那是个洞：
    这两个头是客户端可控的，攻击者每试一次换一个值就换一个桶，节流直接失效。
    只用 `request.client.host`（TCP 对端，不可伪造）。

    代价：若前面挂了 nginx，所有请求的 `client.host` 都是 nginx 的地址，
    桶会退化成全局共享——限流**变严**而不是变松。这是安全的失败方向：
    正常节点一小时才握一次手，30 次/5 分钟的全局额度绰绰有余。
    哪天要按真实客户端 IP 分摊，得先有一份「可信代理」配置，而不是盲信请求头。

    凭据做哈希：明文 access_key 不该落到 Redis 键名里
    （键名会出现在 MONITOR / 慢日志 / 备份里）。
    """
    client = (request.client.host if request.client else None) or "unknown"
    return f"qm:ext:auth:throttle:{client}:{_fingerprint(access_key)}"


def _peer_throttle_key(request: Request) -> str:
    """**与凭据无关**的桶，只按 TCP 对端计。

    这是真正拦得住「换着 access_key 刷」的那一层——见
    `HANDSHAKE_MAX_ATTEMPTS_PER_PEER` 的说明。
    """
    client = (request.client.host if request.client else None) or "unknown"
    return f"qm:ext:auth:throttle:peer:{client}"


def _fingerprint(access_key: str) -> str:
    """凭据指纹（日志与 Redis 键名用它，**绝不用明文**）。

    明文 access_key 是客户端可控字符串（可含换行/ANSI），进日志等于给匿名者
    一个伪造日志行、污染终端回显的通道；进 Redis 键名则会出现在 MONITOR /
    慢日志 / 备份里。
    """
    return hashlib.sha256(access_key.encode("utf-8")).hexdigest()[:16]


async def _check_throttle(request: Request, access_key: str) -> None:
    """超限抛 429。**Redis 不可用时放行**（fail-open）并告警。

    这里刻意选 fail-open：握手是外部系统接入的唯一入口，Redis 抖一下不该让
    整个对外面停摆。代价是 Redis 挂掉期间节流失效——届时 bcrypt 的计算成本
    本身就是最后一道限速。

    两层桶（**先查与凭据无关的那层**）：对端维度拦「换着 access_key 刷」，
    凭据维度拦「很多 IP 试同一个凭据」。
    """
    redis = await _get_redis()
    if redis is None:
        return

    window = HANDSHAKE_WINDOW_SECONDS + 2
    try:
        peer_count = await _bump(redis, _peer_throttle_key(request), window)
        if peer_count > HANDSHAKE_MAX_ATTEMPTS_PER_PEER:
            logger.warning(
                "[ExtAuth] 握手节流触发（对端维度）peer=%s",
                (request.client.host if request.client else "unknown"),
            )
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="too_many_attempts",
                headers={"Retry-After": str(HANDSHAKE_WINDOW_SECONDS)},
            )

        count = await _bump(redis, _throttle_key(request, access_key), window)
        if count > HANDSHAKE_MAX_ATTEMPTS:
            logger.warning("[ExtAuth] 握手节流触发（凭据维度）ak_fp=%s", _fingerprint(access_key))
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="too_many_attempts",
                headers={"Retry-After": str(HANDSHAKE_WINDOW_SECONDS)},
            )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("[ExtAuth] 节流不可用，本次放行（fail-open）：%s", exc)


# ---------------------------------------------------------------------------
# 握手
# ---------------------------------------------------------------------------


class SessionRequest(BaseModel):
    access_key: str = Field(..., min_length=8, max_length=128, description="访问凭据 ID")
    secret_key: str = Field(..., min_length=8, max_length=256, description="访问凭据密钥")


class SessionResponse(BaseModel):
    token: str
    #: `token_type` 是 RFC 6749（OAuth2）里的字段名，值是 `Bearer`。
    #: 原名叫 `token_prefix`——名字自造，机器客户端的实现者按标准名去找会找不到。
    token_type: str = Field("Bearer", description="Authorization 的方案名（RFC 6749）")
    expires_at: int = Field(..., description="Unix 秒。到期前请续期，不要等 401 才换。")
    ttl_seconds: int
    renew_after: int = Field(..., description="建议在此刻之后续期（约为 TTL 的 2/3）")
    user_id: str
    tenant_id: str
    permissions: list[str]


@router.post("/auth/session", response_model=SessionResponse)
async def create_session(payload: SessionRequest, request: Request) -> SessionResponse:
    """用长期访问凭据换一枚短期会话令牌。

    失败的两种情形（凭据不存在 / 密钥不匹配）**返回完全相同的 401**，
    且耗时也被拉平（见 `_DUMMY_BCRYPT_HASH`）。
    """
    await _check_throttle(request, payload.access_key)

    from backend.services.api.user_app.services.api_key_service import pwd_context

    # 查库与「存在/启用/未过期」判定都走 auth 的同一个接缝。此前这里自己写了一遍
    # 查询与判定——那是全仓**第 3 份**，而第 4 份（trade/qmt_agent）就漂成了
    # aware-vs-naive 的 500。`on_backend_error="raise"` 保住握手特有的 503 语义。
    try:
        key = await load_active_key(payload.access_key, on_backend_error="raise")
    except AuthBackendUnavailable as exc:
        logger.error("[ExtAuth] 握手查库失败：%s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="auth_backend_unavailable"
        ) from exc

    # 与 ApiKeyService.verify_secret 同一个 pwd_context——不是另起一套哈希口径。
    # ⚠️ key 为 None 时也必须跑一次 bcrypt（占位哈希，见 `_DUMMY_BCRYPT_HASH`）：
    # 「凭据不存在」与「密钥错」的耗时必须一致，否则计时差就是枚举 oracle。
    stored_hash = key.secret_hash if key is not None else _DUMMY_BCRYPT_HASH
    # ⚠️ 走线程池：bcrypt cost-12 是约 0.23s 的**同步** CPU 活。直接在 async 函数里
    # 调用会占住整个事件循环（API 服务 workers=1），一次握手就把全站卡 0.23s——
    # 攻击者匿名刷这个端点等于一个廉价的拒绝服务。
    secret_ok = await run_in_threadpool(pwd_context.verify, payload.secret_key, stored_hash)

    # 失败一律同一个 401（同 detail、同耗时、同响应头）：区分开就是枚举 oracle。
    # 走 auth._reject 而不是就地造 HTTPException——`WWW-Authenticate` 头与日志
    # 口径只应有一处定义，此前这里漏了那个头，与 `require_external_principal`
    # 的 401 形状不一致（外部节点按 RFC 6750 读那个头来判定「该刷新令牌了」）。
    if key is None or not secret_ok:
        # 日志只记指纹：access_key 是匿名请求体里的自由字符串（可含换行/ANSI），
        # 明文进日志等于给攻击者一个伪造日志行、污染终端回显的通道。
        raise _reject(f"握手失败（命中={key is not None}）", payload.access_key)

    try:
        token, expires_at = mint_external_token(payload.access_key)
    except ExternalAuthError as exc:
        # 密钥未配置——这是**部署问题**，不是调用方的问题，所以给 503 而不是 401。
        # 给 401 会让运维去查客户端凭据，方向反了。
        logger.error("[ExtAuth] %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="external_api_not_configured",
        ) from exc

    # last_used_at 在这里更新（每次会话一次），不在每个请求上——见 auth.load_active_key
    await touch_last_used(payload.access_key)

    return SessionResponse(
        token=token,
        token_type="Bearer",
        expires_at=expires_at,
        ttl_seconds=DEFAULT_TTL_SECONDS,
        renew_after=int(time.time()) + (DEFAULT_TTL_SECONDS * 2 // 3),
        user_id=str(key.user_id),
        tenant_id=str(key.tenant_id),
        permissions=list(key.permissions or []),
    )


# ---------------------------------------------------------------------------
# 能力查询
# ---------------------------------------------------------------------------


class PlaneInfo(BaseModel):
    """对外「面」的自我描述。外部节点按 `plane` 认路，按 `available` 决定要不要试。"""

    plane: str
    description: str
    transport: str
    available: bool = Field(..., description="False = 本部署/本版本还没通，别去试")


class PrincipalInfo(BaseModel):
    access_key: str
    user_id: str
    tenant_id: str
    permissions: list[str]
    session_expires_at: int = Field(..., description="会话令牌到期（Unix 秒）")


class TradingInfo(BaseModel):
    real_trading_enabled: bool
    note: str


class CapabilitiesResponse(BaseModel):
    """对外能力的**契约**。

    ⚠️ 这个模型不只是文档：它是外部节点（以及将来包一层的 MCP server）生成客户端
    的依据。此前这里返回裸 `dict[str, Any]`，OpenAPI 里是一团 `additionalProperties`
    ——字段改名不会有任何提示，外部节点在运行时才发现。对外接口的 schema 就是
    它的说明书，必须是有类型的。
    """

    api_version: str
    server_time: float = Field(..., description="服务器 Unix 秒。用于对齐时钟/判断新鲜度")
    principal: PrincipalInfo
    trading: TradingInfo
    planes: list[PlaneInfo]


@router.get("/capabilities", response_model=CapabilitiesResponse)
async def get_capabilities(
    principal: ExternalPrincipal = Depends(require_external_principal),
) -> CapabilitiesResponse:
    """这个部署对外提供什么。

    **先问再做**是这套接口的设计前提：实盘开关是部署级配置，外部节点猜不到。
    与其让它去试 `/orders` 然后收一个 403，不如在这儿明说。
    """
    real_trading = is_real_trading_enabled()

    # 这里列的是**规划中的面**，状态如实反映当前实现进度——不要让外部节点
    # 以为某条路已经通了。`available=False` 的项在后续批次里逐条翻成 True。
    return CapabilitiesResponse(
        api_version=API_VERSION,
        server_time=time.time(),
        principal=PrincipalInfo(
            access_key=principal.access_key,
            user_id=principal.user_id,
            tenant_id=principal.tenant_id,
            permissions=list(principal.permissions),
            session_expires_at=principal.session_expires_at,
        ),
        trading=TradingInfo(
            # 与闸门同源：这里为 false 时，交易面端点一律 403（detail=real_trading_disabled）。
            real_trading_enabled=real_trading,
            note=(
                "本部署未启用实盘（ENABLE_REAL_TRADING=false），交易面一律 403。"
                if not real_trading
                else "实盘已启用。"
            ),
        ),
        planes=[
            PlaneInfo(
                plane="control",
                description="策略/模型/账户/能力",
                transport="json-rest",
                available=False,
            ),
            PlaneInfo(
                plane="task",
                description="训练、因子演化、回测、数据同步、TradingAgents 分析",
                transport="202 + task_id + SSE",
                available=False,
            ),
            PlaneInfo(
                plane="data",
                description="QuantDB、特征快照、推理结果、RSS",
                transport="cursor-incremental + parquet-over-http",
                available=False,
            ),
            PlaneInfo(
                plane="stream",
                description="实时行情、情报总线、信号",
                transport="websocket",
                available=False,
            ),
            PlaneInfo(
                plane="trading",
                description="订单/持仓/风控",
                transport="rest + idempotency-key",
                available=False,
            ),
        ],
    )


__all__ = ["router", "API_VERSION", "HANDSHAKE_MAX_ATTEMPTS", "HANDSHAKE_WINDOW_SECONDS"]
