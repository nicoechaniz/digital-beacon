"""Tests for preset v2 (scene-based) schema, migration, and validation."""
import os
import tempfile

import pytest

from nh_core import (
    HarmonicField,
    HarmonicScene,
    BeaconSource,
    ShaperSource,
    ShaperVoice,
    SpatialBand,
    Partial,
    SampleSource,
)
from nh_presets import (
    Preset,
    PresetV2,
    load_v2,
    save_v2,
    migrate_v1_to_v2,
    migrate_preset_v1_to_v2,
    validate_v2,
    SCENE_VERSION,
)


# ── PresetV2 round-trip ────────────────────────────────────────────────────────

def test_preset_v2_round_trip():
    scene = HarmonicScene(
        sources={
            "beacon": BeaconSource(source_id="beacon", f1=55.0,
                bands={1: SpatialBand(az=0.0, on=True)}),
        },
    )
    preset = PresetV2(scene=scene, metadata={"name": "test"})

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        path = f.name
    try:
        save_v2(preset, path)
        loaded = load_v2(path)
        assert loaded.version == SCENE_VERSION
        assert loaded.scene.sources["beacon"].f1 == 55.0
        assert loaded.metadata["name"] == "test"
    finally:
        os.unlink(path)


# ── Migration: v1 -> v2 ────────────────────────────────────────────────────────

def test_migrate_v1_to_v2_beacon_only():
    field = HarmonicField(f1=40.0)
    field.partials[1] = Partial(n=1, gain=0.8,
        spatial={"az": 0.0, "dist": 1.0, "q": 0.5, "on": True})
    field.partials[3] = Partial(n=3, gain=0.5,
        spatial={"az": 90.0, "dist": 0.5, "q": 0.1, "on": True})

    scene = migrate_v1_to_v2(field)
    assert isinstance(scene, HarmonicScene)
    assert scene.version == SCENE_VERSION

    beacon = scene.sources["beacon"]
    assert isinstance(beacon, BeaconSource)
    assert beacon.f1 == 40.0
    assert len(beacon.bands) == 2
    assert beacon.bands[1].az == 0.0
    assert beacon.bands[3].dist == 0.5

    # No shaper source (no active voices)
    assert "shaper" not in scene.sources


def test_migrate_v1_to_v2_with_shaper():
    field = HarmonicField(f1=65.0)
    field.partials[1] = Partial(n=1, gain=0.8,
        spatial={"az": 0.0, "dist": 1.0, "on": True, "active": True},
        pan=-0.3, phase=22.5,
        envelope={"attack_s": 0.01, "release_s": 0.15})
    field.partials[2] = Partial(n=2, gain=0.5,
        spatial={"az": 30.0, "dist": 1.0, "on": True})

    scene = migrate_v1_to_v2(field)
    assert "beacon" in scene.sources
    assert "shaper" in scene.sources

    shaper = scene.sources["shaper"]
    assert isinstance(shaper, ShaperSource)
    assert len(shaper.voices) == 1
    v1 = shaper.voices[1]
    assert v1.gain == 0.8
    assert v1.pan == -0.3
    assert v1.active is True

    # Band 2 is beacon-only.
    beacon = scene.sources["beacon"]
    assert 2 in beacon.bands
    assert beacon.bands[2].az == 30.0


def test_migrate_preset_v1_to_v2_from_file():
    """Round-trip: write a v1 preset, migrate to v2."""
    from nh_presets import save as save_v1

    field = HarmonicField(f1=50.0)
    field.partials[1] = Partial(n=1, gain=0.9,
        spatial={"az": 45.0, "on": True, "active": True}, pan=0.5)
    v1 = Preset(harmonic_field=field)

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        path = f.name
    try:
        save_v1(v1, path)
        v2 = migrate_preset_v1_to_v2(path)
        assert v2.version == SCENE_VERSION
        assert v2.metadata["migrated_from_v1"] is True
        assert v2.scene.sources["beacon"].f1 == 50.0
    finally:
        os.unlink(path)


# ── Projection: v2 -> v1 ────────────────────────────────────────────────────────

def test_project_v2_to_v1():
    scene = HarmonicScene(
        sources={
            "beacon": BeaconSource(source_id="beacon", f1=42.0,
                bands={1: SpatialBand(az=0.0, on=True)}),
            "shaper": ShaperSource(source_id="shaper",
                voices={1: ShaperVoice(n=1, gain=0.7, active=True,
                                       pan=-0.2, phase=30.0)}),
        },
    )
    v2 = PresetV2(scene=scene)
    v1 = v2.project_to_v1()
    assert v1.version == "1"
    assert v1.harmonic_field.f1 == 42.0
    assert v1.harmonic_field.partials[1].gain == 0.7  # shaper override
    assert v1.metadata["projected_from_v2"] is True


# ── Validation ─────────────────────────────────────────────────────────────────

def test_validate_v2_valid():
    scene = HarmonicScene(
        sources={
            "beacon": BeaconSource(source_id="beacon", f1=50.0,
                bands={1: SpatialBand(az=0.0)}),
        },
    )
    preset = PresetV2(scene=scene)
    errors = validate_v2(preset)
    assert errors == []


def test_validate_v2_negative_f1():
    scene = HarmonicScene(
        sources={"beacon": BeaconSource(source_id="beacon", f1=-10.0)},
    )
    preset = PresetV2(scene=scene)
    errors = validate_v2(preset)
    assert any("f1" in e for e in errors)


def test_validate_v2_az_out_of_range():
    scene = HarmonicScene(
        sources={"beacon": BeaconSource(source_id="beacon",
            bands={1: SpatialBand(az=400.0)})},
    )
    preset = PresetV2(scene=scene)
    errors = validate_v2(preset)
    assert any("az out of range" in e for e in errors)


def test_validate_v2_source_id_mismatch():
    scene = HarmonicScene(
        sources={"wrong": BeaconSource(source_id="beacon")},
    )
    preset = PresetV2(scene=scene)
    errors = validate_v2(preset)
    assert any("does not match" in e for e in errors)


def test_validate_v2_sample_missing_path():
    scene = HarmonicScene(
        sources={"s1": SampleSource(source_id="s1", audio_path="")},
    )
    preset = PresetV2(scene=scene)
    errors = validate_v2(preset)
    assert any("audio_path" in e for e in errors)


def test_validate_v2_modulation_invalid_target():
    from nh_core import ModulationRoute
    scene = HarmonicScene(
        sources={"beacon": BeaconSource(source_id="beacon")},
        modulations={
            "m1": ModulationRoute(route_id="m1", source="lfo1",
                                  target_path="sources.nonexistent.f1"),
        },
    )
    preset = PresetV2(scene=scene)
    errors = validate_v2(preset)
    assert any("not in scene" in e for e in errors)
