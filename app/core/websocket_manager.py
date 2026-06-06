from __future__ import annotations

import asyncio
from dataclasses import dataclass

from fastapi import WebSocket


@dataclass
class WebSocketStats:
    channel_a_connections: int
    channel_b_connections: int


class WebSocketManager:
    """Two-channel websocket manager.

    Channel A: check-in and reception events (JSON)
    Channel B: debug video frame stream (base64 JSON)
    """

    def __init__(self, send_timeout_sec: float = 0.6) -> None:
        self._channel_a: set[WebSocket] = set()
        self._channel_b: set[WebSocket] = set()
        self._lock = asyncio.Lock()
        self._send_timeout_sec = max(0.05, float(send_timeout_sec))

    async def connect_channel_a(self, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            self._channel_a.add(websocket)

    async def connect_channel_b(self, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            self._channel_b.add(websocket)

    async def disconnect_channel_a(self, websocket: WebSocket) -> None:
        async with self._lock:
            self._channel_a.discard(websocket)

    async def disconnect_channel_b(self, websocket: WebSocket) -> None:
        async with self._lock:
            self._channel_b.discard(websocket)

    async def broadcast_channel_a(self, payload: dict) -> None:
        await self._broadcast(payload, channel="a")

    async def broadcast_channel_b(self, payload: dict) -> None:
        await self._broadcast(payload, channel="b")

    async def get_stats(self) -> WebSocketStats:
        async with self._lock:
            return WebSocketStats(
                channel_a_connections=len(self._channel_a),
                channel_b_connections=len(self._channel_b),
            )

    async def _broadcast(self, payload: dict, channel: str) -> None:
        async with self._lock:
            sockets = list(self._channel_a if channel == "a" else self._channel_b)

        if not sockets:
            return

        async def _send(ws: WebSocket) -> WebSocket | None:
            try:
                await asyncio.wait_for(ws.send_json(payload), timeout=self._send_timeout_sec)
                return None
            except Exception:
                return ws

        results = await asyncio.gather(*(_send(ws) for ws in sockets))
        dead = [ws for ws in results if ws is not None]

        if not dead:
            return

        async with self._lock:
            target = self._channel_a if channel == "a" else self._channel_b
            for ws in dead:
                target.discard(ws)
