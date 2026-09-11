#!/usr/bin/env python3
"""
Strategy monitor event pusher.

Consumes Redis Stream 'strategy_events' and pushes events to
WebSocket clients subscribed to 'strategy.{user_id}'.

背景：仪表盘「策略监控」在 WebSocket 连上后会关掉轮询（useStrategies.ts 中
realtimeStatus === 'connected' 时不注册 refreshOrchestrator），只依赖 WS 推送；
而后端此前没有任何 producer 往 `strategy.*` 主题发消息，导致卡片挂载后永不刷新。
本推送器负责把上游（trade 服务 strategy_monitor_pusher）写入的事件转成 WS 消息。
"""

import asyncio
import logging
import os
import time
from typing import Any

import redis.asyncio as aioredis

from ..market_app.market_config import settings
from .manager import manager

logger = logging.getLogger(__name__)

STRATEGY_EVENTS_STREAM = "strategy_events"

STRATEGY_TOPIC_PREFIX = "strategy."


def _build_redis_client() -> aioredis.Redis:
    # 与 trade_pusher 同源：事件流写在同一 Redis 实例的 db0
    host = os.getenv("STRATEGY_EVENTS_REDIS_HOST", settings.REDIS_HOST)
    port = int(os.getenv("STRATEGY_EVENTS_REDIS_PORT", settings.REDIS_PORT))
    password = os.getenv("STRATEGY_EVENTS_REDIS_PASSWORD", settings.REDIS_PASSWORD)
    # 必须与写事件流的一方同库：producer（trade 服务）用的是 trade_shared 的
    # RedisClient，其库位由 REDIS_DB_TRADE（默认 2）决定；而本模块 settings 是
    # stream 的行情配置（db3）。两边错库会静默收不到任何事件。
    db = int(
        os.getenv("STRATEGY_EVENTS_REDIS_DB", os.getenv("REDIS_DB_TRADE", "2"))
    )

    if settings.REDIS_USE_SENTINEL:
        from redis.asyncio.sentinel import Sentinel

        sentinel = Sentinel(
            settings.REDIS_SENTINELS,
            socket_timeout=0.5,
            password=password or None,
        )
        return sentinel.master_for(
            settings.REDIS_MASTER_NAME,
            socket_timeout=1.0,
            password=password or None,
            db=db,
        )
    return aioredis.Redis(
        host=host,
        port=port,
        password=password or None,
        db=db,
        decode_responses=True,
        socket_connect_timeout=3,
        socket_timeout=5,
    )


class StrategyPusher:
    """Strategy monitor event pusher via Redis Stream -> WebSocket."""

    def __init__(self):
        self.running = False
        self._task: asyncio.Task | None = None
        self._redis: aioredis.Redis | None = None

    async def start(self) -> None:
        if self.running:
            return
        self.running = True
        self._redis = _build_redis_client()
        self._task = asyncio.create_task(self._consume_loop(), name="strategy_pusher")
        logger.info("StrategyPusher started, listening: %s", STRATEGY_EVENTS_STREAM)

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
        logger.info("StrategyPusher stopped")

    async def _consume_loop(self) -> None:
        last_id = "$"
        retry_delay = 1.0

        while self.running:
            try:
                results = await self._redis.xread(
                    {STRATEGY_EVENTS_STREAM: last_id},
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
                logger.warning("StrategyPusher error, retry in %ss: %s", retry_delay, exc)
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
            return

        topic = f"{STRATEGY_TOPIC_PREFIX}{user_id}"
        message = {
            "type": "strategy_update",
            "timestamp": time.time(),
            "data": dict(raw),
        }
        sent = await manager.publish(topic, message)
        if sent:
            logger.debug(
                "StrategyPusher -> topic=%s total_asset=%s sent=%d",
                topic,
                raw.get("total_asset"),
                sent,
            )


strategy_pusher = StrategyPusher()
