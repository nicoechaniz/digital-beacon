"""Processor definitions for the processing chain.

Each processor type has a canonical parameter schema so downstream
renderers know what to expect.

Phase 5: HarmonicCombProcessor, BinauralSpatializer, FilterProcessor,
         DynamicsProcessor.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from typing import Any, Dict, List, Optional


# ── Harmonic Comb Filter ──────────────────────────────────────────────────────

@dataclass
class HarmonicCombParams:
    """Parameters for a harmonic comb filter — tuned to F1 harmonics."""
    bandwidth: float = 0.5  # fractional bandwidth (0.0–1.0)
    q_factor: float = 1.0   # filter resonance
    wet_dry: float = 1.0    # 0.0 = dry, 1.0 = full comb
    residual: bool = False  # output the residual (what the comb removes)
    num_harmonics: int = 32
    pre_gain: float = 1.0
    post_gain: float = 1.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "bandwidth": self.bandwidth,
            "q_factor": self.q_factor,
            "wet_dry": self.wet_dry,
            "residual": self.residual,
            "num_harmonics": self.num_harmonics,
            "pre_gain": self.pre_gain,
            "post_gain": self.post_gain,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "HarmonicCombParams":
        return cls(
            bandwidth=d.get("bandwidth", 0.5),
            q_factor=d.get("q_factor", 1.0),
            wet_dry=d.get("wet_dry", 1.0),
            residual=d.get("residual", False),
            num_harmonics=d.get("num_harmonics", 32),
            pre_gain=d.get("pre_gain", 1.0),
            post_gain=d.get("post_gain", 1.0),
        )


# ── Binaural Spatializer ──────────────────────────────────────────────────────

@dataclass
class SpatialBand:
    """Per-band spatial parameters (13-band variant)."""
    band_index: int
    azimuth: float = 0.0    # degrees
    distance: float = 1.0   # normalized
    q: float = 0.5          # filter Q
    gain: float = 1.0       # band gain
    enabled: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "band_index": self.band_index,
            "azimuth": self.azimuth,
            "distance": self.distance,
            "q": self.q,
            "gain": self.gain,
            "enabled": self.enabled,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "SpatialBand":
        return cls(
            band_index=d["band_index"],
            azimuth=d.get("azimuth", 0.0),
            distance=d.get("distance", 1.0),
            q=d.get("q", 0.5),
            gain=d.get("gain", 1.0),
            enabled=d.get("enabled", True),
        )


@dataclass
class BinauralSpatializerParams:
    """13-band binaural spatializer parameters."""
    bands: List[SpatialBand] = dc_field(default_factory=list)
    hrtf_profile: str = "listen"  # listen | kemar | custom
    head_radius: float = 0.0875   # meters
    master_gain: float = 1.0
    rotation: float = 0.0         # global azimuth rotation (degrees)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "bands": [b.to_dict() for b in self.bands],
            "hrtf_profile": self.hrtf_profile,
            "head_radius": self.head_radius,
            "master_gain": self.master_gain,
            "rotation": self.rotation,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "BinauralSpatializerParams":
        return cls(
            bands=[SpatialBand.from_dict(b) for b in d.get("bands", [])],
            hrtf_profile=d.get("hrtf_profile", "listen"),
            head_radius=d.get("head_radius", 0.0875),
            master_gain=d.get("master_gain", 1.0),
            rotation=d.get("rotation", 0.0),
        )


# ── Filter Processor ──────────────────────────────────────────────────────────

@dataclass
class FilterParams:
    """Generic filter processor (lowpass, highpass, bandpass, etc.)."""
    filter_type: str = "lowpass"  # lowpass | highpass | bandpass | notch
    cutoff_hz: float = 1000.0
    q: float = 0.707  # Butterworth default
    gain_db: float = 0.0
    order: int = 2

    def to_dict(self) -> Dict[str, Any]:
        return {
            "filter_type": self.filter_type,
            "cutoff_hz": self.cutoff_hz,
            "q": self.q,
            "gain_db": self.gain_db,
            "order": self.order,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "FilterParams":
        return cls(
            filter_type=d.get("filter_type", "lowpass"),
            cutoff_hz=d.get("cutoff_hz", 1000.0),
            q=d.get("q", 0.707),
            gain_db=d.get("gain_db", 0.0),
            order=d.get("order", 2),
        )


# ── Dynamics Processor ────────────────────────────────────────────────────────

@dataclass
class DynamicsParams:
    """Dynamics processor (compressor, limiter, expander)."""
    mode: str = "compressor"  # compressor | limiter | expander | gate
    threshold_db: float = -20.0
    ratio: float = 4.0
    attack_ms: float = 5.0
    release_ms: float = 50.0
    knee_db: float = 6.0
    makeup_gain_db: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "threshold_db": self.threshold_db,
            "ratio": self.ratio,
            "attack_ms": self.attack_ms,
            "release_ms": self.release_ms,
            "knee_db": self.knee_db,
            "makeup_gain_db": self.makeup_gain_db,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "DynamicsParams":
        return cls(
            mode=d.get("mode", "compressor"),
            threshold_db=d.get("threshold_db", -20.0),
            ratio=d.get("ratio", 4.0),
            attack_ms=d.get("attack_ms", 5.0),
            release_ms=d.get("release_ms", 50.0),
            knee_db=d.get("knee_db", 6.0),
            makeup_gain_db=d.get("makeup_gain_db", 0.0),
        )
