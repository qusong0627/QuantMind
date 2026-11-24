#!/usr/bin/env python3
"""
Notification event pusher.
Consumes Redis Stream 'notification_events' and pushes events to
WebSocket clients subscribed to 'notification.{user_id}'.
"""

import asyncio
import logging
from typing import Any, Dict, Optional

import redis.asyncio as aioredis

from backend.shared.notification_metrics import (
    inc_counter,
    notification_ws_deliver_total,
)

from ..market_app.market_config import settings
from .manager import manager

logger = logging.getLogger(__name__)

NOTIFICATION_EVENTS_STREAM = "notification_events"


def _build_redis_client() -> aioredis.Redis:
    if settings.REDIS_USE_SENTINEL:
        from redis.asyncio.sentinel import Sentinel

        sentinel = Sentinel(
            settings.REDIS_SENTINELS,
            socket_timeout=0.5,
            password=settings.REDIS_PASSWORD or None,
        )
        return sentinel.master_for(
            settings.REDIS_MASTER_NAME,
            socket_timeout=1.0,
            password=settings.REDIS_PASSWORD or None,
            db=0,
            decode_responses=True,
        )
    return aioredis.Redis(
        host=settings.REDIS_HOST,
        port=settings.REDIS_PORT,
        password=settings.REDIS_PASSWORD or None,
        db=0,
        decode_responses=True,
        socket_connect_timeout=3,
        socket_timeout=5,
    )


class NotificationPusher:
    def __init__(self):
        self.running = False
        self._task: asyncio.Task | None = None
        self._redis: aioredis.Redis | None = None

    async def start(self) -> None:
        if self.running:
            return
        self.running = True
        self._redis = _build_redis_client()
        self._task = asyncio.create_task(self._consume_loop(), name="notification_pusher")
        logger.info("NotificationPusher started, listening: %s", NOTIFICATION_EVENTS_STREAM)

    async def stop(self) -> None:
        self.running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._redis:
            await self._redis.aclose()
        logger.info("NotificationPusher stopped")

    async def _consume_loop(self) -> None:
        last_id = "$"
        retry_delay = 1.0

        while self.running:
            try:
                results = await self._redis.xread(
                    {NOTIFICATION_EVENTS_STREAM: last_id},
                    block=2000,
                    count=50,
                )
                if results:
                    for _stream, messages in results:
                        for msg_id, data in messages:
                            last_id = msg_id
                            await self._broadcast(data)
                retry_delay = 1.0
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("NotificationPusher error, retry in %ss: %s", retry_delay, exc)
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 30)
                try:
                    if self._redis:
                        await self._redis.aclose()
                    self._redis = _build_redis_client()
                except Exception:
                    pass

    async def _broadcast(self, raw: dict[str, Any]) -> None:
        user_id = str(raw.get("user_id", "")).strip()
        if not user_id or user_id == "None":
            inc_counter(notification_ws_deliver_total, "skipped")
            return

        topic = f"notification.{user_id}"
        message = {
            "type": "notification",
            "data": {
                "id": int(raw.get("notification_id") or 0),
                "user_id": user_id,
                "tenant_id": str(raw.get("tenant_id") or "default"),
                "title": str(raw.get("title") or ""),
                "content": str(raw.get("content") or ""),
                "type": str(raw.get("type") or "system"),
                "level": str(raw.get("level") or "info"),
                "action_url": str(raw.get("action_url") or "") or None,
                "market": str(raw.get("market") or "") or None,
                "is_read": False,
                "created_at": str(raw.get("created_at") or ""),
            },
        }
        sent = await manager.publish(topic, message)
        inc_counter(notification_ws_deliver_total, "success" if sent else "skipped")
        if sent:
            logger.debug(
                "NotificationPusher -> topic=%s notification_id=%s sent=%d",
                topic,
                raw.get("notification_id"),
                sent,
            )


notification_pusher = NotificationPusher()
