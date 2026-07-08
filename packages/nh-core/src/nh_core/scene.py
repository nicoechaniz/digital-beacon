"""Harmonic Scene v2 — multi-source scene schema for NaturalHarmony.

A HarmonicScene replaces the flat HarmonicField with independent named sources
(beacon drone, shaper additive synth, sample playback, voice input), a processing
chain, and modulation routes.

CONTRACT: Sources are independent — pads/Launchpad only affect ShaperSource,
never BeaconSource. ProcessingChain is a directed graph of processors with
per-source routing.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from typing import Any, Dict, List, Optional


# ── Sources ───────────────────────────────────────────────────────────────────

@dataclass
class SpatialBand:
    """Per-band spatial parameters for a single harmonic index."""
    az: float = 0.0
    dist: float = 1.0
    q: float = 0.5
    on: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {"az": self.az, "dist": self.dist, "q": self.q, "on": self.on}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "SpatialBand":
        return cls(
            az=d.get("az", 0.0),
            dist=d.get("dist", 1.0),
            q=d.get("q", 0.5),
            on=d.get("on", True),
        )


@dataclass
class BeaconSource:
    """Continuous harmonic drone — the beacon itself."""
    source_id: str
    f1: float = 65.0
    vsrate: float = 1.0
    bands: Dict[int, SpatialBand] = dc_field(default_factory=dict)
    master_gain: float = 0.8
    audio_path: Optional[str] = None  # for file-based varispeed beacon
    kind: str = "beacon"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "source_id": self.source_id,
            "f1": self.f1,
            "vsrate": self.vsrate,
            "master_gain": self.master_gain,
            "audio_path": self.audio_path,
            "bands": {str(n): b.to_dict() for n, b in self.bands.items()},
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "BeaconSource":
        bands = {}
        for k, v in d.get("bands", {}).items():
            bands[int(k)] = SpatialBand.from_dict(v)
        return cls(
            source_id=d["source_id"],
            f1=d.get("f1", 65.0),
            vsrate=d.get("vsrate", 1.0),
            bands=bands,
            master_gain=d.get("master_gain", 0.8),
            audio_path=d.get("audio_path"),
        )


@dataclass
class ShaperVoice:
    """A single additive voice in the shaper synth."""
    n: int
    gain: float = 0.6
    pan: float = 0.0
    phase: float = 0.0
    envelope: Optional[Dict[str, Any]] = None  # attack_s, release_s, shape
    active: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "n": self.n,
            "gain": self.gain,
            "pan": self.pan,
            "phase": self.phase,
            "envelope": self.envelope,
            "active": self.active,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ShaperVoice":
        return cls(
            n=d["n"],
            gain=d.get("gain", 0.6),
            pan=d.get("pan", 0.0),
            phase=d.get("phase", 0.0),
            envelope=d.get("envelope"),
            active=d.get("active", False),
        )


@dataclass
class ShaperSource:
    """Additive synthesizer — pure sine harmonics, per-voice control."""
    source_id: str
    voices: Dict[int, ShaperVoice] = dc_field(default_factory=dict)
    master_gain: float = 0.5
    max_voices: int = 32
    polyphony_mode: str = "steal"  # steal | allocate | mono
    kind: str = "shaper"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "source_id": self.source_id,
            "master_gain": self.master_gain,
            "max_voices": self.max_voices,
            "polyphony_mode": self.polyphony_mode,
            "voices": {str(n): v.to_dict() for n, v in self.voices.items()},
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ShaperSource":
        voices = {}
        for k, v in d.get("voices", {}).items():
            voices[int(k)] = ShaperVoice.from_dict(v)
        return cls(
            source_id=d["source_id"],
            voices=voices,
            master_gain=d.get("master_gain", 0.5),
            max_voices=d.get("max_voices", 32),
            polyphony_mode=d.get("polyphony_mode", "steal"),
        )


@dataclass
class SampleSource:
    """Pre-recorded audio sample — playback with loop/one-shot."""
    source_id: str
    audio_path: str
    loop: bool = False
    one_shot: bool = False
    gain: float = 1.0
    f1_override: Optional[float] = None  # if set, retune beacon to this F1
    kind: str = "sample"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "source_id": self.source_id,
            "audio_path": self.audio_path,
            "loop": self.loop,
            "one_shot": self.one_shot,
            "gain": self.gain,
            "f1_override": self.f1_override,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "SampleSource":
        return cls(
            source_id=d["source_id"],
            audio_path=d["audio_path"],
            loop=d.get("loop", False),
            one_shot=d.get("one_shot", False),
            gain=d.get("gain", 1.0),
            f1_override=d.get("f1_override"),
        )


@dataclass
class VoiceSource:
    """Live microphone input with optional processing."""
    source_id: str
    input_device: Optional[str] = None
    gain: float = 1.0
    kind: str = "voice"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "source_id": self.source_id,
            "input_device": self.input_device,
            "gain": self.gain,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "VoiceSource":
        return cls(
            source_id=d["source_id"],
            input_device=d.get("input_device"),
            gain=d.get("gain", 1.0),
        )


# Union type for source variants.
Source = BeaconSource | ShaperSource | SampleSource | VoiceSource


# ── Processing Chain ───────────────────────────────────────────────────────────

@dataclass
class ProcessorState:
    """A single processor in the chain."""
    processor_id: str
    processor_type: str  # harmonic_comb | binaural_spatializer | filter | dynamics
    params: Dict[str, Any] = dc_field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "processor_id": self.processor_id,
            "processor_type": self.processor_type,
            "params": self.params,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ProcessorState":
        return cls(
            processor_id=d["processor_id"],
            processor_type=d["processor_type"],
            params=d.get("params", {}),
        )


@dataclass
class ProcessingChain:
    """Ordered processors with per-source routing."""
    processors: List[ProcessorState] = dc_field(default_factory=list)
    routing: Dict[str, List[str]] = dc_field(
        default_factory=dict
    )  # source_id -> [processor_ids]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "processors": [p.to_dict() for p in self.processors],
            "routing": self.routing,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ProcessingChain":
        return cls(
            processors=[ProcessorState.from_dict(p) for p in d.get("processors", [])],
            routing=d.get("routing", {}),
        )


# ── Modulation ─────────────────────────────────────────────────────────────────

@dataclass
class LFOState:
    """Low-frequency oscillator state."""
    lfo_id: str
    waveform: str = "sine"  # sine | triangle | saw | square | sample_hold
    rate_hz: Optional[float] = None
    strum_divisor: Optional[int] = None  # rate = f1 / strum_divisor
    depth: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "lfo_id": self.lfo_id,
            "waveform": self.waveform,
            "rate_hz": self.rate_hz,
            "strum_divisor": self.strum_divisor,
            "depth": self.depth,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "LFOState":
        return cls(
            lfo_id=d["lfo_id"],
            waveform=d.get("waveform", "sine"),
            rate_hz=d.get("rate_hz"),
            strum_divisor=d.get("strum_divisor"),
            depth=d.get("depth", 0.0),
        )


@dataclass
class ModulationRoute:
    """Route a modulation source to a path-targeted parameter."""
    route_id: str
    source: str  # lfo_id | sensor_id
    target_path: str  # e.g. "sources.beacon.f1"
    scale: float = 1.0
    offset: float = 0.0
    range_min: Optional[float] = None
    range_max: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "route_id": self.route_id,
            "source": self.source,
            "target_path": self.target_path,
            "scale": self.scale,
            "offset": self.offset,
            "range_min": self.range_min,
            "range_max": self.range_max,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ModulationRoute":
        return cls(
            route_id=d["route_id"],
            source=d["source"],
            target_path=d["target_path"],
            scale=d.get("scale", 1.0),
            offset=d.get("offset", 0.0),
            range_min=d.get("range_min"),
            range_max=d.get("range_max"),
        )


# ── HarmonicScene ──────────────────────────────────────────────────────────────

# Discriminator map for source deserialization.
_SOURCE_KIND_MAP = {
    "beacon": BeaconSource,
    "shaper": ShaperSource,
    "sample": SampleSource,
    "voice": VoiceSource,
}


@dataclass
class HarmonicScene:
    """Multi-source harmonic scene — the v2 preset model.

    A scene contains independent named sources (beacon, shaper, samples, voice),
    a shared processing chain, and modulation routes. This replaces the flat
    HarmonicField for v2 presets.
    """
    version: str = "2"
    sources: Dict[str, Source] = dc_field(default_factory=dict)
    processing_chain: ProcessingChain = dc_field(default_factory=ProcessingChain)
    lfos: Dict[str, LFOState] = dc_field(default_factory=dict)
    modulations: Dict[str, ModulationRoute] = dc_field(default_factory=dict)
    metadata: Dict[str, Any] = dc_field(default_factory=dict)

    # ── helpers ──────────────────────────────────────────────────────────────

    def get_source(self, source_id: str) -> Optional[Source]:
        return self.sources.get(source_id)

    def source_ids(self) -> List[str]:
        return list(self.sources.keys())

    def source_of_kind(self, kind: str) -> List[Source]:
        return [s for s in self.sources.values() if getattr(s, "kind", None) == kind]

    # ── serialization ────────────────────────────────────────────────────────

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "sources": {sid: src.to_dict() for sid, src in self.sources.items()},
            "processing_chain": self.processing_chain.to_dict(),
            "lfos": {lid: lfo.to_dict() for lid, lfo in self.lfos.items()},
            "modulations": {rid: r.to_dict() for rid, r in self.modulations.items()},
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "HarmonicScene":
        sources = {}
        for sid, sd in d.get("sources", {}).items():
            kind = sd.get("kind", "beacon")
            factory = _SOURCE_KIND_MAP.get(kind)
            if factory is not None:
                sources[sid] = factory.from_dict(sd)

        lfos = {}
        for lid, ld in d.get("lfos", {}).items():
            lfos[lid] = LFOState.from_dict(ld)

        modulations = {}
        for rid, rd in d.get("modulations", {}).items():
            modulations[rid] = ModulationRoute.from_dict(rd)

        return cls(
            version=d.get("version", "2"),
            sources=sources,
            processing_chain=ProcessingChain.from_dict(d.get("processing_chain", {})),
            lfos=lfos,
            modulations=modulations,
            metadata=d.get("metadata", {}),
        )

    # ── projection: scene -> base_field (compat) ─────────────────────────────

    def project_to_base_field(self) -> "HarmonicField":  # noqa: F821
        """Lossy projection for v1 compatibility.

        Merges all sources into a single HarmonicField. Beacon bands form the
        base; shaper active voices override gains. Samples and voice are
        dropped (they have no v1 representation).
        """
        from nh_core.field import HarmonicField as HF, Partial, Residual, Transport

        # Find the first beacon source for F1.
        beacons = [s for s in self.sources.values() if isinstance(s, BeaconSource)]
        f1 = beacons[0].f1 if beacons else 65.0

        field = HF(f1=f1)

        # Phase 1: copy beacon bands as base.
        for beacon in beacons:
            for n, band in beacon.bands.items():
                field.partials[n] = Partial(
                    n=n,
                    gain=band.on * beacon.master_gain if band.on else 0.0,
                    spatial={
                        "az": band.az,
                        "dist": band.dist,
                        "q": band.q,
                        "on": band.on,
                    },
                )

        # Phase 2: overlay shaper active voices.
        shapers = [s for s in self.sources.values() if isinstance(s, ShaperSource)]
        for shaper in shapers:
            for n, voice in shaper.voices.items():
                if not voice.active:
                    continue
                if n in field.partials:
                    existing = field.partials[n]
                    existing.spatial = existing.spatial or {}
                    existing.spatial["beacon_gain"] = existing.gain
                    existing.gain = voice.gain
                    existing.pan = voice.pan
                    existing.phase = voice.phase
                    existing.spatial["active"] = True
                    existing.envelope = voice.envelope
                else:
                    field.partials[n] = Partial(
                        n=n,
                        gain=voice.gain,
                        pan=voice.pan,
                        phase=voice.phase,
                        envelope=voice.envelope,
                    )

        return field
