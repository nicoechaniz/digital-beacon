"""Sample-driven modulation: turn descriptors from a SampleLayer into control signals.

Routes ratios to:
- Beacon parameters via OSC to sclang (57120).
- Shaper parameters via VoiceParameterStore.

Mappings are explicit: each descriptor can drive one or more targets with a scale
and offset. This keeps the experiment visible and tunable.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from pythonosc.udp_client import SimpleUDPClient

from digital_beacon.sample_layer import SampleDescriptor
from digital_beacon.state import VoiceParameterStore

log = logging.getLogger(__name__)


@dataclass
class ModulationTarget:
    """A mapping from a descriptor field to a beacon or shaper parameter."""

    descriptor: str       # e.g. "rms", "f0_ratio", "band_0"
    target_type: str      # "beacon" or "shaper"
    param: str            # e.g. "master", "f1", "gain", "pan", "shape"
    voice: Optional[int] = None  # for per-voice shaper params
    band: Optional[int] = None   # for per-band beacon params
    scale: float = 1.0
    offset: float = 0.0
    min_value: float = 0.0
    max_value: float = 1.0
    active: bool = True


class SampleModulator:
    """Apply descriptor frames to beacon and shaper targets."""

    # OSC addresses for beacon parameters (sclang expects these on 57120)
    BEACON_OSC = {
        "master": "/beacon/master",
        "f1": "/beacon/f1",
        "vsrate": "/beacon/vsource",
    }

    def __init__(
        self,
        store: VoiceParameterStore,
        sc_host: str = "127.0.0.1",
        sc_port: int = 57120,
    ):
        self.store = store
        self.sc_osc = SimpleUDPClient(sc_host, sc_port)
        self.targets: List[ModulationTarget] = []
        self._lock = threading.Lock()

    def add_target(self, target: ModulationTarget) -> None:
        with self._lock:
            self.targets.append(target)

    def remove_targets(self, descriptor: Optional[str] = None) -> None:
        with self._lock:
            if descriptor is None:
                self.targets.clear()
            else:
                self.targets = [t for t in self.targets if t.descriptor != descriptor]

    def on_descriptor(self, desc: SampleDescriptor) -> None:
        """Called by SampleLayer for every analyzed chunk."""
        values = desc.to_dict()
        with self._lock:
            targets = list(self.targets)

        for t in targets:
            if not t.active or t.descriptor not in values:
                continue
            raw = float(values[t.descriptor])
            value = t.offset + raw * t.scale
            value = max(t.min_value, min(t.max_value, value))
            self._apply(t, value)

    def _apply(self, t: ModulationTarget, value: float) -> None:
        if t.target_type == "beacon":
            self._apply_beacon(t, value)
        elif t.target_type == "shaper":
            self._apply_shaper(t, value)

    def _apply_beacon(self, t: ModulationTarget, value: float) -> None:
        if t.param == "master":
            self.sc_osc.send_message("/beacon/master", [value])
        elif t.param == "f1":
            self.sc_osc.send_message("/beacon/f1", [value])
            self.store.update_f1(value)
        elif t.param == "vsrate":
            self.sc_osc.send_message("/beacon/vsource", [value])
            self.store.set_vsrate(value)
        elif t.band is not None and t.param in ("gain", "az", "dist", "q", "on"):
            self.sc_osc.send_message(f"/beacon/{t.param}/{t.band}", [value])
        else:
            log.debug("unknown beacon target: %s", t.param)

    def _apply_shaper(self, t: ModulationTarget, value: float) -> None:
        if t.param == "master":
            self.store.set_master_gain(value)
        elif t.param == "sidechain":
            self.store.set_sidechain_amount(value)
        elif t.param == "lfo_amount":
            self.store.set_lfo_amount(value)
        elif t.voice is not None and t.param in ("gain", "pan", "shape", "lfo_gain", "lfo_pan", "lfo_phase"):
            fn = getattr(self.store, f"set_{t.param}", None)
            if fn is not None:
                fn(t.voice, value)
        else:
            log.debug("unknown shaper target: %s", t.param)

    def list_targets(self) -> List[ModulationTarget]:
        with self._lock:
            return list(self.targets)

    def default_mapping(self, sample_path: Optional[str] = None) -> None:
        """Install a sensible default mapping for exploration."""
        with self._lock:
            self.targets = [
                # Sample energy -> beacon master gain (ducking-like)
                ModulationTarget("rms", "beacon", "master", scale=2.0, offset=0.2, max_value=1.5),
                # Sample f0 ratio -> beacon varispeed (slow down / speed up)
                ModulationTarget("f0_ratio", "beacon", "vsrate", scale=0.2, offset=1.0, min_value=0.25, max_value=2.0),
                # Sample low-band energy -> shaper gain on voice 1
                ModulationTarget("band_0", "shaper", "gain", voice=1, scale=0.05, offset=0.0, max_value=1.0),
                # Sample overall energy -> shaper master gain
                ModulationTarget("rms", "shaper", "master", scale=1.0, offset=0.2, max_value=1.0),
            ]
        log.info("SampleModulator default mapping installed")
