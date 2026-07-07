from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from nh_control.event import ControlEvent


class LaunchpadAdapter:
    """Launchpad Mini adapter that emits normalized ControlEvents.

    Mirrors the digital_beacon/midi_control.py split-mode layout:
    - y=0 is the TOP physical row, y=7 is the BOTTOM physical row.
    - The bottom half of the 8x8 grid (rows H..E, i.e. rows 0..3 from the
      bottom) are momentary pads addressing harmonics 1..32.
    - The upper half (rows D..A, i.e. rows 4..7 from the bottom) are toggles
      addressing the same harmonics 1..32.
    - CC104 toggles split mode.
    - CC111 is panic.

    n is always in 1..32 for both halves (upper addresses same harmonics as lower).
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
        return note // self.stride

    def _col(self, note: int) -> int:
        return note % self.stride

    def note_to_harmonic(self, note: int) -> int:
        """Map a pad note to a harmonic index (1-based, 1..32 for both halves)."""
        row = self._row(note)
        col = self._col(note)
        # Match pre-refactor layout: row 0 is the top physical row, row 7 the bottom.
        row_from_bottom = 7 - row
        if self.split_mode and row_from_bottom >= 4:
            row_from_bottom = row_from_bottom - 4
        return row_from_bottom * 8 + col + 1

    def _is_upper_half(self, note: int) -> bool:
        """Return True if the physical pad is in the upper (toggle) half."""
        return (7 - self._row(note)) >= 4

    def on_midi_message(self, msg: Any) -> Optional[ControlEvent]:
        """Process a mido message and emit a ControlEvent."""
        if msg.type == "note_on":
            note = msg.note
            velocity = msg.velocity
            n = self.note_to_harmonic(note)
            if self.split_mode and self._is_upper_half(note):
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
        row_from_bottom = idx // 8
        col = idx % 8
        if upper:
            row_from_bottom += 4
        # Convert back to Launchpad coordinates (row 0 = top).
        row = 7 - row_from_bottom
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
