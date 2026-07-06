"""NaturalHarmony base-field server."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, Optional, Set

import websockets
from websockets.server import WebSocketServerProtocol

from nh_core import HarmonicField, RendererCapabilities
from nh_model import ModelState
from nh_runtime.transport import TransportMessage

logger = logging.getLogger(__name__)


class BaseFieldServer:
    """Emits a modulated harmonic field to connected clients and accepts control/sensor events.

    The server maintains a single `ModelState` as the source of truth. Clients send
    `control_event` and `sensor_event` messages; the server applies them to the model and
    broadcasts the resulting snapshot as `base_field`.
    """

    def __init__(
        self,
        base_field: HarmonicField = None,
        host: str = "127.0.0.1",
        port: int = 8765,
        update_hz: float = 10.0,
        renderer_capabilities: RendererCapabilities = None,
        sensor_mapping: Optional[Dict[str, Any]] = None,
    ):
        self.base_field = base_field or HarmonicField(f1=65.0)
        self.host = host
        self.port = port
        self.update_hz = update_hz
        self.renderer_capabilities = renderer_capabilities or RendererCapabilities(
            max_partials=32, supports_phase=True, supports_spatial=True
        )
        self.sensor_mapping = sensor_mapping or {}
        self.sensor_influence: float = 1.0
        self.sensor_sources_enabled: Dict[str, bool] = {}
        self.model = ModelState()
        self.model.update_from_base_field(self.base_field)
        self.clients: Set[WebSocketServerProtocol] = set()
        self._stop_event = asyncio.Event()
        self._server = None

    async def register(self, websocket: WebSocketServerProtocol):
        self.clients.add(websocket)
        try:
            await self._send_capabilities(websocket)
            await self._send_field(websocket)
            await self._handle_client(websocket)
        finally:
            self.clients.discard(websocket)

    async def _send_capabilities(self, websocket: WebSocketServerProtocol):
        msg = TransportMessage("renderer_capabilities", self.renderer_capabilities.to_dict())
        await websocket.send(msg.to_json())

    async def _send_field(self, websocket: WebSocketServerProtocol):
        snapshot = self.model.to_snapshot()
        msg = TransportMessage("base_field", snapshot.to_dict())
        await websocket.send(msg.to_json())

    async def _broadcast_field(self):
        if not self.clients:
            return
        snapshot = self.model.to_snapshot()
        msg = TransportMessage("base_field", snapshot.to_dict())
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
                await self._broadcast_field()
            except Exception as e:
                logger.warning("client message error: %s", e)
                try:
                    err = TransportMessage("error", {"code": "parse_error", "message": str(e)})
                    await websocket.send(err.to_json())
                except Exception:
                    pass

    def _apply_sensor_safety(self, event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Apply global influence and per-source kill switch to a sensor event.

        Returns None if the event should be hard-killed.
        """
        if self.sensor_influence <= 0.0:
            return None
        source = event.get("source", "unknown")
        if source in self.sensor_sources_enabled and not self.sensor_sources_enabled[source]:
            return None
        if self.sensor_influence >= 1.0:
            return event
        scaled = dict(event)
        value = scaled.get("value")
        if isinstance(value, (int, float)):
            scaled["value"] = value * self.sensor_influence
        elif isinstance(value, dict):
            scaled["value"] = {k: (v * self.sensor_influence if isinstance(v, (int, float)) else v) for k, v in value.items()}
        return scaled

    async def _handle_message(self, websocket: WebSocketServerProtocol, msg: TransportMessage):
        if msg.type == "control_event":
            etype = msg.payload.get("type")
            if etype == "sensor_influence":
                self.sensor_influence = max(0.0, min(1.0, float(msg.payload.get("value", 1.0))))
                return
            if etype == "sensor_source_enable":
                cfg = msg.payload.get("value", {})
                self.sensor_sources_enabled[cfg.get("source", "unknown")] = bool(cfg.get("enabled", True))
                return
            self.model.apply_control(msg.payload)
            await self._relay_control_event(websocket, msg)
        elif msg.type == "sensor_event":
            safe_event = self._apply_sensor_safety(msg.payload)
            if safe_event is not None:
                self.model.apply_sensor(safe_event, self.sensor_mapping)
        elif msg.type == "ping":
            await websocket.send_text(TransportMessage("pong", {}).to_json())
        elif msg.type == "pong":
            pass

    async def _relay_control_event(self, sender: WebSocketServerProtocol, msg: TransportMessage):
        """Relay non-state control events (e.g. pad_on) to other clients for UI mirror."""
        payload = msg.to_json()
        disconnected = []
        for client in self.clients:
            if client is sender:
                continue
            try:
                await client.send(payload)
            except Exception:
                disconnected.append(client)
        for client in disconnected:
            self.clients.discard(client)

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
        self.model.update_from_base_field(field)
