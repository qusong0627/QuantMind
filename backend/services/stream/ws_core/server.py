#!/usr/bin/env python3
"""
WebSocket服务器
Week 20 Day 3
"""

import asyncio
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from backend.shared.auth import auth_manager, decode_jwt_token
from backend.shared.cors import resolve_cors_origins

from .manager import manager
from .notification_pusher import notification_pusher
from .quote_pusher import quote_pusher
from .strategy_pusher import strategy_pusher
from .trade_pusher import trade_pusher
from .ws_config import ws_config

logger = logging.getLogger(__name__)


async def _extract_ws_auth_metadata(websocket: WebSocket) -> dict[str, Any]:
    """解析并校验 WS 鉴权信息，返回连接元数据。"""
    headers = websocket.headers
    params = websocket.query_params

    tenant_id = str(headers.get("x-tenant-id") or params.get("tenant_id") or "").strip()
    user_id = str(headers.get("x-user-id") or params.get("user_id") or "").strip()

    auth_header = str(headers.get("authorization") or "").strip()
    token = str(params.get("token") or "").strip()
    if auth_header.lower().startswith("bearer ") and not token:
        token = auth_header[7:].strip()

    payload: dict[str, Any] = {}
    if token:
        try:
            payload = auth_manager.verify_token(token)
        except Exception:
            payload = decode_jwt_token(token)
        tenant_id = tenant_id or str(payload.get("tenant_id") or "").strip()
        user_id = user_id or str(payload.get("sub") or payload.get("user_id") or "").strip()

    if not tenant_id:
        tenant_id = "default"

    authenticated = bool(user_id and token)
    return {
        "tenant_id": tenant_id,
        "user_id": user_id or "anonymous",
        "authenticated": authenticated,
        "auth_source": "jwt" if token else "anonymous",
        "connected_at": time.time(),
    }


async def handle_message(connection_id: str, message: dict):
    """
    处理客户端消息

    Args:
        connection_id: 连接ID
        message: 消息内容
    """
    msg_type = message.get("type")
    action = message.get("action")
    # 兼容旧版 /api/v1/ws/market 协议（action + symbols）
    if not msg_type and action:
        msg_type = action

    if msg_type == "ping":
        # 心跳消息
        await manager.update_heartbeat(connection_id)
        await manager.send_message(
            connection_id,
            {"type": "pong", "timestamp": time.time()},
            use_queue=False,
        )

    elif msg_type == "subscribe":
        # 订阅主题
        if action:
            symbols = message.get("symbols", []) or []
            topics = [f"stock.{str(symbol).strip()}" for symbol in symbols if str(symbol).strip()]
            for topic in topics:
                await manager.subscribe(connection_id, topic)
                if topic.startswith("stock."):
                    await quote_pusher.subscribe_quote(topic.split("stock.", 1)[1])
            await manager.send_message(connection_id, {"type": "subscribed", "symbols": symbols})
        else:
            topic = message.get("topic")
            if topic:
                metadata = manager.connection_metadata.get(connection_id, {})
                if topic.startswith("notification."):
                    expected_topic = f"notification.{metadata.get('user_id')}"
                    if not metadata.get("authenticated") or topic != expected_topic:
                        await manager.send_message(
                            connection_id,
                            {
                                "type": "error",
                                "error_code": "SUBSCRIPTION_FORBIDDEN",
                                "error_message": "Forbidden notification subscription",
                            },
                            use_queue=False,
                        )
                        return
                await manager.subscribe(connection_id, topic)
                if topic.startswith("stock."):
                    await quote_pusher.subscribe_quote(topic.split("stock.", 1)[1])
                elif topic.startswith("trade.updates."):
                    pass  # trade_pusher handles broadcasting; no extra per-client setup needed
                elif topic.startswith("strategy."):
                    pass  # strategy_pusher handles broadcasting
                elif topic.startswith("notification."):
                    pass  # notification_pusher handles broadcasting
                await manager.send_message(connection_id, {"type": "subscribed", "topic": topic})

    elif msg_type == "unsubscribe":
        # 取消订阅
        if action:
            symbols = message.get("symbols", []) or []
            topics = [f"stock.{str(symbol).strip()}" for symbol in symbols if str(symbol).strip()]
            for topic in topics:
                await manager.unsubscribe(connection_id, topic)
                if topic.startswith("stock.") and topic not in manager.subscriptions:
                    await quote_pusher.unsubscribe_quote(topic.split("stock.", 1)[1])
            await manager.send_message(connection_id, {"type": "unsubscribed", "symbols": symbols})
        else:
            topic = message.get("topic")
            if topic:
                await manager.unsubscribe(connection_id, topic)
                if topic.startswith("stock.") and topic not in manager.subscriptions:
                    await quote_pusher.unsubscribe_quote(topic.split("stock.", 1)[1])
                await manager.send_message(connection_id, {"type": "unsubscribed", "topic": topic})

    else:
        logger.warning(f"未知消息类型: {msg_type}")


class WebSocketServer:
    """WebSocket服务器"""

    def __init__(self):
        """初始化服务器"""
        self.running = False
        self.heartbeat_task: asyncio.Task | None = None
        logger.info("WebSocket服务器初始化")

    async def start(self):
        """启动服务器"""
        self.running = True

        # 启动心跳检测任务
        self.heartbeat_task = asyncio.create_task(self._heartbeat_checker())

        # 启动消息队列处理器
        await manager.start_queue_processor()
        # 启动行情推送器（驱动 stock.* 主题）
        await quote_pusher.start()
        # 启动交易事件推送器（驱动 trade.updates.* 主题）
        await trade_pusher.start()
        # 启动策略监控推送器（驱动 strategy.* 主题）
        await strategy_pusher.start()
        # 启动通知事件推送器（驱动 notification.* 主题）
        await notification_pusher.start()

    async def stop(self):
        """停止服务器"""
        self.running = False

        # 停止消息队列处理器
        await manager.stop_queue_processor()
        # 停止行情推送器
        await quote_pusher.stop()
        # 停止交易事件推送器
        await trade_pusher.stop()
        # 停止策略监控推送器
        await strategy_pusher.stop()
        # 停止通知事件推送器
        await notification_pusher.stop()

        # 取消心跳检测任务
        if self.heartbeat_task:
            self.heartbeat_task.cancel()
            try:
                await self.heartbeat_task
            except asyncio.CancelledError:
                pass

        # 断开所有连接
        await manager.disconnect_all()

        logger.info("WebSocket服务器已停止")

    async def _heartbeat_checker(self):
        """心跳检测"""
        while self.running:
            try:
                await manager.check_connections()
                await asyncio.sleep(ws_config.heartbeat_interval)
            except Exception as e:
                logger.error(f"心跳检测错误: {e}")
                await asyncio.sleep(1)


# 全局服务器实例
server = WebSocketServer()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期事件"""
    # 启动事件
    await server.start()
    logger.info("WebSocket server started")
    yield
    # 关闭事件
    await server.stop()
    logger.info("WebSocket server stopped")


# 创建FastAPI应用
app = FastAPI(title="QuantMind WebSocket Server", lifespan=lifespan)

# 添加CORS中间件
app.add_middleware(
    CORSMiddleware,
    allow_origins=resolve_cors_origins(logger=logger),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """
    WebSocket端点

    客户端可以连接到 ws://host:port/ws
    """
    # 生成连接ID
    connection_id = str(uuid.uuid4())

    try:
        try:
            metadata = await _extract_ws_auth_metadata(websocket)
        except Exception as exc:
            logger.warning("WebSocket握手鉴权失败: %s", exc)
            await websocket.close(code=1008, reason="Authentication failed")
            return

        if ws_config.auth_required and not metadata.get("authenticated"):
            logger.warning("WebSocket握手鉴权失败: missing/invalid auth")
            await websocket.close(code=1008, reason="Authentication required")
            return

        # 建立连接
        success = await manager.connect(websocket, connection_id, metadata=metadata)

        if not success:
            return


        # 发送欢迎消息
        await manager.send_message(
            connection_id,
            {
                "type": "welcome",
                "connection_id": connection_id,
                "message": "连接成功",
                "tenant_id": metadata.get("tenant_id"),
                "user_id": metadata.get("user_id"),
            },
        )

        # 消息循环
        while True:
            try:
                # 接收消息
                data = await websocket.receive_text()
                message = json.loads(data)

                # 处理消息
                await handle_message(connection_id, message)

            except WebSocketDisconnect:
                logger.info(f"客户端主动断开: {connection_id}")
                break

            except json.JSONDecodeError:
                logger.error(f"JSON解析错误: {connection_id}")
                await manager.send_message(
                    connection_id,
                    {"type": "error", "message": "JSON格式错误"},
                )

            except Exception as e:
                logger.error(f"接收消息错误 {connection_id}: {e}")
                break

    finally:
        # 断开连接
        await manager.disconnect(connection_id)
        # 连接断开后重算行情订阅，防止单连接取消误伤全局订阅
        await quote_pusher.reconcile_subscriptions(manager.subscriptions.keys())


@app.get("/")
async def root():
    """根路径"""
    return {
        "service": "QuantMind WebSocket Server",
        "version": "1.0.0",
        "status": "running" if server.running else "stopped",
        "stats": manager.get_stats(),
    }


@app.get("/health")
async def health_check():
    """健康检查"""
    return {
        "status": "healthy",
        "connections": len(manager.active_connections),
        "running": server.running,
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host=ws_config.host,
        port=ws_config.port,
        log_level=ws_config.log_level.lower(),
    )
