"""Preset v2 — multi-source scene preset schema.

Preset v2 wraps a HarmonicScene. Migrations from v1 and legacy formats
decompose the flat HarmonicField into independent sources.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field as dc_field
from typing import Any, Dict, List, Optional

from nh_core import (
    HarmonicField,
    HarmonicScene,
    BeaconSource,
    ShaperSource,
    ShaperVoice,
    SampleSource,
    VoiceSource,
    SpatialBand,
    ProcessingChain,
    RendererCapabilities,
)


SCENE_VERSION = "2"


@dataclass
class PresetV2:
    """Canonical v2 preset wrapper around a HarmonicScene."""
    version: str = SCENE_VERSION
    scene: HarmonicScene = dc_field(default_factory=HarmonicScene)
    renderer_capabilities_required: Optional[RendererCapabilities] = None
    metadata: Dict[str, Any] = dc_field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "scene": self.scene.to_dict(),
            "renderer_capabilities_required": (
                self.renderer_capabilities_required.to_dict()
                if self.renderer_capabilities_required else None
            ),
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "PresetV2":
        caps = d.get("renderer_capabilities_required")
        return cls(
            version=d.get("version", SCENE_VERSION),
            scene=HarmonicScene.from_dict(d.get("scene", {})),
            renderer_capabilities_required=RendererCapabilities.from_dict(caps) if caps else None,
            metadata=d.get("metadata", {}),
        )

    def project_to_v1(self) -> "PresetV1":  # noqa: F821
        """Lossy projection to v1 preset (HarmonicField)."""
        from nh_presets.schema import Preset as PresetV1
        field = self.scene.project_to_base_field()
        return PresetV1(
            version="1",
            harmonic_field=field,
            renderer_capabilities_required=self.renderer_capabilities_required,
            metadata={**self.metadata, "projected_from_v2": True},
        )


def load_v2(path: str) -> PresetV2:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return PresetV2.from_dict(data)


def save_v2(preset: PresetV2, path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(preset.to_dict(), f, indent=2, ensure_ascii=False)


# ── Migration: HarmonicField -> HarmonicScene ──────────────────────────────────

def _bands_to_spatial(field: HarmonicField) -> Dict[int, SpatialBand]:
    """Extract SpatialBand dict from a HarmonicField's partials."""
    bands = {}
    for n, p in field.partials.items():
        sp = p.spatial or {}
        bands[n] = SpatialBand(
            az=sp.get("az", 0.0),
            dist=sp.get("dist", 1.0),
            q=sp.get("q", 0.5),
            on=sp.get("on", True),
        )
    return bands


def _extract_shaper_voices(field: HarmonicField) -> Dict[int, ShaperVoice]:
    """Extract shaper voices from partials that have active=True in spatial."""
    voices = {}
    for n, p in field.partials.items():
        sp = p.spatial or {}
        if sp.get("active"):
            voices[n] = ShaperVoice(
                n=n,
                gain=p.gain,
                pan=p.pan,
                phase=p.phase,
                envelope=p.envelope,
                active=True,
            )
    return voices


def migrate_v1_to_v2(field: HarmonicField, beacon_id: str = "beacon",
                     shaper_id: str = "shaper") -> HarmonicScene:
    """Migrate a v1 HarmonicField to a v2 HarmonicScene.

    Splits the flat field into a BeaconSource (bands) and a ShaperSource
    (active voices). Any partial without active flag is treated as a
    beacon band.
    """
    scene = HarmonicScene(version=SCENE_VERSION)

    # Beacon — all bands from the field, minus gains overridden by shaper.
    bands = _bands_to_spatial(field)
    if bands:
        scene.sources[beacon_id] = BeaconSource(
            source_id=beacon_id,
            f1=field.f1,
            bands=bands,
        )

    # Shaper — voices that were active.
    voices = _extract_shaper_voices(field)
    if voices:
        scene.sources[shaper_id] = ShaperSource(
            source_id=shaper_id,
            voices=voices,
        )

    return scene


def migrate_preset_v1_to_v2(path: str) -> PresetV2:
    """Load a v1 preset JSON and migrate it to v2."""
    from nh_presets.schema import load
    v1 = load(path)
    scene = migrate_v1_to_v2(v1.harmonic_field)
    return PresetV2(
        scene=scene,
        renderer_capabilities_required=v1.renderer_capabilities_required,
        metadata={**v1.metadata, "migrated_from_v1": True},
    )


# ── Validation ─────────────────────────────────────────────────────────────────

def validate_v2(preset: PresetV2) -> List[str]:
    """Validate a v2 preset. Returns a list of errors (empty if valid)."""
    errors: List[str] = []
    scene = preset.scene

    if scene.version != SCENE_VERSION:
        errors.append(f"expected version {SCENE_VERSION}, got {scene.version}")

    for sid, source in scene.sources.items():
        # Source ID must match key.
        if source.source_id != sid:
            errors.append(f"source key '{sid}' does not match source_id '{source.source_id}'")

        # BeaconSource validation.
        if isinstance(source, BeaconSource):
            if source.f1 <= 0:
                errors.append(f"beacon '{sid}': f1 must be positive, got {source.f1}")
            for n, band in source.bands.items():
                if n <= 0:
                    errors.append(f"beacon '{sid}': band index must be positive, got {n}")
                if not (0.0 <= band.az <= 360.0):
                    errors.append(f"beacon '{sid}' band {n}: az out of range {band.az}")
                if not (0.0 <= band.dist <= 10.0):
                    errors.append(f"beacon '{sid}' band {n}: dist out of range {band.dist}")

        # ShaperSource validation.
        if isinstance(source, ShaperSource):
            for n, voice in source.voices.items():
                if voice.n != n:
                    errors.append(f"shaper '{sid}': voice key {n} != voice.n {voice.n}")
                if voice.gain < 0:
                    errors.append(f"shaper '{sid}' voice {n}: negative gain")

        # SampleSource validation.
        if isinstance(source, SampleSource):
            if not source.audio_path:
                errors.append(f"sample '{sid}': audio_path is required")

    # Modulation route target paths must reference valid sources.
    source_ids = set(scene.sources.keys())
    for rid, route in scene.modulations.items():
        # target_path format: "sources.<source_id>.<param>"
        parts = route.target_path.split(".")
        if len(parts) >= 2 and parts[0] == "sources":
            target_sid = parts[1]
            if target_sid not in source_ids:
                errors.append(
                    f"modulation '{rid}': target source '{target_sid}' not in scene"
                )

    return errors
