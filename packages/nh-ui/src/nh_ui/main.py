"""Launcher for nh-ui host + runtime server + Python sounddevice renderer."""
import asyncio
import os

from nh_core import HarmonicField, RendererCapabilities
from nh_renderers import PythonSounddeviceRenderer
from nh_runtime import BaseFieldServer, LocalModelClient
from nh_ui.server import set_runtime_server, set_renderer_changed_callback
import uvicorn

async def main():
    runtime = BaseFieldServer(
        base_field=HarmonicField(f1=65.0),
        host=os.getenv("NH_RUNTIME_HOST", "127.0.0.1"),
        port=int(os.getenv("NH_RUNTIME_PORT", "8765")),
        update_hz=10.0,
        renderer_capabilities=RendererCapabilities(
            max_partials=32,
            supports_phase=True,
            supports_spatial=True,
            available_renderers=["python", "webaudio"],
            default_renderer="python",
        ),
        sensor_mapping={
            "muse_focus": {"param": "master_gain", "scale": 1.0, "offset": 0.0},
            "imu.orientation.yaw": {"param": "spatial_rotation", "scale": 1.0, "offset": 0.0},
            "imu.orientation.pitch": {"param": "f1_offset", "scale": 0.5, "offset": 0.0},
        },
    )
    await runtime.start()
    set_runtime_server(runtime)

    renderer = PythonSounddeviceRenderer(sr=48000, block_size=512)
    client = LocalModelClient(uri=f"ws://{os.getenv("NH_RUNTIME_HOST", "127.0.0.1")}:{os.getenv("NH_RUNTIME_PORT", "8765")}", renderer=renderer)

    async def _toggle_renderer(renderer_name: str) -> None:
        if renderer_name == "python":
            if not client._running:
                await client.start()
        elif renderer_name == "webaudio":
            if client._running:
                await client.stop()

    set_renderer_changed_callback(_toggle_renderer)
    # Default to the Python sounddevice renderer.
    await client.start()

    config = uvicorn.Config("nh_ui.server:app", host=os.getenv("NH_UI_HOST", "127.0.0.1"), port=int(os.getenv("NH_UI_PORT", "8080")), log_level="info")
    server = uvicorn.Server(config)
    try:
        await server.serve()
    finally:
        await client.stop()
        await runtime.stop()

if __name__ == "__main__":
    asyncio.run(main())
