"""Bridge between a physical Novation Launchpad and the NaturalHarmony runtime.

Wires :class:`nh_control.LaunchpadAdapter` to MIDI I/O:

* physical pad presses become normalized control events that are relayed to the
  runtime (so they affect audio) and broadcast to the web UI (so the on-screen
  mirror follows the hardware);
* those same events drive LED feedback on the device — orange for the upper-half
  toggles, green for the lower-half momentaries.

All pad mapping and LED colour logic lives in ``LaunchpadAdapter``; this module
only handles MIDI ports, the reader thread, and thread-safe scheduling onto the
UI event loop. It degrades to a no-op when no MIDI backend or device is present,
so the host (and the test suite) run unaffected without hardware.
"""
from __future__ import annotations

import asyncio
import os
import threading
from typing import Any, Callable, Iterable, Optional

from nh_control import LaunchpadAdapter

try:  # mido is optional; absent in headless / CI environments.
    import mido
except Exception:  # pragma: no cover - depends on host packages
    mido = None

_PORT_HINTS = ("launchpad", "lpmini")

# Sentinel distinguishing "midi not provided -> auto-detect the real backend"
# from an explicit ``midi=None`` meaning "no MIDI backend" (used by tests).
_AUTODETECT = object()


def _looks_like_launchpad(name: str) -> bool:
    lower = name.lower()
    return any(hint in lower for hint in _PORT_HINTS)


class LaunchpadBridge:
    """Connects a Launchpad's MIDI ports to the runtime via ``LaunchpadAdapter``."""

    def __init__(
        self,
        client: Any,
        loop: Optional[asyncio.AbstractEventLoop],
        broadcast: Callable[[dict], None],
        *,
        stride: int = 16,
        split_mode: bool = True,
        midi: Any = _AUTODETECT,
        scene_control_handler: Optional[Callable[[dict], None]] = None,
    ):
        self._client = client
        self._loop = loop
        self._broadcast = broadcast
        self._scene_control_handler = scene_control_handler
        # Unset -> use the real mido backend; explicit None -> no MIDI at all.
        self._midi = mido if midi is _AUTODETECT else midi
        self._stride = stride
        self._split_mode = split_mode
        self.adapter: Optional[LaunchpadAdapter] = None
        self.in_port: Any = None
        self.out_port: Any = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._out_lock = threading.Lock()

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> bool:
        """Open MIDI ports and start reading. Returns True if an input attached.

        A missing MIDI backend or device is not an error: the bridge simply stays
        inert and the rest of the system keeps running.
        """
        if self._midi is None:
            print("[launchpad] no MIDI backend available")
            return False
        try:
            self.adapter = LaunchpadAdapter(
                stride=self._stride, split_mode=self._split_mode, callback=self._relay
            )
            input_names = self._midi.get_input_names()
            output_names = self._midi.get_output_names()
            print(f"[launchpad] MIDI inputs: {input_names}")
            print(f"[launchpad] MIDI outputs: {output_names}")
            in_name = self._first_port(input_names)
            out_name = self._first_port(output_names)
            print(f"[launchpad] selected input={in_name}, output={out_name}")
            if in_name:
                self.in_port = self._midi.open_input(in_name)
            if out_name:
                self.out_port = self._midi.open_output(out_name)
        except Exception as e:
            print(f"[launchpad] failed to open MIDI ports: {e}")
            self._teardown_ports()
            self.adapter = None
            return False
        if self.in_port is None:
            print("[launchpad] no MIDI input port found")
            return False
        print(f"[launchpad] reading from {self.in_port}")
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._reader, name="nh-ui-launchpad", daemon=True
        )
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        self._clear_leds()
        self._teardown_ports()

    # -- relay + LED feedback ---------------------------------------------
    def _relay(self, event: Any) -> None:
        """Adapter callback (runs on the MIDI thread): forward a control event.

        Non-blocking: hands the work to the UI loop in a single thread-safe hop
        and returns immediately, so the MIDI reader never stalls. On the loop we
        start the runtime send and mirror the event to the web UI.
        """
        if event is None or self._loop is None:
            return
        payload = event.to_dict() if hasattr(event, "to_dict") else event

        def _dispatch() -> None:
            try:
                asyncio.ensure_future(self._safe_send(payload))
            except Exception:
                pass
            try:
                self._broadcast(payload)
            except Exception:
                pass
            try:
                if self._scene_control_handler is not None:
                    self._scene_control_handler(payload)
            except Exception:
                pass

        try:
            self._loop.call_soon_threadsafe(_dispatch)
        except Exception:
            pass

    async def _safe_send(self, payload: dict) -> None:
        try:
            await self._client.send_control(payload)
        except Exception:
            pass

    def _drive_led(self, event: Any) -> None:
        """Drive physical LED feedback for an event (orange toggle / green momentary)."""
        if self.out_port is None or self.adapter is None or event is None or self._midi is None:
            return
        led = self.adapter.led_for_event(event)
        if led is None:
            return
        if led.get("type") == "all_off":
            self._clear_leds()
            return
        message = self._midi.Message(
            led.get("type", "note_on"),
            note=led.get("note", 0),
            velocity=led.get("velocity", 0),
        )
        with self._out_lock:
            try:
                self.out_port.send(message)
            except Exception:
                pass

    def on_control_event(self, payload: dict) -> None:
        """Handle a control from any source (e.g. web PANIC) for LED feedback."""
        if payload and payload.get("type") == "panic":
            if self.adapter is not None:
                self.adapter.toggle_state.clear()
            self._clear_leds()

    # -- helpers -----------------------------------------------------------
    def _reader(self) -> None:
        try:
            for msg in self.in_port:
                if self._stop.is_set():
                    break
                event = self.adapter.on_midi_message(msg) if self.adapter else None
                if event is not None:
                    self._drive_led(event)
                    self._relay(event)
        except Exception as e:
            print(f"[launchpad] reader error: {e}")

    def _clear_leds(self) -> None:
        if self.out_port is None or self._midi is None:
            return
        with self._out_lock:
            try:
                for note in range(128):
                    self.out_port.send(self._midi.Message("note_on", note=note, velocity=0))
            except Exception:
                pass

    def _first_port(self, names: Iterable[str]) -> Optional[str]:
        forced = os.getenv("NH_LAUNCHPAD_PORT")
        if forced:
            for name in names:
                if forced in name:
                    return name
            # If forced env var is set but no exact match, allow partial match.
            for name in names:
                if forced.lower() in name.lower():
                    return name
        for name in names:
            if _looks_like_launchpad(name):
                return name
        # Fallback: prefer any non-virtual MIDI port with both input and output.
        for name in names:
            lower = name.lower()
            if "through" in lower or "virtual" in lower or "dummy" in lower:
                continue
            return name
        return None

    def _teardown_ports(self) -> None:
        for port in (self.in_port, self.out_port):
            if port is not None:
                try:
                    port.close()
                except Exception:
                    pass
        self.in_port = None
        self.out_port = None

