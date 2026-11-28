"""SDK 密钥 → 对外短期 token 的认证/续期。

MCP 客户端在 Streamable HTTP 头里带 `X-QM-Access-Key` / `X-QM-Secret-Key`；
本模块负责换 token、缓存、按 `renew_after` 续期。Secret 只存在内存，不进日志。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from .config import settings

logger = logging.getLogger(__name__)


class AuthError(Exception):
    def __init__(self, code: str, message: str, *, status_code: int = 401, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.retryable = retryable


def access_key_fp(access_key: str) -> str:
    return hashlib.sha256((access_key or "").encode("utf-8")).hexdigest()[:16]


def _secret_fp(secret: str) -> str:
    return hashlib.sha256((secret or "").encode("utf-8")).hexdigest()[:16]


@dataclass
class ExternalSession:
    access_key: str
    secret_key: str
    token: str
    expires_at: float
    renew_after: float
    user_id: str
    tenant_id: str
    permissions: list[str]
    config_cache: dict[str, Any] | None = None
    config_ts: float = 0.0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def access_key_fp(self) -> str:
        return access_key_fp(self.access_key)

    @property
    def secret_fp(self) -> str:
        return _secret_fp(self.secret_key)

    def needs_refresh(self) -> bool:
        return time.time() >= (self.renew_after - 30)


class AuthManager:
    def __init__(self) -> None:
        self._cache: dict[str, ExternalSession] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._stdio_session: ExternalSession | None = None

    @staticmethod
    def cache_key(access_key: str, secret_key: str) -> str:
        raw = f"{access_key}\x00{secret_key}".encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    async def _exchange(self, access_key: str, secret_key: str) -> ExternalSession:
        url = f"{settings.api_base}/api/ext/v1/auth/session"
        try:
            async with httpx.AsyncClient(timeout=settings.request_timeout) as client:
                resp = await client.post(
                    url, json={"access_key": access_key, "secret_key": secret_key}
                )
        except Exception as exc:  # noqa: BLE001 - 网络失败按可重试 503
            logger.warning("[mcp-auth] token exchange network error: %s", type(exc).__name__)
            raise AuthError(
                "auth_backend_unavailable",
                "无法连接 QuantMind API",
                status_code=503,
                retryable=True,
            ) from exc

        if resp.status_code == 401:
            raise AuthError("auth_failed", "凭据无效", status_code=401)
        if resp.status_code == 403:
            raise AuthError("external_api_disabled", "本部署未开放对外 API", status_code=403)
        if resp.status_code >= 500:
            raise AuthError(
                "auth_backend_unavailable",
                "认证后端暂不可用",
                status_code=503,
                retryable=True,
            )
        if resp.status_code != 200:
            raise AuthError("auth_failed", "认证失败", status_code=401)
        data = resp.json()
        token = str(data.get("token") or "")
        if not token:
            raise AuthError("auth_failed", "认证响应缺少 token", status_code=401)
        now = time.time()
        return ExternalSession(
            access_key=access_key,
            secret_key=secret_key,
            token=token,
            expires_at=float(data.get("expires_at") or now + 3600),
            renew_after=float(data.get("renew_after") or now + 2400),
            user_id=str(data.get("user_id") or ""),
            tenant_id=str(data.get("tenant_id") or "default"),
            permissions=[str(p) for p in (data.get("permissions") or [])],
        )

    async def session_for(self, access_key: str, secret_key: str) -> ExternalSession:
        if not access_key or not secret_key:
            raise AuthError("auth_required", "缺少 X-QM-Access-Key / X-QM-Secret-Key")
        cache_key = self.cache_key(access_key, secret_key)
        cached = self._cache.get(cache_key)
        if cached and not cached.needs_refresh():
            return cached

        lock = self._locks.setdefault(cache_key, asyncio.Lock())
        async with lock:
            cached = self._cache.get(cache_key)
            if cached and not cached.needs_refresh():
                return cached
            session = await self._exchange(access_key, secret_key)
            self._cache[cache_key] = session
            return session

    async def refresh(self, session: ExternalSession) -> ExternalSession:
        cache_key = self.cache_key(session.access_key, session.secret_key)
        lock = self._locks.setdefault(cache_key, asyncio.Lock())
        async with lock:
            fresh = await self._exchange(session.access_key, session.secret_key)
            # 原地更新：工具持有的是同一个 session 引用，后续请求必须用新 token
            session.token = fresh.token
            session.expires_at = fresh.expires_at
            session.renew_after = fresh.renew_after
            session.user_id = fresh.user_id
            session.tenant_id = fresh.tenant_id
            session.permissions = fresh.permissions
            session.config_cache = None
            session.config_ts = 0.0
            self._cache[cache_key] = session
            return session

    async def stdio_session(self) -> ExternalSession:
        if not settings.stdio_access_key or not settings.stdio_secret_key:
            raise AuthError(
                "auth_required",
                "stdio 模式需要设置 QM_ACCESS_KEY / QM_SECRET_KEY",
            )
        if self._stdio_session is None or self._stdio_session.needs_refresh():
            self._stdio_session = await self.session_for(
                settings.stdio_access_key, settings.stdio_secret_key
            )
        return self._stdio_session
