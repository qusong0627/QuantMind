"""MCP 配额（Redis 计数；Redis 不可用时放行并告警）。"""

from __future__ import annotations

import logging
import time
from typing import Any

from .auth import ExternalSession
from .config import settings

logger = logging.getLogger(__name__)

try:
    from redis.asyncio import Redis
except Exception:  # pragma: no cover
    Redis = None  # type: ignore


class QuotaExceeded(Exception):
    def __init__(self, metric: str, limit: float):
        super().__init__(f"quota_exceeded: {metric} >= {limit}")
        self.metric = metric
        self.limit = limit


class QuotaManager:
    def __init__(self) -> None:
        self._redis: Any = None
        self._redis_ready = False

    async def _client(self) -> Any:
        if self._redis_ready:
            return self._redis
        self._redis_ready = True
        if Redis is None:
            return None
        try:
            client = Redis(
                host=settings.redis_host,
                port=settings.redis_port,
                password=settings.redis_password or None,
                db=settings.redis_db,
                decode_responses=True,
                socket_connect_timeout=1.0,
                socket_timeout=1.0,
            )
            await client.ping()
            self._redis = client
        except Exception as exc:  # noqa: BLE001
            logger.warning("[mcp-quota] Redis 不可用，配额放行：%s", type(exc).__name__)
            self._redis = None
        return self._redis

    async def check_and_incr(
        self,
        session: ExternalSession,
        metric: str,
        limit: int | float,
        *,
        window_seconds: int,
    ) -> None:
        if limit is None or float(limit) <= 0:
            return
        client = await self._client()
        if client is None:
            return
        bucket = int(time.time() // window_seconds)
        key = f"qm:mcp:quota:{session.access_key_fp}:{metric}:{bucket}"
        try:
            count = await client.incr(key)
            if count == 1:
                await client.expire(key, window_seconds + 5)
            elif await client.ttl(key) < 0:
                await client.expire(key, window_seconds + 5)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[mcp-quota] 计数失败，放行：%s", type(exc).__name__)
            return
        if count > float(limit):
            raise QuotaExceeded(metric, float(limit))

    async def close(self) -> None:
        if self._redis is not None:
            try:
                await self._redis.aclose()
            except Exception:  # noqa: BLE001
                pass
            self._redis = None
