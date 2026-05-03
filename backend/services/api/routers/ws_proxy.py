"""WebSocket proxy router - forward WebSocket connections to stream service.

This module provides WebSocket proxy functionality to forward connections
from the API gateway (port 8000) to the stream service (port 8003).
"""

import os
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState
import asyncio
import websockets

router = APIRouter()

STREAM_SERVICE_URL = os.getenv("STREAM_SERVICE_URL", "http://127.0.0.1:8003")


def _get_stream_ws_url(path: str, query_params: dict) -> str:
    """构建 stream 服务的 WebSocket URL."""
    ws_url = STREAM_SERVICE_URL.replace("http://", "ws://").replace("https://", "wss://")
    full_url = f"{ws_url}{path}"
    if query_params:
        query_str = "&".join(f"{k}={v}" for k, v in query_params.items())
        full_url = f"{full_url}?{query_str}"
    return full_url


async def _proxy_websocket(websocket: WebSocket, target_path: str):
    """代理 WebSocket 连接到 stream 服务."""
    await websocket.accept()

    # 构建目标 URL
    query_params = dict(websocket.query_params)
    target_url = _get_stream_ws_url(target_path, query_params)

    try:
        async with websockets.connect(target_url) as upstream:
            # 双向转发
            async def forward_to_upstream():
                try:
                    while websocket.client_state == WebSocketState.CONNECTED:
                        # 接收客户端消息
                        message = await websocket.receive()
                        if "text" in message:
                            await upstream.send(message["text"])
                        elif "bytes" in message:
                            await upstream.send(message["bytes"])
                        elif message.get("type") == "websocket.disconnect":
                            break
                except WebSocketDisconnect:
                    pass
                except Exception:
                    pass

            async def forward_to_client():
                try:
                    while True:
                        # 接收上游消息
                        message = await upstream.recv()
                        if isinstance(message, bytes):
                            await websocket.send_bytes(message)
                        else:
                            await websocket.send_text(message)
                except Exception:
                    pass

            # 并行执行双向转发
            done, pending = await asyncio.wait(
                [
                    asyncio.create_task(forward_to_upstream()),
                    asyncio.create_task(forward_to_client()),
                ],
                return_when=asyncio.FIRST_COMPLETED,
            )
            # 取消未完成的任务
            for task in pending:
                task.cancel()

    except Exception as e:
        if websocket.client_state == WebSocketState.CONNECTED:
            await websocket.close(code=1011, reason=str(e))


@router.websocket("/ws")
async def websocket_proxy_root(websocket: WebSocket):
    """代理 /ws 到 stream 服务."""
    await _proxy_websocket(websocket, "/ws")


@router.websocket("/ws/bridge")
async def websocket_proxy_bridge(websocket: WebSocket):
    """代理 /ws/bridge 到 stream 服务."""
    await _proxy_websocket(websocket, "/ws/bridge")


@router.websocket("/api/v1/ws/market")
async def websocket_proxy_market(websocket: WebSocket):
    """代理 /api/v1/ws/market 到 stream 服务."""
    await _proxy_websocket(websocket, "/api/v1/ws/market")
