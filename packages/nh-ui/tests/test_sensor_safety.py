
import pytest
import websockets
import asyncio
from nh_runtime import BaseFieldServer, TransportMessage
from nh_core import HarmonicField, RendererCapabilities


@pytest.mark.asyncio
async def test_sensor_influence_zero_is_hard_kill():
    server = BaseFieldServer(
        base_field=HarmonicField(f1=65.0),
        renderer_capabilities=RendererCapabilities(max_partials=32, supports_phase=True, supports_spatial=True),
        sensor_mapping={"muse_focus": {"param": "master_gain", "scale": 1.0, "offset": 0.0}},
        host="127.0.0.1", port=18801, update_hz=20.0
    )
    await server.start()
    try:
        ws = await websockets.connect("ws://127.0.0.1:18801")
        try:
            await ws.recv()  # caps
            await ws.recv()  # field
            await ws.send(TransportMessage("control_event", {"type": "master", "value": 1.0}).to_json())
            await ws.send(TransportMessage("control_event", {"type": "sensor_influence", "value": 0.0}).to_json())
            await ws.send(TransportMessage("sensor_event", {"source": "muse", "type": "muse_focus", "value": 0.75}).to_json())
            await asyncio.sleep(0.1)
            assert server.model.master_gain == 1.0
        finally:
            await ws.close()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_sensor_influence_scaled():
    server = BaseFieldServer(
        base_field=HarmonicField(f1=65.0),
        renderer_capabilities=RendererCapabilities(max_partials=32, supports_phase=True, supports_spatial=True),
        sensor_mapping={"muse_focus": {"param": "master_gain", "scale": 1.0, "offset": 0.0}},
        host="127.0.0.1", port=18802, update_hz=20.0
    )
    await server.start()
    try:
        ws = await websockets.connect("ws://127.0.0.1:18802")
        try:
            await ws.recv()  # caps
            await ws.recv()  # field
            await ws.send(TransportMessage("control_event", {"type": "sensor_influence", "value": 0.5}).to_json())
            await ws.send(TransportMessage("sensor_event", {"source": "muse", "type": "muse_focus", "value": 0.8}).to_json())
            await asyncio.sleep(0.1)
            assert server.model.master_gain == 0.4
        finally:
            await ws.close()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_sensor_source_disable():
    server = BaseFieldServer(
        base_field=HarmonicField(f1=65.0),
        renderer_capabilities=RendererCapabilities(max_partials=32, supports_phase=True, supports_spatial=True),
        sensor_mapping={"muse_focus": {"param": "master_gain", "scale": 1.0, "offset": 0.0}},
        host="127.0.0.1", port=18803, update_hz=20.0
    )
    await server.start()
    try:
        ws = await websockets.connect("ws://127.0.0.1:18803")
        try:
            await ws.recv()  # caps
            await ws.recv()  # field
            await ws.send(TransportMessage("control_event", {"type": "master", "value": 1.0}).to_json())
            await ws.send(TransportMessage("control_event", {"type": "sensor_source_enable", "value": {"source": "muse", "enabled": False}}).to_json())
            await ws.send(TransportMessage("sensor_event", {"source": "muse", "type": "muse_focus", "value": 0.75}).to_json())
            await asyncio.sleep(0.1)
            assert server.model.master_gain == 1.0
        finally:
            await ws.close()
    finally:
        await server.stop()
