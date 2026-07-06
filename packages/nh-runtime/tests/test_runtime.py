import asyncio
import pytest
import websockets

from nh_core import HarmonicField, Partial
from nh_runtime import BaseFieldServer, LocalModelClient, TransportMessage
from nh_renderers import PythonSounddeviceRenderer


@pytest.mark.asyncio
async def test_server_emits_base_field():
    field = HarmonicField(f1=65.0)
    field.partials[1] = Partial(n=1, gain=1.0)
    server = BaseFieldServer(base_field=field, host="127.0.0.1", port=18765, update_hz=20.0)
    await server.start()
    try:
        uri = "ws://127.0.0.1:18765"
        ws = await websockets.connect(uri)
        try:
            msg = await asyncio.wait_for(ws.recv(), timeout=2.0)
            data = TransportMessage.from_json(msg)
            assert data.type == "renderer_capabilities"
            msg = await asyncio.wait_for(ws.recv(), timeout=2.0)
            data = TransportMessage.from_json(msg)
            assert data.type == "base_field"
            assert data.payload["f1"] == 65.0
        finally:
            await ws.close()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_client_updates_model():
    field = HarmonicField(f1=80.0)
    field.partials[1] = Partial(n=1, gain=1.0)
    server = BaseFieldServer(base_field=field, host="127.0.0.1", port=18766, update_hz=20.0)
    await server.start()
    try:
        renderer = PythonSounddeviceRenderer(sr=16000, block_size=256)
        client = LocalModelClient(uri="ws://127.0.0.1:18766", renderer=renderer)
        await client.start()
        await asyncio.sleep(0.2)
        assert client.model.base_field.f1 == 80.0
        await client.stop()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_server_relays_control_event_to_other_clients():
    server = BaseFieldServer(host="127.0.0.1", port=18767, update_hz=20.0)
    await server.start()
    try:
        ws1 = await websockets.connect("ws://127.0.0.1:18767")
        ws2 = await websockets.connect("ws://127.0.0.1:18767")
        try:
            await ws1.recv()  # capabilities
            await ws1.recv()  # base_field
            await ws2.recv()  # capabilities
            await ws2.recv()  # base_field
            await ws1.send(TransportMessage("control_event", {"type": "pad_on", "value": {"n": 5}}).to_json())
            msg = await asyncio.wait_for(ws2.recv(), timeout=2.0)
            data = TransportMessage.from_json(msg)
            assert data.type == "control_event"
            assert data.payload["type"] == "pad_on"
            assert data.payload["value"]["n"] == 5
        finally:
            await ws1.close()
            await ws2.close()
    finally:
        await server.stop()
