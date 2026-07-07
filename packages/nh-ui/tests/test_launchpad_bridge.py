"""Deterministic tests for the Launchpad bridge (no physical hardware).

A fake ``mido`` module supplies message and port objects so the whole
pad -> LED feedback path can be exercised in-process.
"""
import asyncio
import threading
import time

from nh_control import LaunchpadAdapter
from nh_control.event import ControlEvent
from nh_ui.launchpad_bridge import LaunchpadBridge


class FakeMessage:
    def __init__(self, type, note=0, velocity=0, control=0, value=0):
        self.type = type
        self.note = note
        self.velocity = velocity
        self.control = control
        self.value = value


class FakeOutPort:
    def __init__(self):
        self.sent = []

    def send(self, msg):
        self.sent.append(msg)

    def close(self):
        pass


class FakeMido:
    """Minimal stand-in exposing only what the bridge uses."""
    Message = FakeMessage

    @staticmethod
    def get_input_names():
        return []

    @staticmethod
    def get_output_names():
        return []


def _bridge_with_out_port():
    bridge = LaunchpadBridge(client=None, loop=None, broadcast=lambda payload: None, midi=FakeMido())
    bridge.adapter = LaunchpadAdapter(stride=16, split_mode=True)
    bridge.out_port = FakeOutPort()
    return bridge


def test_lower_pad_lights_green():
    bridge = _bridge_with_out_port()
    # bottom-left pad -> momentary -> green
    ev = bridge.adapter.on_midi_message(FakeMessage("note_on", note=112, velocity=127))
    bridge._drive_led(ev)
    assert bridge.out_port.sent, "expected an LED message"
    last = bridge.out_port.sent[-1]
    assert last.note == 112
    assert last.velocity == LaunchpadAdapter.COLOR_GREEN


def test_upper_toggle_lights_orange_then_off():
    bridge = _bridge_with_out_port()
    ev_on = bridge.adapter.on_midi_message(FakeMessage("note_on", note=48, velocity=127))
    bridge._drive_led(ev_on)
    assert bridge.out_port.sent[-1].note == 48
    assert bridge.out_port.sent[-1].velocity == LaunchpadAdapter.COLOR_ORANGE

    ev_off = bridge.adapter.on_midi_message(FakeMessage("note_on", note=48, velocity=127))
    bridge._drive_led(ev_off)
    assert bridge.out_port.sent[-1].velocity == LaunchpadAdapter.COLOR_OFF


def test_panic_clears_all_leds():
    bridge = _bridge_with_out_port()
    bridge.on_control_event({"type": "panic"})
    assert len(bridge.out_port.sent) == 128
    assert all(m.velocity == 0 for m in bridge.out_port.sent)


def test_relay_schedules_runtime_send_and_broadcast():
    """A pad event on the MIDI thread reaches both the runtime and the web mirror.

    Uses a real event loop on a background thread — the exact cross-thread hand-off
    the physical controller relies on — with no hardware involved.
    """
    sent = []
    broadcasts = []

    class RecordingClient:
        async def send_control(self, payload):
            sent.append(payload)

    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    try:
        bridge = LaunchpadBridge(
            client=RecordingClient(),
            loop=loop,
            broadcast=lambda payload: broadcasts.append(payload),
            midi=FakeMido(),
        )
        event = ControlEvent(source="launchpad", type="pad_on", value={"n": 1, "vel": 127})
        bridge._relay(event)  # called as the adapter callback would, off the loop
        deadline = time.time() + 2.0
        while time.time() < deadline and (not sent or not broadcasts):
            time.sleep(0.02)
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2.0)
        loop.close()

    assert broadcasts and broadcasts[-1]["type"] == "pad_on"
    assert broadcasts[-1]["value"]["n"] == 1
    assert sent and sent[-1]["type"] == "pad_on"


def test_start_without_midi_backend_is_noop():
    bridge = LaunchpadBridge(client=None, loop=None, broadcast=lambda payload: None, midi=None)
    assert bridge.start() is False
    assert bridge.adapter is None


def test_start_without_device_is_noop():
    """A MIDI backend with no Launchpad attached must not start a reader thread."""
    bridge = LaunchpadBridge(client=None, loop=None, broadcast=lambda payload: None, midi=FakeMido())
    assert bridge.start() is False
    assert bridge.in_port is None
