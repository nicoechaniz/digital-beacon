"""MIDI control for the Shaper.

Two surfaces:
  - Launchpad Mini (primary): pad-mode 8x8 → 32 harmonics (pads 1..32),
    pads 33..64 reserved. In split mode (CC104 from NH), bottom 32 momentary
    + top 32 toggle. CC22 (stacking) cycles behaviour.
  - Minilab3 (auxiliary): CC74 f1, modwheel master, panic pad.

NOTE: Today (2026-06-22) this module only wires the Shaper. The Launchpad
pad-N → /beacon/voice/on mapping is what turns the Launchpad into the
performance surface. The Minilab3 is optional — beacon works without it.

Adapted from NaturalHarmony/harmonic_shaper/midi_control.py.
"""

import logging
import threading
from typing import Optional

try:
    import mido
    HAS_MIDO = True
except ImportError:
    HAS_MIDO = False

from .state import VoiceParameterStore
from . import config

log = logging.getLogger(__name__)


class LaunchpadMiniControl:
    """Launchpad Mini → Shaper voice_on/off for pads 1..N_BANDS.

    Pad mapping (programmer mode):
      Pad N (1..N_BANDS) on press   → store.voice_on(harmonic_n=N, ...)
      Pad N (1..N_BANDS) on release → store.voice_off(voice_id=...)

    The voice_id is generated locally (monotonic) so the same pad can be
    pressed again to retrigger without colliding with a still-held note.

    Pads above N_BANDS are ignored (they may be used later for split-mode
    page 2, or for transport controls).
    """

    def __init__(self, store: VoiceParameterStore, port_pattern: str = config.LAUNCHPAD_PORT_PATTERN):
        if not HAS_MIDO:
            raise ImportError("mido is required for MIDI control.")
        self._store = store
        self._port_pattern = port_pattern
        self._port = None
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._next_voice_id = 1
        # Map of (pad_n, voice_id) currently held for proper note-off routing
        self._held: dict[int, int] = {}

    def start(self) -> None:
        port_name = self._find_port()
        if not port_name:
            log.warning("Launchpad not found (pattern=%r). MIDI pad control disabled.",
                        self._port_pattern)
            return
        self._port = mido.open_input(port_name)
        self._running = True
        self._thread = threading.Thread(target=self._run, name="shaper-launchpad", daemon=True)
        self._thread.start()
        log.info("Launchpad control started: %s", port_name)

    def stop(self) -> None:
        self._running = False
        if self._port:
            try:
                self._port.close()
            except Exception:
                pass

    def _find_port(self) -> Optional[str]:
        for name in mido.get_input_names():
            if self._port_pattern.lower() in name.lower():
                return name
        return None

    def _run(self) -> None:
        for msg in self._port:
            if not self._running:
                break
            self._handle(msg)

    def _handle(self, msg) -> None:
        # Launchpad Mini in programmer mode: note_on (vel>0) = press, note_off or vel=0 = release
        if msg.type == "note_on" and msg.velocity > 0:
            pad_n = msg.note + 1   # Launchpad pads are 0-indexed
            if 1 <= pad_n <= config.N_BANDS:
                vid = self._next_voice_id
                self._next_voice_id += 1
                self._held[pad_n] = vid
                freq = self._store.f1 * pad_n
                self._store.voice_on(pad_n, vid, freq)
                log.debug("Launchpad pad %d ON  -> n=%d freq=%.1f Hz", pad_n, pad_n, freq)
            else:
                log.debug("Launchpad pad %d (out of range, only first %d active)",
                          pad_n, config.N_BANDS)
        elif msg.type == "note_off" or (msg.type == "note_on" and msg.velocity == 0):
            pad_n = msg.note + 1
            if pad_n in self._held:
                vid = self._held.pop(pad_n)
                self._store.voice_off(vid)
                log.debug("Launchpad pad %d OFF", pad_n)


class Minilab3Control:
    """Minilab3 — f1 modulation + master gain + panic.

    For the Kai demo this is OPTIONAL. The Launchpad is the primary surface.
    f1 modulation: 12 discrete points (slot reserved, not yet wired).
    """

    def __init__(self, store: VoiceParameterStore, port_pattern: str = config.MINILAB_PORT_PATTERN):
        if not HAS_MIDO:
            raise ImportError("mido is required for MIDI control.")
        self._store = store
        self._port_pattern = port_pattern
        self._port = None
        self._thread: Optional[threading.Thread] = None
        self._running = False

    def start(self) -> None:
        port_name = self._find_port()
        if not port_name:
            log.warning("Minilab3 not found (pattern=%r). Auxiliary MIDI disabled.",
                        self._port_pattern)
            return
        self._port = mido.open_input(port_name)
        self._running = True
        self._thread = threading.Thread(target=self._run, name="shaper-minilab", daemon=True)
        self._thread.start()
        log.info("Minilab3 control started: %s", port_name)

    def stop(self) -> None:
        self._running = False
        if self._port:
            try:
                self._port.close()
            except Exception:
                pass

    def _find_port(self) -> Optional[str]:
        for name in mido.get_input_names():
            if self._port_pattern.lower() in name.lower():
                return name
        return None

    def _run(self) -> None:
        for msg in self._port:
            if not self._running:
                break
            self._handle(msg)

    def _handle(self, msg) -> None:
        if msg.type == "control_change":
            self._handle_cc(msg.control, msg.value)
        elif msg.type in ("note_on", "note_off") and msg.velocity > 0:
            self._handle_pad(msg.note)

    def _handle_cc(self, cc: int, value: int) -> None:
        norm = value / 127.0
        # Modwheel (CC1) — master gain
        if cc == 1:
            self._store.set_master_gain(norm)
            log.debug("Minilab modwheel -> master gain=%.3f", norm)
            return
        # CC74 — f1 modulation (CONTINUOUS today; 12-point discrete slot reserved)
        # Today's behaviour: scale F1_MIN..F1_MAX linearly.
        # When F1_MOD_POINTS is non-empty, snap to the closest point instead.
        if cc == 74:
            if config.F1_MOD_POINTS:
                # Snap to closest of 12 discrete points
                points = config.F1_MOD_POINTS
                idx = int(round(norm * (len(points) - 1)))
                idx = max(0, min(len(points) - 1, idx))
                hz = points[idx]
            else:
                hz = config.F1_MIN + norm * (config.F1_MAX - config.F1_MIN)
            self._store.update_f1(hz)
            log.debug("Minilab CC74 -> f1=%.2f Hz", hz)

    def _handle_pad(self, note: int) -> None:
        if note == config.MINILAB_PANIC_PAD:
            log.info("Minilab panic")
            self._store.panic()
