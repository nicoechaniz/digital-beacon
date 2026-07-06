"""NaturalHarmony local model client."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable, Dict, Optional

import websockets
from websockets.client import WebSocketClientProtocol


websockets

from nh_core import HarmonicField
from nh_model import ModelState
from nh_renderers import PythonSounddeviceRenderer
from nh_runtime.transport import TransportMessage

logger = logging.getLogger(__name__)


class LocalModelClient:
    """Connects to a BaseFieldServer, maintains a local ModelState, and renders audio."""

    def __init__(self, uri: str = "ws://127.0.0.1:8765", renderer: PythonSounddeviceRenderer = None,
                 on_field: Callable[[HarmonicField], None] = None):
        self.uri = uri
        self.renderer = renderer or PythonSounddeviceRenderer(sr=48000, block_size=512)
        self.on_field = on_field
        self.model = ModelState()
        self.websocket: Optional[WebSocketClientProtocol] = None
        self._running = False

    async def start(self):
        self.websocket = await websockets.connect(self.uri)
        self._running = True
        self.renderer.start()
        asyncio.create_task(self._receive_loop())
        asyncio.create_task(self._render_loop())
        logger.info("LocalModelClient connected to %s", self.uri)

    async def stop(self):
        self._running = False
        self.renderer.stop()
        if self.websocket:
            await self.websocket.close()
            self.websocket = None

    async def send_control(self, event: Dict[str, Any]):
        msg = TransportMessage("control_event", event)
        await self.websocket.send(msg.to_json())

    async def send_sensor(self, event: Dict[str, Any]):
        msg = TransportMessage("sensor_event", event)
        await self.websocket.send(msg.to_json())

    async def _receive_loop(self):
        try:
            async for message in self.websocket:
                try:
                    data = json.loads(message)
                    msg = TransportMessage.from_dict(data)
                    if msg.type == "base_field":
                        field = HarmonicField.from_dict(msg.payload)
                        self.model.update_from_base_field(field)
                        if self.on_field:
                            self.on_field(field)
                    elif msg.type == "renderer_capabilities":
                        logger.debug("server capabilities: %s", msg.payload)
                except Exception as e:
                    logger.warning("server message error: %s", e)
        except Exception as e:
            logger.warning("receive loop ended: %s", e)

    async def _render_loop(self):
        while self._running:
            snapshot = self.model.to_snapshot()
            self.renderer.render(snapshot)
            await asyncio.sleep(0.01)
