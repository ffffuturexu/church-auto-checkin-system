from __future__ import annotations

import asyncio
from dataclasses import dataclass

from fastapi import WebSocket


@dataclass
class WebSocketStats:
    channel_a_connections: int
    channel_b_connections: int
    channel_queue_connections: int


class WebSocketManager:
    """Three-channel websocket manager.

    Channel A: check-in and reception events (JSON)
    Channel B: debug video frame stream (base64 JSON)
    Channel Queue: pending/unknown queue events with full payload
    """

    def __init__(self, send_timeout_sec: float = 0.6) -> None:
        self._channel_a: set[WebSocket] = set()
        self._channel_b: set[WebSocket] = set()
        self._channel_queue: set[WebSocket] = set()
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

    async def connect_channel_queue(self, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            self._channel_queue.add(websocket)

    async def disconnect_channel_a(self, websocket: WebSocket) -> None:
        async with self._lock:
            self._channel_a.discard(websocket)

    async def disconnect_channel_b(self, websocket: WebSocket) -> None:
        async with self._lock:
            self._channel_b.discard(websocket)

    async def disconnect_channel_queue(self, websocket: WebSocket) -> None:
        async with self._lock:
            self._channel_queue.discard(websocket)

    async def broadcast_channel_a(self, payload: dict) -> None:
        safe_payload = self._strip_large_image_fields(payload)
        await self._broadcast(safe_payload, channel="a")

    async def broadcast_channel_b(self, payload: dict) -> None:
        await self._broadcast(payload, channel="b")

    async def broadcast_channel_queue(self, payload: dict) -> None:
        await self._broadcast(payload, channel="queue")

    async def get_stats(self) -> WebSocketStats:
        async with self._lock:
            return WebSocketStats(
                channel_a_connections=len(self._channel_a),
                channel_b_connections=len(self._channel_b),
                channel_queue_connections=len(self._channel_queue),
            )

    async def _broadcast(self, payload: dict, channel: str) -> None:
        async with self._lock:
            if channel == "a":
                sockets = list(self._channel_a)
            elif channel == "b":
                sockets = list(self._channel_b)
            elif channel == "queue":
                sockets = list(self._channel_queue)
            else:
                sockets = []

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
            if channel == "a":
                target = self._channel_a
            elif channel == "b":
                target = self._channel_b
            elif channel == "queue":
                target = self._channel_queue
            else:
                target = set()
            for ws in dead:
                target.discard(ws)

    @staticmethod
    def _strip_large_image_fields(payload: dict) -> dict:
        if not isinstance(payload, dict):
            return payload

        safe_payload = dict(payload)
        for field_name in (
            "faceimagebase64",
            "face_image_base64",
            "imagebase64",
            "image_base64",
        ):
            safe_payload.pop(field_name, None)

        for nested_key in ("data", "recognitiondata", "recognition_data"):
            nested_value = safe_payload.get(nested_key)
            if not isinstance(nested_value, dict):
                continue
            cleaned_nested = dict(nested_value)
            for field_name in (
                "faceimagebase64",
                "face_image_base64",
                "imagebase64",
                "image_base64",
            ):
                cleaned_nested.pop(field_name, None)
            safe_payload[nested_key] = cleaned_nested

        return safe_payload
