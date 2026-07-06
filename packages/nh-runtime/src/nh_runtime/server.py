"""NaturalHarmony base-field server."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable, Dict, List, Set

import websockets
from websockets.server import WebSocketServerProtocol

from nh_core import HarmonicField, RendererCapabilities
from nh_model import ModelState
from nh_runtime.transport import TransportMessage

logger = logging.getLogger(__name__)


class BaseFieldServer:
    """Emits a base harmonic field to connected clients and accepts control/sensor events."""

    def __init__(self, base_field: HarmonicField = None, host: str = "127.0.0.1", port: int = 8765,
                 update_hz: float = 10.0):
        self.base_field = base_field or HarmonicField(f1=65.0)
        self.host = host
        self.port = port
        self.update_hz = update_hz
        self.clients: Set[WebSocketServerProtocol] = set()
        self._stop_event = asyncio.Event()
        self._server = None

    async def register(self, websocket: WebSocketServerProtocol):
        self.clients.add(websocket)
        try:
            await self._send_capabilities(websocket)
            await self._broadcast_field()
            await self._handle_client(websocket)
        finally:
            self.clients.discard(websocket)

    async def _send_capabilities(self, websocket: WebSocketServerProtocol):
        caps = RendererCapabilities(max_partials=32, supports_phase=True, supports_spatial=True)
        msg = TransportMessage("renderer_capabilities", caps.to_dict())
        await websocket.send(msg.to_json())

    async def _broadcast_field(self):
        if not self.clients:
            return
        msg = TransportMessage("base_field", self.base_field.to_dict())
        payload = msg.to_json()
        disconnected = []
        for client in self.clients:
            try:
                await client.send(payload)
            except Exception:
                disconnected.append(client)
        for client in disconnected:
            self.clients.discard(client)

    async def _handle_client(self, websocket: WebSocketServerProtocol):
        async for message in websocket:
            try:
                data = json.loads(message)
                msg = TransportMessage.from_dict(data)
                await self._handle_message(websocket, msg)
            except Exception as e:
                logger.warning("client message error: %s", e)

    async def _handle_message(self, websocket: WebSocketServerProtocol, msg: TransportMessage):
        if msg.type == "control_event":
            logger.debug("control_event: %s", msg.payload)
        elif msg.type == "sensor_event":
            logger.debug("sensor_event: %s", msg.payload)

    async def _broadcast_loop(self):
        while not self._stop_event.is_set():
            await self._broadcast_field()
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=1.0 / self.update_hz)
            except asyncio.TimeoutError:
                pass

    async def start(self):
        self._stop_event.clear()
        self._server = await websockets.serve(self.register, self.host, self.port)
        asyncio.create_task(self._broadcast_loop())
        logger.info("BaseFieldServer listening on ws://%s:%d", self.host, self.port)

    async def stop(self):
        self._stop_event.set()
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    def update_base_field(self, field: HarmonicField):
        self.base_field = field
