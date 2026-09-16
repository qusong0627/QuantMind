"""情报总线的 WS 消费端（T-P6-11）：intel:events Stream → topic 广播。

- 消费组 ``intel:ws``（与生产者 SDK 同源常量；组内单消费者=本进程）；
- 每事件路由到 ``intel.{tenant}.market.{MARKET}``（v1 市场级广播）；
- 计数（Redis INCR ``intel:consumed:{type}`` best-effort）供"被消费可观测"验收；
- 畸形事件：计数 + **ack 跳过**（防毒丸卡组）；路由失败不 ack（下轮重读重投）。
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

import redis.asyncio as aioredis

from backend.shared.intel_events import (
    CONSUMER_GROUP,
    STREAM_KEY,
    read_events,
    topic_for_event,
)
from backend.services.stream.ws_core.manager import manager

logger = logging.getLogger(__name__)

CONSUMED_COUNTER_PREFIX = "intel:consumed"


def _build_redis_client() -> aioredis.Redis:
    return aioredis.Redis(
        host=os.getenv("REDIS_HOST") or "redis",
        port=int(os.getenv("REDIS_PORT", "6379")),
        password=os.getenv("REDIS_PASSWORD") or None,
        db=int(os.getenv("REDIS_DB", "0")),
        decode_responses=True,
        socket_connect_timeout=3,
        socket_timeout=5,
    )


class IntelPusher:
    """总线 → WS 的常驻消费循环（stream 服务 lifespan 启动）。"""

    def __init__(self, *, tenant: str = "default", consumer: str = "ws-1") -> None:
        self.tenant = tenant
        self.consumer = consumer
        self._redis: aioredis.Redis | None = None
        self.running = False
        self._task: asyncio.Task | None = None
        self.counters: dict[str, Any] = {
            "read": 0, "broadcast": 0, "malformed": 0, "errors": 0, "last_error": None,
        }

    async def start(self) -> None:
        if self.running:
            return
        self.running = True
        self._task = asyncio.get_running_loop().create_task(
            self._consume_loop(), name="intel-pusher"
        )
        logger.info("IntelPusher started (group=%s)", CONSUMER_GROUP)

    async def stop(self) -> None:
        self.running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        if self._redis is not None:
            try:
                await self._redis.aclose()
            except Exception:  # noqa: BLE001
                pass

    async def _ensure_ready(self) -> None:
        if self._redis is None:
            self._redis = _build_redis_client()
        try:
            await self._redis.xgroup_create(STREAM_KEY, CONSUMER_GROUP, id="0", mkstream=True)
        except Exception as exc:  # noqa: BLE001
            if "BUSYGROUP" not in str(exc):
                raise

    async def _consume_loop(self) -> None:
        retry_delay = 1.0
        while self.running:
            try:
                await self._ensure_ready()
                assert self._redis is not None
                raw = await self._redis.xreadgroup(
                    CONSUMER_GROUP, self.consumer, {STREAM_KEY: ">"}, count=200, block=2000
                )
                for _stream, messages in (raw or []):
                    for msg_id, fields in messages:
                        await self._handle_one(msg_id, fields)
                retry_delay = 1.0
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                self.counters["errors"] += 1
                self.counters["last_error"] = f"{type(exc).__name__}: {exc}"
                logger.warning("IntelPusher error, retry in %ss: %s", retry_delay, exc)
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 30)
                try:
                    if self._redis is not None:
                        await self._redis.aclose()
                except Exception:  # noqa: BLE001
                    pass
                self._redis = None

    async def _handle_one(self, msg_id: str, fields: dict[str, Any]) -> None:
        from backend.shared.intel_events import IntelEventError, decode_event

        assert self._redis is not None
        self.counters["read"] += 1
        try:
            event = decode_event(fields.get("data") or "{}")
        except IntelEventError as exc:
            # 毒丸：计数 + ack 跳过（否则卡组）；绝不静默——日志如实
            self.counters["malformed"] += 1
            logger.warning("intel 畸形事件跳过 id=%s: %s", msg_id, exc)
            await self._redis.xack(STREAM_KEY, CONSUMER_GROUP, msg_id)
            return
        topic = topic_for_event(event, tenant=self.tenant)
        sent = await manager.publish(
            topic,
            {"type": "intel_event", "topic": topic, "event": event},
        )
        self.counters["broadcast"] += 1 if sent else 0
        try:
            await self._redis.incr(f"{CONSUMED_COUNTER_PREFIX}:{event['type']}")
            await self._redis.expire(f"{CONSUMED_COUNTER_PREFIX}:{event['type']}", 86400)
        except Exception:  # noqa: BLE001 - 计数 best-effort
            pass
        await self._redis.xack(STREAM_KEY, CONSUMER_GROUP, msg_id)

    def status(self) -> dict[str, Any]:
        return {"running": self.running, "group": CONSUMER_GROUP, **dict(self.counters)}


_pusher: IntelPusher | None = None


def get_intel_pusher() -> IntelPusher:
    global _pusher
    if _pusher is None:
        _pusher = IntelPusher()
    return _pusher


intel_pusher = get_intel_pusher()  # 模块级单例（与 notification_pusher 同模式）
