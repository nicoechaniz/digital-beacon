"""Sample-driven modulation: turn descriptors from a SampleLayer into control signals.

Routes ratios to:
- Beacon parameters via OSC to sclang (57120).
- Shaper parameters via VoiceParameterStore.

Mappings are declarative: each descriptor can drive one or more targets with
scale, offset, smoothing, threshold and inversion. This keeps the experiment
visible and tunable.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from pythonosc.udp_client import SimpleUDPClient

from digital_beacon.sample_layer import SampleDescriptor
from digital_beacon.state import VoiceParameterStore

log = logging.getLogger(__name__)


# Valid target parameters per system
BEACON_PARAMS = {"master", "f1", "vsrate", "gain", "az", "dist", "q", "on"}
SHAPER_PARAMS = {"master", "sidechain", "lfo_amount", "gain", "pan", "shape", "lfo_gain", "lfo_pan", "lfo_phase"}

# Valid descriptor names (from SampleLayer + derived ones)
BASE_DESCRIPTORS = {
    "rms", "f0_hz", "f0_ratio", "centroid", "bandwidth", "flatness",
}
DERIVED_DESCRIPTORS = {
    "rms_delta", "rms_smooth", "f0_stability", "centroid_delta", "inharmonicity",
}
BAND_DESCRIPTORS = {f"band_{i}" for i in range(32)}
# Suggested stable ranges for descriptor normalization (per-sample values are
# clamped and then mapped to 0..1). These make presets portable across samples.
DESCRIPTOR_RANGES: Dict[str, Tuple[float, float]] = {
    "rms": (0.0, 0.5),
    "f0_hz": (20.0, 200.0),
    "f0_ratio": (0.5, 4.0),
    "centroid": (20.0, 8000.0),
    "bandwidth": (20.0, 8000.0),
    "flatness": (0.0, 1.0),
    "rms_delta": (-0.2, 0.2),
    "rms_smooth": (0.0, 0.5),
    "f0_stability": (0.0, 1.0),
    "centroid_delta": (-1000.0, 1000.0),
    "inharmonicity": (0.0, 1.0),
}
# Add band_0..31 ranges dynamically
for _i in range(32):
    DESCRIPTOR_RANGES[f"band_{_i}"] = (0.0, 1.0)


VALID_DESCRIPTORS = BASE_DESCRIPTORS | DERIVED_DESCRIPTORS | BAND_DESCRIPTORS


def _normalize_descriptor(name: str, raw: float) -> float:
    """Map a raw descriptor to a 0..1 range using declared (min, max)."""
    lo, hi = DESCRIPTOR_RANGES.get(name, (0.0, 1.0))
    if hi == lo:
        return 0.0
    return max(0.0, min(1.0, (raw - lo) / (hi - lo)))


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
    smooth: float = 0.0   # 0..1, higher = more smoothing (EWMA alpha)
    threshold: float = 0.0  # value below which output is clamped to min_value
    invert: bool = False    # invert normalized value before scaling
    active: bool = True

    # Runtime state (not serialized)
    _smoothed_value: float = field(default=0.0, repr=False)

    def validate(self) -> None:
        if self.descriptor not in VALID_DESCRIPTORS:
            raise ValueError(f"unknown descriptor: {self.descriptor}")
        if self.target_type not in ("beacon", "shaper"):
            raise ValueError(f"target_type must be 'beacon' or 'shaper', got {self.target_type}")
        if self.target_type == "beacon" and self.param not in BEACON_PARAMS:
            raise ValueError(f"unknown beacon param: {self.param}")
        if self.target_type == "shaper" and self.param not in SHAPER_PARAMS:
            raise ValueError(f"unknown shaper param: {self.param}")
        if self.target_type == "beacon" and self.param in ("gain", "az", "dist", "q", "on") and self.band is None:
            raise ValueError(f"beacon param {self.param} requires band")
        if self.target_type == "shaper" and self.param in ("gain", "pan", "shape", "lfo_gain", "lfo_pan", "lfo_phase") and self.voice is None:
            raise ValueError(f"shaper param {self.param} requires voice")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "descriptor": self.descriptor,
            "target_type": self.target_type,
            "param": self.param,
            "voice": self.voice,
            "band": self.band,
            "scale": self.scale,
            "offset": self.offset,
            "min_value": self.min_value,
            "max_value": self.max_value,
            "smooth": self.smooth,
            "threshold": self.threshold,
            "invert": self.invert,
            "active": self.active,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ModulationTarget":
        return cls(
            descriptor=d["descriptor"],
            target_type=d["target_type"],
            param=d["param"],
            voice=d.get("voice"),
            band=d.get("band"),
            scale=float(d.get("scale", 1.0)),
            offset=float(d.get("offset", 0.0)),
            min_value=float(d.get("min_value", 0.0)),
            max_value=float(d.get("max_value", 1.0)),
            smooth=float(d.get("smooth", 0.0)),
            threshold=float(d.get("threshold", 0.0)),
            invert=bool(d.get("invert", False)),
            active=bool(d.get("active", True)),
        )


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
        target.validate()
        with self._lock:
            self.targets.append(target)

    def set_targets(self, targets: List[ModulationTarget]) -> None:
        for t in targets:
            t.validate()
        with self._lock:
            self.targets = targets

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

            # Normalize descriptor to a stable 0..1 range before applying scale
            normalized = _normalize_descriptor(t.descriptor, raw)
            if normalized < t.threshold:
                value = t.min_value
            else:
                if t.invert:
                    normalized = 1.0 - normalized
                value = t.offset + normalized * t.scale

            value = max(t.min_value, min(t.max_value, value))

            # Smoothing: EWMA
            if t.smooth > 0:
                alpha = max(0.0, min(1.0, t.smooth))
                t._smoothed_value = alpha * value + (1.0 - alpha) * t._smoothed_value
                value = t._smoothed_value

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

    def mapping_to_dict(self) -> List[Dict[str, Any]]:
        return [t.to_dict() for t in self.list_targets()]

    def mapping_from_dict(self, data: List[Dict[str, Any]]) -> None:
        targets = [ModulationTarget.from_dict(d) for d in data]
        self.set_targets(targets)

    def default_mapping(self) -> None:
        """Install a sensible default mapping for exploration."""
        self.set_targets([
            # Sample energy -> beacon master gain (0.2 .. 1.5)
            ModulationTarget("rms", "beacon", "master", scale=1.3, offset=0.2, max_value=1.5),
            # Sample f0 ratio -> beacon varispeed (0.25 .. 2.0)
            ModulationTarget("f0_ratio", "beacon", "vsrate", scale=1.75, offset=0.25, min_value=0.25, max_value=2.0),
            # Sample low-band energy -> shaper gain on voice 1
            ModulationTarget("band_0", "shaper", "gain", voice=1, scale=1.0, offset=0.0, max_value=1.0),
            # Sample overall energy -> shaper master gain (0.2 .. 1.0)
            ModulationTarget("rms", "shaper", "master", scale=0.8, offset=0.2, max_value=1.0),
        ])
        log.info("SampleModulator default mapping installed")

    def preset_mapping(self, name: str) -> None:
        """Install a named preset mapping."""
        presets = {
            "tune-to-sample": [
                ModulationTarget("f0_hz", "beacon", "f1", scale=180.0, offset=20.0, min_value=20.0, max_value=200.0, smooth=0.9),
                ModulationTarget("f0_stability", "beacon", "vsrate", scale=0.2, offset=0.9, min_value=0.9, max_value=1.1, smooth=0.95),
                ModulationTarget("inharmonicity", "beacon", "q", band=1, scale=2.5, offset=0.5, max_value=3.0, invert=True, smooth=0.9),
                ModulationTarget("rms", "beacon", "master", scale=1.3, offset=0.2, max_value=1.5, smooth=0.8),
                ModulationTarget("rms", "shaper", "master", scale=0.8, offset=0.2, max_value=1.0, smooth=0.8),
            ],
            "spectrum-projection": [
                ModulationTarget("band_0", "beacon", "gain", band=1, scale=1.5, offset=0.0, max_value=1.5, smooth=0.8),
                ModulationTarget("band_1", "beacon", "gain", band=7, scale=1.5, offset=0.0, max_value=1.5, smooth=0.8),
                ModulationTarget("band_2", "beacon", "gain", band=14, scale=1.5, offset=0.0, max_value=1.5, smooth=0.8),
                ModulationTarget("rms", "shaper", "master", scale=0.8, offset=0.2, max_value=1.0),
            ],
            "timbre-filter": [
                ModulationTarget("centroid", "shaper", "shape", voice=1, scale=1.0, offset=0.0, max_value=1.0, smooth=0.9),
                ModulationTarget("flatness", "beacon", "q", band=1, scale=1.5, offset=0.5, max_value=2.0, smooth=0.9),
                ModulationTarget("rms", "beacon", "dist", band=1, scale=10.0, offset=0.0, max_value=10.0, smooth=0.8),
            ],
            "rhythmic-pump": [
                ModulationTarget("rms", "shaper", "lfo_amount", scale=1.0, offset=0.0, max_value=1.0, smooth=0.7),
                ModulationTarget("rms", "beacon", "master", scale=1.3, offset=0.2, max_value=1.5, smooth=0.7),
                ModulationTarget("rms_delta", "shaper", "gain", voice=7, scale=1.0, offset=0.0, max_value=1.0, threshold=0.2),
            ],
            "phase-manifold-tune": [
                ModulationTarget("f0_hz", "beacon", "f1", scale=180.0, offset=20.0, min_value=20.0, max_value=200.0, smooth=0.9),
                ModulationTarget("f0_stability", "beacon", "vsrate", scale=0.2, offset=0.9, min_value=0.9, max_value=1.1, smooth=0.95),
                ModulationTarget("inharmonicity", "beacon", "q", band=1, scale=2.5, offset=0.5, max_value=3.0, invert=True, smooth=0.9),
                ModulationTarget("rms", "beacon", "master", scale=1.3, offset=0.2, max_value=1.5, smooth=0.8),
                ModulationTarget("rms", "shaper", "master", scale=0.8, offset=0.2, max_value=1.0, smooth=0.8),
            ] + [
                ModulationTarget(f"band_{i}", "beacon", "gain", band=i+1, scale=1.5, offset=0.0, max_value=1.5, smooth=0.8)
                for i in range(32)
            ],
        }
        if name not in presets:
            raise ValueError(f"unknown preset: {name}")
        self.set_targets(presets[name])
        log.info("SampleModulator preset mapping installed: %s", name)
