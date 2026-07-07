"""Launcher for nh-ui host + runtime server + Python sounddevice renderer."""
import asyncio
import os

from nh_core import HarmonicField, RendererCapabilities
from nh_renderers import PythonSounddeviceRenderer
from nh_runtime import BaseFieldServer, LocalModelClient
from nh_ui.launchpad_bridge import LaunchpadBridge
from nh_ui.server import (
    broadcast_control_event,
    set_launchpad_control_handler,
    set_renderer_changed_callback,
    set_runtime_server,
    set_ui_loop,
)
import uvicorn


async def main():
    runtime_host = os.getenv("NH_RUNTIME_HOST", "127.0.0.1")
    runtime_port = int(os.getenv("NH_RUNTIME_PORT", "8765"))

    runtime = BaseFieldServer(
        base_field=HarmonicField(f1=65.0),
        host=runtime_host,
        port=runtime_port,
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

    # Bind the UI server's event loop so control broadcasts scheduled from the
    # Launchpad MIDI thread land on the correct loop.
    main_loop = asyncio.get_running_loop()
    set_ui_loop(main_loop)

    device_str = os.getenv("NH_DEVICE", "").strip()
    device = int(device_str) if device_str else None
    renderer = PythonSounddeviceRenderer(sr=48000, block_size=512, device=device)
    client = LocalModelClient(uri=f"ws://{runtime_host}:{runtime_port}", renderer=renderer)

    async def _toggle_renderer(renderer_name: str) -> None:
        if renderer_name == "python" and not client._running:
            await client.start()
        elif renderer_name == "webaudio" and client._running:
            await client.stop()

    set_renderer_changed_callback(_toggle_renderer)
    # Default to the Python sounddevice renderer. Master gain stays at the model
    # default of 0 (silence) until the performer explicitly raises it, so audio
    # never starts loud regardless of the loaded preset.
    await client.start()

    # Physical Launchpad: pads -> control events (runtime + web mirror) and LED
    # feedback. Reuses nh_control.LaunchpadAdapter; a no-op without hardware.
    launchpad = LaunchpadBridge(client=client, loop=main_loop, broadcast=broadcast_control_event)
    launchpad.start()
    set_launchpad_control_handler(launchpad.on_control_event)

    config = uvicorn.Config(
        "nh_ui.server:app",
        host=os.getenv("NH_UI_HOST", "127.0.0.1"),
        port=int(os.getenv("NH_UI_PORT", "8080")),
        log_level="info",
    )
    server = uvicorn.Server(config)
    try:
        await server.serve()
    finally:
        launchpad.stop()
        await client.stop()
        await runtime.stop()


if __name__ == "__main__":
    asyncio.run(main())
