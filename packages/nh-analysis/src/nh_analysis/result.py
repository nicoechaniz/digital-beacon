"""AnalysisResult — complete audio analysis data model (Phase 6).

Combines F0 tracking, harmonicity, spectral metrics, Phideus descriptors,
emotion classification, and speaker recognition into a single result object
that can be serialized, cached, and served via /nh/v1/analyze.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from typing import Any, Dict, List, Optional


@dataclass
class F0Track:
    """Frame-level F0 estimate."""
    times: List[float] = dc_field(default_factory=list)
    f0_hz: List[float] = dc_field(default_factory=list)
    voiced: List[bool] = dc_field(default_factory=list)
    confidence: List[float] = dc_field(default_factory=list)

    # Aggregates.
    f0_mean: float = 0.0
    f0_std: float = 0.0
    f0_median: float = 0.0
    voiced_fraction: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "times": self.times,
            "f0_hz": self.f0_hz,
            "voiced": self.voiced,
            "confidence": self.confidence,
            "f0_mean": self.f0_mean,
            "f0_std": self.f0_std,
            "f0_median": self.f0_median,
            "voiced_fraction": self.voiced_fraction,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "F0Track":
        return cls(
            times=d.get("times", []),
            f0_hz=d.get("f0_hz", []),
            voiced=d.get("voiced", []),
            confidence=d.get("confidence", []),
            f0_mean=d.get("f0_mean", 0.0),
            f0_std=d.get("f0_std", 0.0),
            f0_median=d.get("f0_median", 0.0),
            voiced_fraction=d.get("voiced_fraction", 0.0),
        )


@dataclass
class SpectralMetrics:
    """Aggregate spectral measurements."""
    centroid_hz: float = 0.0
    spread_hz: float = 0.0
    skewness: float = 0.0
    kurtosis: float = 0.0
    rolloff_hz: float = 0.0
    flux: float = 0.0
    crest_factor: float = 0.0
    rms_db: float = -60.0
    peak_db: float = -60.0
    dynamic_range_db: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "centroid_hz": self.centroid_hz,
            "spread_hz": self.spread_hz,
            "skewness": self.skewness,
            "kurtosis": self.kurtosis,
            "rolloff_hz": self.rolloff_hz,
            "flux": self.flux,
            "crest_factor": self.crest_factor,
            "rms_db": self.rms_db,
            "peak_db": self.peak_db,
            "dynamic_range_db": self.dynamic_range_db,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "SpectralMetrics":
        return cls(**{k: d.get(k, 0.0) for k in cls.__dataclass_fields__})


@dataclass
class PhideusDescriptors:
    """Frame-level Phideus descriptors.

    H-series (8-dim): log(Hn/H1) for n=2..6, harmonic concentration,
    harmonic deviation, voicing quality.
    V4: 4-dim linear and log variants.
    A4-16k: 4-dim aggregate.
    """
    h_series: Optional[Dict[str, Any]] = None  # h2_h1, h3_h1, ... concentration, deviation, voicing
    v4_linear: Optional[Dict[str, Any]] = None
    v4_log: Optional[Dict[str, Any]] = None
    a4_16k: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "h_series": self.h_series,
            "v4_linear": self.v4_linear,
            "v4_log": self.v4_log,
            "a4_16k": self.a4_16k,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "PhideusDescriptors":
        return cls(
            h_series=d.get("h_series"),
            v4_linear=d.get("v4_linear"),
            v4_log=d.get("v4_log"),
            a4_16k=d.get("a4_16k"),
        )


@dataclass
class EmotionResult:
    """Emotion classification from voice analysis."""
    primary: str = "neutral"
    confidence: float = 0.0
    valence: float = 0.0
    arousal: float = 0.0
    dominance: float = 0.0
    all_scores: Dict[str, float] = dc_field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "primary": self.primary,
            "confidence": self.confidence,
            "valence": self.valence,
            "arousal": self.arousal,
            "dominance": self.dominance,
            "all_scores": self.all_scores,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "EmotionResult":
        return cls(
            primary=d.get("primary", "neutral"),
            confidence=d.get("confidence", 0.0),
            valence=d.get("valence", 0.0),
            arousal=d.get("arousal", 0.0),
            dominance=d.get("dominance", 0.0),
            all_scores=d.get("all_scores", {}),
        )


@dataclass
class SpeakerResult:
    """Speaker recognition / diarization result."""
    speaker_id: str = "unknown"
    confidence: float = 0.0
    embedding: Optional[List[float]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "speaker_id": self.speaker_id,
            "confidence": self.confidence,
            "embedding": self.embedding,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "SpeakerResult":
        return cls(
            speaker_id=d.get("speaker_id", "unknown"),
            confidence=d.get("confidence", 0.0),
            embedding=d.get("embedding"),
        )


@dataclass
class AnalysisResult:
    """Complete audio analysis result.

    Returned by /nh/v1/analyze. All fields optional — a partial analysis
    is valid (e.g. F0 only, no emotion).
    """
    audio_path: str = ""
    duration_s: float = 0.0
    sample_rate: float = 44100.0
    channels: int = 1

    f0_track: Optional[F0Track] = None
    spectral: Optional[SpectralMetrics] = None
    harmonicity: Optional[Dict[str, Any]] = None  # from harmonicity_score
    phideus: Optional[PhideusDescriptors] = None
    emotion: Optional[EmotionResult] = None
    speaker: Optional[SpeakerResult] = None

    # Proposed beacon tuning.
    proposed_f1: Optional[float] = None
    proposed_bands: Optional[Dict[int, Dict[str, Any]]] = None

    # Cache / sidecar.
    analysis_version: str = "1.0"
    computed_at: str = ""  # ISO 8601
    cache_key: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "audio_path": self.audio_path,
            "duration_s": self.duration_s,
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "f0_track": self.f0_track.to_dict() if self.f0_track else None,
            "spectral": self.spectral.to_dict() if self.spectral else None,
            "harmonicity": self.harmonicity,
            "phideus": self.phideus.to_dict() if self.phideus else None,
            "emotion": self.emotion.to_dict() if self.emotion else None,
            "speaker": self.speaker.to_dict() if self.speaker else None,
            "proposed_f1": self.proposed_f1,
            "proposed_bands": self.proposed_bands,
            "analysis_version": self.analysis_version,
            "computed_at": self.computed_at,
            "cache_key": self.cache_key,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "AnalysisResult":
        return cls(
            audio_path=d.get("audio_path", ""),
            duration_s=d.get("duration_s", 0.0),
            sample_rate=d.get("sample_rate", 44100.0),
            channels=d.get("channels", 1),
            f0_track=F0Track.from_dict(d["f0_track"]) if d.get("f0_track") else None,
            spectral=SpectralMetrics.from_dict(d["spectral"]) if d.get("spectral") else None,
            harmonicity=d.get("harmonicity"),
            phideus=PhideusDescriptors.from_dict(d["phideus"]) if d.get("phideus") else None,
            emotion=EmotionResult.from_dict(d["emotion"]) if d.get("emotion") else None,
            speaker=SpeakerResult.from_dict(d["speaker"]) if d.get("speaker") else None,
            proposed_f1=d.get("proposed_f1"),
            proposed_bands=d.get("proposed_bands"),
            analysis_version=d.get("analysis_version", "1.0"),
            computed_at=d.get("computed_at", ""),
            cache_key=d.get("cache_key"),
        )
