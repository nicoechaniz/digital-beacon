"""Thread-safe voice parameter store for the Shaper.

Adapted from NaturalHarmony/harmonic_shaper/state.py:
- Raised polyphony from 5 to MAX_VOICES (32).
- Voice identity is `harmonic_n` (1..32). Multiple voice_ids can map to the
  same harmonic_n (layering) — we keep the most recent active one.
- f1 is exposed as a first-class store attribute so the OSC receiver can
  push updates without touching VoiceParams.
"""

import math
import threading
from copy import copy
from dataclasses import dataclass, field
from typing import Callable, Optional

from . import config


@dataclass
class VoiceParams:
    """Parameters for a single Shaper voice (one pure sine)."""
    harmonic_n: int = 0
    freq: float = 0.0          # Hz — set by beacon broadcast
    gain: float = config.DEFAULT_VOICE_GAIN
    pan: float = config.DEFAULT_VOICE_PAN       # -1..+1
    phase: float = config.DEFAULT_VOICE_PHASE_DEG  # radians
    attack_s: float = config.DEFAULT_VOICE_ATTACK_S
    release_s: float = config.DEFAULT_VOICE_RELEASE_S
    active: bool = False
    voice_id: Optional[int] = None

    def copy(self) -> "VoiceParams":
        return copy(self)

    def to_dict(self) -> dict:
        return {
            "harmonic_n": self.harmonic_n,
            "freq": round(self.freq, 3),
            "gain": round(self.gain, 4),
            "pan": round(self.pan, 4),
            "phase_deg": round(math.degrees(self.phase) % 360, 1),
            "active": self.active,
        }


class VoiceParameterStore:
    """Thread-safe store for per-harmonic Shaper parameters.

    Keyed by harmonic_n (1..32). The beacon populates voice_on/off/freq;
    control surfaces (MIDI/OSC/Web) set gain/pan/phase.
    """

    def __init__(self, on_change: Optional[Callable[[], None]] = None):
        self._lock = threading.RLock()
        self._voices: dict[int, VoiceParams] = {}
        self._active_history: list[int] = []  # chronological, for note stealing
        self.f1: float = config.DEFAULT_F1
        self._master_gain: float = config.DEFAULT_SHAPER_MASTER
        self._global_attack_s: float = config.DEFAULT_VOICE_ATTACK_S
        self._global_release_s: float = config.DEFAULT_VOICE_RELEASE_S
        self._on_change = on_change
        self._panic_callback: Optional[Callable[[], None]] = None

    # ─── Internal helpers ─────────────────────────────────────────────────

    def _notify(self) -> None:
        if self._on_change:
            try:
                self._on_change()
            except Exception:
                pass

    def _ensure(self, n: int) -> None:
        if n not in self._voices:
            v = VoiceParams(harmonic_n=n)
            v.attack_s = self._global_attack_s
            v.release_s = self._global_release_s
            self._voices[n] = v

    # ─── Beacon-driven lifecycle ──────────────────────────────────────────

    def voice_on(self, harmonic_n: int, voice_id: int, freq: float, gain: float = None) -> None:
        with self._lock:
            self._ensure(harmonic_n)
            v = self._voices[harmonic_n]
            v.voice_id = voice_id
            v.freq = freq
            v.active = True
            if gain is not None:
                v.gain = max(0.0, min(1.0, float(gain)))
            else:
                v.gain = config.DEFAULT_VOICE_GAIN

            if harmonic_n in self._active_history:
                self._active_history.remove(harmonic_n)
            self._active_history.append(harmonic_n)

            # Note stealing: drop oldest if over limit
            while len(self._active_history) > config.MAX_VOICES:
                oldest_n = self._active_history.pop(0)
                self._voices[oldest_n].active = False
        self._notify()

    def voice_off(self, voice_id: int) -> None:
        with self._lock:
            for n, v in self._voices.items():
                if v.voice_id == voice_id:
                    v.active = False
                    if n in self._active_history:
                        self._active_history.remove(n)
                    break
        self._notify()

    def voice_freq(self, voice_id: int, freq: float) -> None:
        with self._lock:
            for v in self._voices.values():
                if v.voice_id == voice_id:
                    v.freq = freq
                    break

    def update_f1(self, f1: float) -> None:
        with self._lock:
            self.f1 = max(config.F1_MIN, min(config.F1_MAX, float(f1)))
        self._notify()

    # ─── Parameter control ────────────────────────────────────────────────

    def set_gain(self, harmonic_n: int, gain: float) -> None:
        with self._lock:
            self._ensure(harmonic_n)
            self._voices[harmonic_n].gain = max(0.0, min(1.0, float(gain)))
        self._notify()

    def set_pan(self, harmonic_n: int, pan: float) -> None:
        with self._lock:
            self._ensure(harmonic_n)
            self._voices[harmonic_n].pan = max(-1.0, min(1.0, float(pan)))
        self._notify()

    def set_phase(self, harmonic_n: int, phase_deg: float) -> None:
        with self._lock:
            self._ensure(harmonic_n)
            self._voices[harmonic_n].phase = math.radians(float(phase_deg) % 360)
        self._notify()

    def set_attack(self, harmonic_n: int, attack_s: float) -> None:
        with self._lock:
            self._ensure(harmonic_n)
            self._voices[harmonic_n].attack_s = max(0.0, min(5.0, float(attack_s)))
        self._notify()

    def set_release(self, harmonic_n: int, release_s: float) -> None:
        with self._lock:
            self._ensure(harmonic_n)
            self._voices[harmonic_n].release_s = max(0.0, min(5.0, float(release_s)))
        self._notify()

    def set_params(self, harmonic_n: int, **kwargs) -> None:
        with self._lock:
            self._ensure(harmonic_n)
            v = self._voices[harmonic_n]
            if "gain" in kwargs:
                v.gain = max(0.0, min(1.0, float(kwargs["gain"])))
            if "pan" in kwargs:
                v.pan = max(-1.0, min(1.0, float(kwargs["pan"])))
            if "phase_deg" in kwargs:
                v.phase = math.radians(float(kwargs["phase_deg"]) % 360)
            if "attack_s" in kwargs:
                v.attack_s = max(0.0, min(5.0, float(kwargs["attack_s"])))
            if "release_s" in kwargs:
                v.release_s = max(0.0, min(5.0, float(kwargs["release_s"])))
        self._notify()

    def set_master_gain(self, gain: float) -> None:
        with self._lock:
            self._master_gain = max(0.0, min(1.0, float(gain)))

    def get_master_gain(self) -> float:
        with self._lock:
            return self._master_gain

    def set_global_attack(self, attack_s: float) -> None:
        with self._lock:
            self._global_attack_s = max(0.0, min(5.0, float(attack_s)))
        self._notify()

    def set_global_release(self, release_s: float) -> None:
        with self._lock:
            self._global_release_s = max(0.0, min(5.0, float(release_s)))
        self._notify()

    def panic(self) -> None:
        with self._lock:
            for v in self._voices.values():
                v.active = False
                v.gain = config.DEFAULT_VOICE_GAIN
                v.pan = 0.0
                v.phase = 0.0
            self._active_history.clear()
        self._notify()
        # Also notify the MIDI control (Launchpad) to clear lights + state
        if self._panic_callback:
            try:
                self._panic_callback()
            except Exception:
                pass

    # ─── Snapshot accessors ───────────────────────────────────────────────

    def get_snapshot(self) -> dict[int, VoiceParams]:
        """Active voices only — for the audio callback (hot path)."""
        with self._lock:
            return {k: v.copy() for k, v in self._voices.items()
                    if v.active and v.freq > 0}

    def get_all_snapshot(self) -> dict[int, VoiceParams]:
        with self._lock:
            return {k: v.copy() for k, v in self._voices.items()}

    def to_dict(self) -> dict:
        with self._lock:
            return {
                "f1": self.f1,
                "master_gain": self._master_gain,
                "global_attack_s": self._global_attack_s,
                "global_release_s": self._global_release_s,
                "voices": {
                    str(k): {
                        "gain": v.gain,
                        "pan": v.pan,
                        "phase_deg": round(math.degrees(v.phase) % 360, 1),
                        "attack_s": v.attack_s,
                        "release_s": v.release_s,
                        "active": v.active,
                        "freq": v.freq,
                    }
                    for k, v in sorted(self._voices.items())
                },
            }
