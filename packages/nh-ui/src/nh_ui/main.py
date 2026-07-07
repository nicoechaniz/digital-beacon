"""Launcher for nh-ui host + runtime server + Python sounddevice renderer."""
import asyncio
import os
import threading
from typing import Any, Optional

try:
    import mido
except Exception:  # mido optional at runtime if no MIDI
    mido = None

from nh_core import HarmonicField, RendererCapabilities
from nh_renderers import PythonSounddeviceRenderer
from nh_runtime import BaseFieldServer, LocalModelClient
from nh_control import LaunchpadAdapter
from nh_ui.server import (
    set_runtime_server,
    set_renderer_changed_callback,
    set_launchpad_control_handler,
    broadcast_control_event,
)
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

    # --- LaunchpadAdapter integration (physical MIDI mirror + LED feedback) ---
    # Clean architecture: adapter from nh-control; no reimplementation of pad logic.
    # Sends control events (pad_on/pad_toggle etc) to runtime; receives drive LEDs orange/green.
    # Optional, deterministic (no hardware required for tests).
    lp_adapter: Optional[LaunchpadAdapter] = None
    lp_in_port: Any = None
    lp_out_port: Any = None
    lp_thread: Optional[threading.Thread] = None
    lp_stop = threading.Event()

    def _send_lp_control(ev: Any) -> None:
        """Send launchpad-derived control to runtime (via client) and broadcast to UI mirrors."""
        if ev is None:
            return
        evd = ev.to_dict() if hasattr(ev, "to_dict") else ev
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = asyncio.get_event_loop()
        # runtime via WS client
        try:
            fut = asyncio.run_coroutine_threadsafe(client.send_control(evd), loop)
            fut.result(timeout=0.5)
        except Exception:
            pass
        # UI mirror broadcast (thread-safe)
        try:
            loop.call_soon_threadsafe(lambda e=evd: broadcast_control_event(e))
        except Exception:
            pass

    def _drive_led(ev: Any) -> None:
        """Immediately drive physical LED feedback from event (orange upper toggle, green lower momentary)."""
        if not lp_out_port or not lp_adapter or ev is None:
            return
        try:
            led = lp_adapter.led_for_event(ev) if hasattr(lp_adapter, "led_for_event") else None
            if led is None:
                return
            if led.get("type") == "all_off":
                for nn in range(128):
                    try:
                        lp_out_port.send(mido.Message("note_on", note=nn, velocity=0))
                    except Exception:
                        pass
                return
            m = mido.Message(
                led.get("type", "note_on"),
                note=led.get("note", 0),
                velocity=led.get("velocity", 0),
            )
            lp_out_port.send(m)
        except Exception:
            pass

    def _lp_panic_handler(payload: dict) -> None:
        """Handle panic (from web or elsewhere) by clearing launchpad lights and adapter state."""
        if payload and payload.get("type") == "panic":
            if lp_adapter:
                try:
                    lp_adapter.toggle_state.clear()
                except Exception:
                    pass
            if lp_out_port:
                try:
                    for nn in range(128):
                        lp_out_port.send(mido.Message("note_on", note=nn, velocity=0))
                except Exception:
                    pass

    if mido is not None:
        try:
            lp_adapter = LaunchpadAdapter(stride=16, split_mode=True, callback=_send_lp_control)
            # find in/out ports (graceful if absent)
            in_name = None
            for name in mido.get_input_names():
                if "launchpad" in name.lower() or "lpmini" in name.lower() or name.lower().startswith("launchpad"):
                    in_name = name
                    break
            if in_name:
                lp_in_port = mido.open_input(in_name)
            out_name = None
            for name in mido.get_output_names():
                if in_name and in_name.split()[0] in name:
                    out_name = name
                    break
                if not out_name and ("launchpad" in name.lower() or "lpmini" in name.lower()):
                    out_name = name
            if out_name:
                try:
                    lp_out_port = mido.open_output(out_name)
                except Exception:
                    lp_out_port = None
            if lp_in_port:
                def _lp_loop():
                    try:
                        for msg in lp_in_port:
                            if lp_stop.is_set():
                                break
                            ev = lp_adapter.on_midi_message(msg) if lp_adapter else None
                            if ev:
                                _drive_led(ev)
                            if ev and getattr(ev, "type", None) == "panic":
                                _drive_led(ev)  # all off
                    except Exception:
                        pass
                lp_thread = threading.Thread(target=_lp_loop, name="nh-ui-launchpad", daemon=True)
                lp_thread.start()
                set_launchpad_control_handler(_lp_panic_handler)
        except Exception:
            # no launchpad or mido port issue - continue without, tests unaffected
            lp_adapter = None
            lp_in_port = None
            lp_out_port = None

    # Send a safe initial master from server side too (in case no UI client yet)
    try:
        asyncio.create_task(client.send_control({"type": "master", "value": 0.5}))
    except Exception:
        pass

    config = uvicorn.Config("nh_ui.server:app", host=os.getenv("NH_UI_HOST", "127.0.0.1"), port=int(os.getenv("NH_UI_PORT", "8080")), log_level="info")
    server = uvicorn.Server(config)
    try:
        await server.serve()
    finally:
        lp_stop.set()
        if lp_in_port:
            try:
                lp_in_port.close()
            except Exception:
                pass
        if lp_out_port:
            try:
                # clear lights on shutdown
                for nn in range(128):
                    lp_out_port.send(mido.Message("note_on", note=nn, velocity=0))
            except Exception:
                pass
            try:
                lp_out_port.close()
            except Exception:
                pass
        await client.stop()
        await runtime.stop()

if __name__ == "__main__":
    asyncio.run(main())
