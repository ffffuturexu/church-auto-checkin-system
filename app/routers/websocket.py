from __future__ import annotations

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.core.websocket_manager import WebSocketManager

router = APIRouter(tags=["websocket"])


@router.websocket("/ws/channel-a")
async def ws_channel_a(websocket: WebSocket) -> None:
    # 直接由 websocket 对象获取 app 状态
    manager: WebSocketManager = websocket.app.state.ws_manager
    await manager.connect_channel_a(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        await manager.disconnect_channel_a(websocket)
    except Exception:
        await manager.disconnect_channel_a(websocket)


@router.websocket("/ws/channel-b")
async def ws_channel_b(websocket: WebSocket) -> None:
    manager: WebSocketManager = websocket.app.state.ws_manager
    await manager.connect_channel_b(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        await manager.disconnect_channel_b(websocket)
    except Exception:
        await manager.disconnect_channel_b(websocket)