from __future__ import annotations

import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.core.websocket_manager import WebSocketManager

router = APIRouter(tags=["websocket"])


async def _wait_for_disconnect(websocket: WebSocket) -> None:
    while True:
        try:
            await asyncio.wait_for(websocket.receive(), timeout=1.0)
        except asyncio.TimeoutError:
            continue
        except WebSocketDisconnect:
            raise


@router.websocket("/ws/channel-a")
async def ws_channel_a(websocket: WebSocket) -> None:
    # 直接由 websocket 对象获取 app 状态
    manager: WebSocketManager = websocket.app.state.ws_manager
    await manager.connect_channel_a(websocket)
    try:
        await _wait_for_disconnect(websocket)
    except WebSocketDisconnect:
        await manager.disconnect_channel_a(websocket)
    except Exception:
        await manager.disconnect_channel_a(websocket)


@router.websocket("/ws/channel-b")
async def ws_channel_b(websocket: WebSocket) -> None:
    manager: WebSocketManager = websocket.app.state.ws_manager
    await manager.connect_channel_b(websocket)
    try:
        await _wait_for_disconnect(websocket)
    except WebSocketDisconnect:
        await manager.disconnect_channel_b(websocket)
    except Exception:
        await manager.disconnect_channel_b(websocket)


@router.websocket("/ws/reception/queue")
async def ws_reception_queue(websocket: WebSocket) -> None:
    manager: WebSocketManager = websocket.app.state.ws_manager
    await manager.connect_channel_queue(websocket)
    try:
        await _wait_for_disconnect(websocket)
    except WebSocketDisconnect:
        await manager.disconnect_channel_queue(websocket)
    except Exception:
        await manager.disconnect_channel_queue(websocket)