"""Launcher for NaturalHarmony v2 — scene-based runtime.

Starts the scene-aware system with:
  - HarmonicScene with beacon + shaper sources
  - SceneState for runtime voice lifecycle + path controls
  - V2 API endpoints (/nh/v2/scene, /nh/v2/presets, etc.)
  - V1 compatibility via projected base_field + legacy endpoints
  - Python sounddevice renderer
  - Launchpad bridge
  - Web UI on http://127.0.0.1:8080

Usage:
    cd ~/Projects/digital-beacon && source .venv/bin/activate
    python -m nh_ui.main_v2
"""

import asyncio
import os

from nh_core import (
    HarmonicScene,
    HarmonicField,
    BeaconSource,
    ShaperSource,
    SpatialBand,
    RendererCapabilities,
    Partial,
)
from nh_model import SceneState
from nh_renderers import PythonSounddeviceRenderer
from nh_runtime import BaseFieldServer, LocalModelClient
from nh_ui.launchpad_bridge import LaunchpadBridge
from nh_ui.scene_api import set_scene_state, set_legacy_control_handler
from nh_ui.server import (
    broadcast_control_event,
    set_launchpad_control_handler,
    set_renderer_changed_callback,
    set_runtime_server,
    set_ui_loop,
    set_scene_control_handler,
)
import uvicorn


def make_default_scene() -> HarmonicScene:
    """Create a default scene with beacon (32 bands) and shaper."""
    scene = HarmonicScene(
        version="2",
        sources={
            "beacon": BeaconSource(
                source_id="beacon",
                f1=65.0,
                vsrate=1.0,
                master_gain=0.8,
            ),
            "shaper": ShaperSource(
                source_id="shaper",
                master_gain=0.5,
                max_voices=32,
                polyphony_mode="steal",
            ),
        },
        metadata={"name": "default", "created_by": "main_v2"},
    )

    # 32 default beacon bands (1:1 with natural harmonics).
    for n in range(1, 33):
        scene.sources["beacon"].bands[n] = SpatialBand(
            az=(n - 1) * (360.0 / 32),
            dist=1.0,
            q=0.5,
            on=True,
        )

    return scene


def scene_to_base_field(scene: HarmonicScene) -> HarmonicField:
    """Project scene to legacy HarmonicField for v1 renderers."""
    return scene.project_to_base_field()


async def main():
    runtime_host = os.getenv("NH_RUNTIME_HOST", "127.0.0.1")
    runtime_port = int(os.getenv("NH_RUNTIME_PORT", "8765"))

    # Create the v2 scene and runtime state.
    scene = make_default_scene()
    state = SceneState(scene=scene)
    set_scene_state(state)  # Wire into /nh/v2/ API endpoints.

    # Bridge: WebSocket controls also update SceneState (pads, f1, gain).
    def _legacy_control(event):
        # Drive the legacy runtime model so web UI pads produce audio immediately.
        asyncio.create_task(_apply_to_runtime(event))

    async def _apply_to_runtime(event):
        runtime.model.apply_control(event)
        await runtime._broadcast_field()

    set_legacy_control_handler(_legacy_control)
    set_scene_control_handler(state.apply_control)

    # Create v1-compatible base field for legacy renderers.
    base_field = scene_to_base_field(scene)

    runtime = BaseFieldServer(
        base_field=base_field,
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

    # Bind UI event loop.
    main_loop = asyncio.get_running_loop()
    set_ui_loop(main_loop)

    # Audio renderer.
    device_str = os.getenv("NH_DEVICE", "").strip()
    device = int(device_str) if device_str else None
    renderer = PythonSounddeviceRenderer(sr=48000, block_size=512, device=device)
    client = LocalModelClient(
        uri=f"ws://{runtime_host}:{runtime_port}", renderer=renderer
    )

    async def _toggle_renderer(renderer_name: str) -> None:
        if renderer_name == "python" and not client._running:
            await client.start()
        elif renderer_name == "webaudio" and client._running:
            await client.stop()

    set_renderer_changed_callback(_toggle_renderer)
    await client.start()

    # Launchpad bridge.
    launchpad = LaunchpadBridge(
        client=client, loop=main_loop, broadcast=broadcast_control_event
    )
    launchpad.start()
    set_launchpad_control_handler(launchpad.on_control_event)

    print("""
╔══════════════════════════════════════════════════════╗
║  NaturalHarmony v2 — Scene Runtime                  ║
║                                                      ║
║  Web UI:    http://127.0.0.1:8080                    ║
║  Scene API: http://127.0.0.1:8080/nh/v2/scene        ║
║  Presets:   http://127.0.0.1:8080/nh/v2/presets      ║
║  V1 compat: http://127.0.0.1:8080/nh/v1/presets      ║
║                                                      ║
║  Sources:   beacon (32 bands) + shaper (additive)    ║
║  Renderer:  Python sounddevice (48000 Hz)            ║
║  Launchpad: auto-detect (if connected)               ║
╚══════════════════════════════════════════════════════╝
""")
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

