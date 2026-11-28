"""`/api/ext/v1` 客户端：统一鉴权、续期、错误映射、配置缓存、审计上报。"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from .auth import AuthError, AuthManager, ExternalSession
from .config import settings

logger = logging.getLogger(__name__)


class ApiError(Exception):
    def __init__(self, code: str, message: str, *, status_code: int = 502, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.retryable = retryable


class ApiClient:
    def __init__(self, auth: AuthManager):
        self._auth = auth

    async def _request(
        self,
        session: ExternalSession,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        timeout: float | None = None,
        retry_auth: bool = True,
    ) -> Any:
        url = f"{settings.api_base}/api/ext/v1{path}"
        headers = {"Authorization": f"Bearer {session.token}"}
        try:
            async with httpx.AsyncClient(timeout=timeout or settings.request_timeout) as client:
                resp = await client.request(
                    method, url, params=params, json=json, headers=headers
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[mcp-api] %s %s network error: %s", method, path, type(exc).__name__)
            raise ApiError(
                "upstream_unavailable", "QuantMind API 不可达", retryable=True
            ) from exc

        if resp.status_code == 401 and retry_auth:
            fresh = await self._auth.refresh(session)
            return await self._request(
                fresh,
                method,
                path,
                params=params,
                json=json,
                timeout=timeout,
                retry_auth=False,
            )
        if resp.status_code == 403:
            detail = _detail(resp)
            raise ApiError("permission_denied", detail or "权限不足", status_code=403)
        if resp.status_code == 404:
            raise ApiError("not_found", _detail(resp) or "资源不存在", status_code=404)
        if resp.status_code >= 500:
            raise ApiError(
                "upstream_unavailable", _detail(resp) or "上游服务暂不可用", retryable=True
            )
        if resp.status_code >= 400:
            raise ApiError(
                "invalid_request",
                _detail(resp) or f"请求失败（HTTP {resp.status_code}）",
                status_code=resp.status_code,
            )
        if not resp.content:
            return None
        content_type = resp.headers.get("content-type", "")
        if "application/json" in content_type:
            return resp.json()
        return resp.text

    async def config(self, session: ExternalSession) -> dict[str, Any]:
        now = time.time()
        if session.config_cache is not None and now - session.config_ts < settings.config_ttl_seconds:
            return session.config_cache
        data = await self._request(session, "GET", "/mcp/config")
        if not isinstance(data, dict):
            raise ApiError("upstream_contract_changed", "配置响应格式异常")
        session.config_cache = data
        session.config_ts = now
        return data

    async def get(self, session: ExternalSession, path: str, **kwargs: Any) -> Any:
        return await self._request(session, "GET", path, **kwargs)

    async def post(self, session: ExternalSession, path: str, **kwargs: Any) -> Any:
        return await self._request(session, "POST", path, **kwargs)

    async def post_audit(self, session: ExternalSession, events: list[dict[str, Any]]) -> None:
        if not events:
            return
        try:
            await self._request(
                session,
                "POST",
                "/mcp/audit",
                json={"events": events[:100]},
                timeout=3.0,
            )
        except Exception as exc:  # noqa: BLE001 - 审计失败不阻断工具结果
            logger.warning("[mcp-audit] 上报失败：%s", type(exc).__name__)


def _detail(resp: httpx.Response) -> str:
    try:
        data = resp.json()
    except ValueError:
        return (resp.text or "").strip()[:300]
    if isinstance(data, dict):
        detail = data.get("detail")
        if isinstance(detail, str):
            return detail[:300]
        if isinstance(detail, dict):
            return str(detail.get("message") or detail.get("code") or "")[:300]
    return ""
