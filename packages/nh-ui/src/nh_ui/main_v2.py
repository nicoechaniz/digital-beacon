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

import argparse
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
from nh_renderers import (
    CompositeRenderer,
    PythonSounddeviceRenderer,
    SuperColliderOSCAdapter,
)
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
import time
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
    parser = argparse.ArgumentParser(description="NaturalHarmony v2 scene runtime")
    parser.add_argument("--beacon-osc", default=os.getenv("NH_BEACON_OSC", ""),
                        help="SuperCollider OSC address as host:port (e.g. 127.0.0.1:57120). Enables SC beacon file.")
    parser.add_argument("--no-shaper", action="store_true", help="Disable local Python sounddevice shaper audio")
    args = parser.parse_args()

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
        # Mirror the control back to any UI WebSocket clients so the browser
        # reflects external/Launchpad-initiated changes immediately.
        broadcast_control_event(event)

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

    # Audio renderers.
    device_str = os.getenv("NH_DEVICE", "").strip()
    device = int(device_str) if device_str else None

    renderers = []
    if not args.no_shaper:
        renderers.append(PythonSounddeviceRenderer(sr=48000, block_size=512, device=device))

    sc_adapter = None
    if args.beacon_osc:
        host, port = args.beacon_osc.rsplit(":", 1)
        sc_adapter = SuperColliderOSCAdapter(host=host, port=int(port), max_partials=32)
        renderers.append(sc_adapter)

    renderer = CompositeRenderer(renderers) if len(renderers) > 1 else renderers[0]
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

    # Synchronise SceneState into the legacy runtime model so OSC/sounddevice renderers
    # follow the v2 scene (f1, bands, spatial controls, shaper voices).
    async def _sync_scene_to_runtime():
        while True:
            try:
                runtime.model.update_from_base_field(state.to_base_field())
            except Exception:
                pass
            try:
                await asyncio.sleep(0.05)
            except asyncio.CancelledError:
                break

    async def _advance_envelopes():
        """Advance shaper envelopes so release phases decay and idle voices are removed."""
        while True:
            try:
                clock = time.time()
                for sr in state.shapers.values():
                    sr.advance_envelopes(dt=0.01, clock=clock)
                    sr.cleanup_released(max_age_s=0.5, clock=clock)
            except Exception:
                pass
            try:
                await asyncio.sleep(0.01)
            except asyncio.CancelledError:
                break

    sync_task = asyncio.create_task(_sync_scene_to_runtime())
    envelope_task = asyncio.create_task(_advance_envelopes())

    # Launchpad bridge.
    launchpad = LaunchpadBridge(
        client=client,
        loop=main_loop,
        broadcast=broadcast_control_event,
        scene_control_handler=state.apply_control,
    )
    attached = launchpad.start()
    print(f"[main_v2] Launchpad bridge attached: {attached}")
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
        sync_task.cancel()
        envelope_task.cancel()
        try:
            await sync_task
        except asyncio.CancelledError:
            pass
        try:
            await envelope_task
        except asyncio.CancelledError:
            pass


if __name__ == "__main__":
    asyncio.run(main())






