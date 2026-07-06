import os
import tempfile

import pytest

from nh_core import HarmonicField, Partial, RendererCapabilities
from nh_presets import (
    Preset,
    load,
    migrate_beacon_spatial,
    migrate_digital_beacon_v1,
    migrate_digital_beacon_v2,
    project_to_capabilities,
    save,
    validate,
)


def _write_tmp_json(data: dict) -> str:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        import json
        json.dump(data, f)
        return f.name


def test_migrate_digital_beacon_v1():
    data = {
        "bands": [
            {"n": 1, "gain": 2.0, "az": 0.0, "dist": 1.0, "on": 1, "q": 0.5},
            {"n": 2, "gain": 1.5, "az": 30.0, "dist": 2.0, "on": 1, "q": 0.1},
        ],
        "master": 1.0,
    }
    path = _write_tmp_json(data)
    try:
        field = migrate_digital_beacon_v1(path)
        assert field.f1 == 65.0
        assert len(field.partials) == 2
        assert field.partials[1].gain == 2.0
        assert field.partials[2].spatial["az"] == 30.0
    finally:
        os.unlink(path)


def test_migrate_digital_beacon_v2():
    data = {
        "version": 2,
        "saved_at": 1234567890,
        "beacon": {
            "f1": 40,
            "vsrate": 1,
            "master": 1.5,
            "bands": [
                {"n": 1, "gain": 2.0, "az": 0.0, "dist": 1.0, "on": 1, "q": 0.5},
            ],
        },
        "shaper": {
            "master_gain": 0.16,
            "voices": {
                "1": {"gain": 0.6, "pan": -0.5, "phase_deg": 45.0, "active": True, "freq": 40.4},
            },
        },
    }
    path = _write_tmp_json(data)
    try:
        field = migrate_digital_beacon_v2(path)
        assert field.f1 == 40.0
        assert len(field.partials) == 1
        p = field.partials[1]
        assert p.gain == 0.6  # shaper overrides beacon
        assert p.pan == -0.5
        assert p.phase == 45.0
    finally:
        os.unlink(path)


def test_migrate_beacon_spatial():
    data = {
        "bands": [
            {"gain": 2.0, "az": 0.0, "dist": 1.0, "solo": 0, "q": 0.042},
            {"gain": 1.5, "az": 30.0, "dist": 2.0, "solo": 0, "q": 0.042},
        ],
        "mix": 1,
        "master": 1,
    }
    path = _write_tmp_json(data)
    try:
        field = migrate_beacon_spatial(path)
        assert field.f1 == 65.0
        assert len(field.partials) == 2
        assert field.partials[1].gain == 2.0
        assert field.partials[2].spatial["az"] == 30.0
    finally:
        os.unlink(path)


def test_preset_round_trip():
    field = HarmonicField(f1=50.0)
    field.partials[1] = Partial(n=1, gain=1.0)
    field.partials[3] = Partial(n=3, gain=0.5, spatial={"az": 90.0})
    preset = Preset(harmonic_field=field)

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        path = f.name
    try:
        save(preset, path)
        loaded = load(path)
        assert loaded.harmonic_field.f1 == 50.0
        assert len(loaded.harmonic_field.partials) == 2
        assert loaded.harmonic_field.partials[3].spatial["az"] == 90.0
    finally:
        os.unlink(path)


def test_project_to_capabilities():
    field = HarmonicField(f1=65.0)
    for n in range(1, 33):
        field.partials[n] = Partial(n=n, gain=1.0 / n)
    preset = Preset(harmonic_field=field)

    caps = RendererCapabilities(max_partials=13)
    projected = project_to_capabilities(preset, caps)
    assert len(projected.harmonic_field.partials) == 13
    assert projected.renderer_capabilities_required.max_partials == 13


def test_validate():
    field = HarmonicField(f1=-5.0)
    preset = Preset(harmonic_field=field)
    errors = validate(preset)
    assert any("f1" in e for e in errors)

    field2 = HarmonicField(f1=65.0)
    field2.partials[1] = Partial(n=1, gain=-1.0)
    preset2 = Preset(harmonic_field=field2)
    errors2 = validate(preset2)
    assert any("gain" in e for e in errors2)
