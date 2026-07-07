from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from nh_control.event import ControlEvent


class LaunchpadAdapter:
    """Launchpad Mini adapter that emits normalized ControlEvents.

    Mirrors the digital_beacon/midi_control.py split-mode logic:
    - lower half of the 8x8 grid (rows 0-3) are momentary pads
    - upper half (rows 4-7) are toggles
    - CC104 toggles split mode
    - CC111 / note 120 is panic

    n always in 1..32 for both halves (upper addresses same harmonics as lower).
    """

    # LED feedback colors (Launchpad velocity in Programmer mode)
    COLOR_OFF = 0
    COLOR_GREEN = 60      # lower half momentary
    COLOR_ORANGE = 21     # upper half toggle

    def __init__(self, stride: int = 16, split_mode: bool = True,
                 callback: Optional[Callable[[ControlEvent], None]] = None):
        self.stride = stride
        self.split_mode = split_mode
        self.callback = callback
        self.toggle_state: Dict[int, bool] = {}

    def _row(self, note: int) -> int:
        if self.stride == 8:
            return note // 8
        return note // 16

    def _col(self, note: int) -> int:
        if self.stride == 8:
            return note % 8
        return note % 16

    def _pad_to_n(self, note: int) -> int:
        """Map a pad note to a harmonic index (1-based, 1..32 for both halves)."""
        row = self._row(note)
        col = self._col(note)
        if self.split_mode and row >= 4:
            row = row - 4
        return row * 8 + col + 1

    def on_midi_message(self, msg: Any) -> Optional[ControlEvent]:
        """Process a mido message and emit a ControlEvent."""
        if msg.type == "note_on":
            note = msg.note
            velocity = msg.velocity
            n = self._pad_to_n(note)
            row = self._row(note)
            if self.split_mode and row >= 4:
                # toggle
                if velocity > 0:
                    self.toggle_state[note] = not self.toggle_state.get(note, False)
                    active = self.toggle_state[note]
                    ev = ControlEvent(
                        source="launchpad",
                        type="pad_toggle",
                        value={"n": n, "active": active, "note": note},
                    )
                else:
                    ev = None
            else:
                ev = ControlEvent(
                    source="launchpad",
                    type="pad_on" if velocity > 0 else "pad_off",
                    value={"n": n, "vel": velocity, "note": note},
                )
            if ev and self.callback:
                self.callback(ev)
            return ev
        elif msg.type == "control_change":
            if msg.control == 104:
                self.split_mode = bool(msg.value > 63)
                ev = ControlEvent(source="launchpad", type="split_mode", value=self.split_mode)
            elif msg.control == 111:
                ev = ControlEvent(source="launchpad", type="panic", value=None)
            else:
                ev = ControlEvent(source="launchpad", type="cc", value={"cc": msg.control, "value": msg.value})
            if self.callback:
                self.callback(ev)
            return ev
        return None

    def _n_to_note(self, n: int, upper: bool = False) -> int:
        """Map harmonic n (1-based) back to physical pad note for lower or upper half."""
        idx = n - 1
        row = idx // 8
        col = idx % 8
        if upper:
            row += 4
        if self.stride == 8:
            return row * 8 + col
        return row * 16 + col

    def to_midi(self, event: ControlEvent) -> Optional[Dict[str, Any]]:
        """Convert a control event back to a simple LED update dict (no real MIDI sent here)."""
        if event.type == "pad_on":
            note = event.value.get("note", event.value.get("n", 0))
            return {"type": "note_on", "note": note, "velocity": 127}
        elif event.type == "pad_off":
            note = event.value.get("note", event.value.get("n", 0))
            return {"type": "note_on", "note": note, "velocity": 0}
        elif event.type == "pad_toggle":
            note = event.value.get("note", self._n_to_note(event.value.get("n", 1), upper=True))
            vel = self.COLOR_ORANGE if event.value.get("active") else self.COLOR_OFF
            return {"type": "note_on", "note": note, "velocity": vel}
        elif event.type == "split_mode":
            return {"type": "control_change", "control": 104, "value": 127 if event.value else 0}
        return None

    def led_for_event(self, event: ControlEvent) -> Optional[Dict[str, Any]]:
        """Return dict for LED update (note_on + velocity=color) to drive physical Launchpad lights."""
        if event.type == "pad_toggle":
            note = event.value.get("note") or self._n_to_note(event.value.get("n", 1), upper=True)
            vel = self.COLOR_ORANGE if event.value.get("active", False) else self.COLOR_OFF
            return {"type": "note_on", "note": note, "velocity": vel}
        elif event.type == "pad_on":
            note = event.value.get("note") or self._n_to_note(event.value.get("n", 1), upper=False)
            vel = self.COLOR_GREEN if event.value.get("vel", 0) > 0 else self.COLOR_OFF
            return {"type": "note_on", "note": note, "velocity": vel}
        elif event.type == "pad_off":
            note = event.value.get("note") or self._n_to_note(event.value.get("n", 1), upper=False)
            return {"type": "note_on", "note": note, "velocity": self.COLOR_OFF}
        elif event.type == "panic":
            return {"type": "all_off"}
        return None
