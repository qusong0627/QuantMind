"""SSE 长连接的会话复查守卫（A02 遗留：各 SSE 流建立后不再看会话状态）。

WS 侧有周期复核与 ``auth:invalidate`` 断开，但流式响应一旦开始就再没复查过会话：
被撤销或被禁用的令牌可以让 AI IDE 日志、Lab 进度、QuantBot 对话等一条流无限跑下去。
本模块把每条活动流纳入登记表，并按 ``revalidate_seconds()``（硬上限 60 秒）复查。

依赖不可用按「未知」处理并保留连接（与 WS 周期复核同口径），避免 Redis/PG 抖动
触发全量断流。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from backend.shared.logging_config import get_logger
from backend.shared.session_validation import (
    AuthRejected,
    ensure_session_live,
    revalidate_seconds,
)

logger = get_logger(__name__)


def identity_from(value: Any) -> tuple[str, str, str] | None:
    """取 ``(tenant_id, user_id, jti)``；任一缺失即 None。

    内部服务委托路径没有 ``jti``（网关只透传 ``X-User-Id``/``X-Tenant-Id``），其撤销
    判定归签发方那一跳；这里返回 None 让调用方原样透传，不伪称已经复查过。
    """
    if not isinstance(value, dict):
        return None
    tenant_id = str(value.get("tenant_id") or "").strip()
    user_id = str(value.get("user_id") or "").strip()
    jti = str(value.get("jti") or "").strip()
    if not (tenant_id and user_id and jti):
        return None
    return tenant_id, user_id, jti


def identity_from_request(request: Any) -> dict[str, Any] | None:
    """端点侧的统一取法：认证中间件写入的 ``request.state.user``。"""
    return getattr(getattr(request, "state", None), "user", None)


class _Stream:
    __slots__ = ("tenant_id", "user_id", "jti", "expired", "reader")

    def __init__(self, tenant_id: str, user_id: str, jti: str) -> None:
        self.tenant_id = tenant_id
        self.user_id = user_id
        self.jti = jti
        self.expired = False
        self.reader: asyncio.Future | None = None

    def stop(self) -> None:
        self.expired = True
        if self.reader is not None and not self.reader.done():
            self.reader.cancel()


_active: set[_Stream] = set()


async def _watch(stream: _Stream, interval: int | None = None) -> None:
    interval = revalidate_seconds() if interval is None else interval
    while True:
        await asyncio.sleep(interval)
        try:
            await ensure_session_live(stream.tenant_id, stream.user_id, stream.jti)
        except AuthRejected as exc:
            logger.info(
                "SSE 会话复查拒绝，结束流: tenant=%s user=%s reason=%s",
                stream.tenant_id,
                stream.user_id,
                exc.reason,
            )
            stream.stop()
            return
        except Exception as exc:  # noqa: BLE001 - 依赖不可用视为未知，保留连接
            logger.warning("SSE 会话复查依赖异常，保留连接: %s", type(exc).__name__)


async def guard_sse(identity: Any, body: AsyncIterator[str]) -> AsyncIterator[str]:
    """包装 SSE 迭代器：会话失效时在下一个检查点结束流，不再产出内容。"""
    triple = identity_from(identity)
    if triple is None:
        async for chunk in body:
            yield chunk
        return

    tenant_id, user_id, jti = triple
    stream = _Stream(tenant_id, user_id, jti)
    _active.add(stream)
    watchdog = asyncio.ensure_future(_watch(stream))
    reader = body.__aiter__()
    try:
        while True:
            # 检查点必须在取下一块**之前**：一条迟迟不产出的流（IDE 日志、Lab 进度
            # 就是这样）如果只在拿到块后再判，失效就要空等到下一个块出现为止。
            if stream.expired:
                return
            pending = asyncio.ensure_future(reader.__anext__())
            stream.reader = pending
            try:
                chunk = await pending
            except StopAsyncIteration:
                return
            except asyncio.CancelledError:
                if stream.expired:
                    return
                raise
            finally:
                stream.reader = None
            if stream.expired:
                return
            yield chunk
    finally:
        _active.discard(stream)
        watchdog.cancel()
        await asyncio.gather(watchdog, return_exceptions=True)


def close_matching(
    *, tenant_id: str | None = None, user_id: str | None = None, jti: str | None = None
) -> int:
    """结束本进程内命中身份的活动流；供 ``auth:invalidate`` 订阅者做事件驱动收口。"""
    closed = 0
    for stream in list(_active):
        if tenant_id and stream.tenant_id != tenant_id:
            continue
        if user_id and stream.user_id != user_id:
            continue
        if jti and stream.jti != jti:
            continue
        stream.stop()
        closed += 1
    return closed


def active_count() -> int:
    return len(_active)
