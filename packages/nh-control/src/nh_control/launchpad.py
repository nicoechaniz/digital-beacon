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
    """

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
        """Map a pad note to a harmonic index (1-based)."""
        row = self._row(note)
        col = self._col(note)
        # 8 columns x 8 rows = 64 pads -> 1..64
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
                        value={"n": n, "active": active},
                    )
                else:
                    ev = None
            else:
                ev = ControlEvent(
                    source="launchpad",
                    type="pad_on" if velocity > 0 else "pad_off",
                    value={"n": n, "vel": velocity},
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

    def to_midi(self, event: ControlEvent) -> Optional[Dict[str, Any]]:
        """Convert a control event back to a simple LED update dict (no real MIDI sent here)."""
        if event.type == "pad_on":
            return {"type": "note_on", "note": event.value["n"], "velocity": 127}
        elif event.type == "pad_off":
            return {"type": "note_off", "note": event.value["n"], "velocity": 0}
        elif event.type == "split_mode":
            return {"type": "control_change", "control": 104, "value": 127 if event.value else 0}
        return None
