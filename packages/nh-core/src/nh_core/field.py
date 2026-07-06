"""Core data classes for the NaturalHarmony harmonic field."""
from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from typing import Any, Dict, Optional


@dataclass
class Partial:
    """A single harmonic partial."""
    n: int
    freq: Optional[float] = None  # explicit override; default is n * f1
    gain: float = 1.0
    phase: float = 0.0
    width: float = 0.0
    pan: float = 0.0
    spatial: Optional[Dict[str, Any]] = None  # e.g. {"az": 0.0, "dist": 1.0, "q": 0.01}
    envelope: Optional[Dict[str, Any]] = None  # attack/decay/release shape

    def effective_freq(self, f1: float) -> float:
        if self.freq is not None:
            return self.freq
        return f1 * self.n


@dataclass
class Residual:
    """Residual / noise component. Can be an audio buffer reference or parametric."""
    kind: str = "none"  # none | audio | parametric
    audio_path: Optional[str] = None
    params: Optional[Dict[str, Any]] = None


@dataclass
class Transport:
    """Clock and transport state."""
    clock: float = 0.0  # seconds
    playing: bool = True
    seeking: bool = False
    loop_start: Optional[float] = None
    loop_end: Optional[float] = None


@dataclass
class HarmonicField:
    """Renderer-neutral time-varying harmonic field."""
    f1: float = 65.0
    partials: Dict[int, Partial] = dc_field(default_factory=dict)
    residual: Residual = dc_field(default_factory=Residual)
    descriptors: Optional[Dict[str, Any]] = None
    modulations: Optional[Dict[str, Any]] = None
    transport: Transport = dc_field(default_factory=Transport)

    def sorted_partials(self):
        return sorted(self.partials.values(), key=lambda p: p.n)

    def project_to_capabilities(self, caps, *, keep_explicit_freq: bool = True) -> "HarmonicField":
        """Return a copy limited to the renderer capabilities.

        Loses partials beyond max_partials. Drops spatial/pan if not supported.
        Drops phase if not supported. Drops residual if not supported.
        """
        from copy import deepcopy

        new = HarmonicField(
            f1=self.f1,
            residual=deepcopy(self.residual) if caps.supports_residual else Residual(kind="none"),
            descriptors=deepcopy(self.descriptors),
            modulations=deepcopy(self.modulations),
            transport=deepcopy(self.transport),
        )
        for n, p in sorted(self.partials.items()):
            if n > caps.max_partials:
                continue
            new_p = deepcopy(p)
            if not caps.supports_phase:
                new_p.phase = 0.0
            if not caps.supports_spatial:
                new_p.pan = 0.0
                new_p.spatial = None
            new.partials[n] = new_p
        return new

    def to_dict(self) -> Dict[str, Any]:
        return {
            "f1": self.f1,
            "partials": {str(n): {"n": p.n, "freq": p.freq, "gain": p.gain,
                                   "phase": p.phase, "width": p.width, "pan": p.pan,
                                   "spatial": p.spatial, "envelope": p.envelope}
                         for n, p in self.partials.items()},
            "residual": {"kind": self.residual.kind, "audio_path": self.residual.audio_path,
                         "params": self.residual.params},
            "descriptors": self.descriptors,
            "modulations": self.modulations,
            "transport": {"clock": self.transport.clock, "playing": self.transport.playing,
                          "seeking": self.transport.seeking, "loop_start": self.transport.loop_start,
                          "loop_end": self.transport.loop_end},
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "HarmonicField":
        partials = {}
        for k, v in d.get("partials", {}).items():
            partials[int(k)] = Partial(
                n=v["n"], freq=v.get("freq"), gain=v.get("gain", 1.0),
                phase=v.get("phase", 0.0), width=v.get("width", 0.0),
                pan=v.get("pan", 0.0), spatial=v.get("spatial"),
                envelope=v.get("envelope"),
            )
        residual = d.get("residual", {})
        return cls(
            f1=d.get("f1", 65.0),
            partials=partials,
            residual=Residual(
                kind=residual.get("kind", "none"),
                audio_path=residual.get("audio_path"),
                params=residual.get("params"),
            ),
            descriptors=d.get("descriptors"),
            modulations=d.get("modulations"),
            transport=Transport(
                clock=d.get("transport", {}).get("clock", 0.0),
                playing=d.get("transport", {}).get("playing", True),
                seeking=d.get("transport", {}).get("seeking", False),
                loop_start=d.get("transport", {}).get("loop_start"),
                loop_end=d.get("transport", {}).get("loop_end"),
            ),
        )
