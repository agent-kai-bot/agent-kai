"""WebSocket endpoint for streaming market data."""

import asyncio
import json
from datetime import datetime

from fastapi import WebSocket, WebSocketDisconnect


class ConnectionManager:
    """Manages WebSocket connections grouped by subscription."""

    def __init__(self):
        self._connections: dict[str, list[WebSocket]] = {}  # key -> [ws]
        self._lock = asyncio.Lock()

    def _key(self, symbol: str, interval: str) -> str:
        return f"{symbol.upper()}.{interval}"

    async def connect(self, ws: WebSocket, symbol: str, interval: str):
        await ws.accept()
        key = self._key(symbol, interval)
        async with self._lock:
            self._connections.setdefault(key, []).append(ws)

    async def disconnect(self, ws: WebSocket, symbol: str, interval: str):
        key = self._key(symbol, interval)
        async with self._lock:
            conns = self._connections.get(key, [])
            if ws in conns:
                conns.remove(ws)

    async def broadcast(self, symbol: str, interval: str, data: dict):
        """Send data to all subscribers of a symbol+interval."""
        key = self._key(symbol, interval)
        dead = []
        for ws in self._connections.get(key, []):
            try:
                await ws.send_json(data)
            except Exception:
                dead.append(ws)
        if dead:
            async with self._lock:
                conns = self._connections.get(key, [])
                for ws in dead:
                    if ws in conns:
                        conns.remove(ws)

    async def broadcast_price(self, symbol: str, data: dict):
        """Send price update to all connections watching any timeframe for this symbol."""
        async with self._lock:
            keys = [k for k in self._connections if k.startswith(f"{symbol.upper()}.")]
        for key in keys:
            for ws in self._connections.get(key, []):
                try:
                    await ws.send_json(data)
                except Exception:
                    pass


ws_manager = ConnectionManager()


async def ws_endpoint(websocket: WebSocket, symbol: str, interval: str = "1m"):
    """WebSocket endpoint: /ws/{symbol}/{interval}"""
    await ws_manager.connect(websocket, symbol, interval)
    try:
        while True:
            # Keep connection alive, handle client messages
            data = await websocket.receive_text()
            try:
                msg = json.loads(data)
                if msg.get("action") == "ping":
                    await websocket.send_json({"action": "pong"})
            except json.JSONDecodeError:
                pass
    except WebSocketDisconnect:
        pass
    finally:
        await ws_manager.disconnect(websocket, symbol, interval)
