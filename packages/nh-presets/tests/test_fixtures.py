"""Test that legacy fixture files migrate correctly to HarmonicField."""
import os
import pytest

from nh_core import HarmonicField, Partial
from nh_presets import (
    migrate_beacon_spatial,
    migrate_digital_beacon_v1,
    migrate_digital_beacon_v2,
)

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def test_migrate_v1_fixture():
    path = os.path.join(FIXTURES, "legacy_digital_beacon_v1.json")
    field = migrate_digital_beacon_v1(path)
    assert isinstance(field, HarmonicField)
    assert field.f1 == 40.0
    assert len(field.partials) == 5
    # Band 1: gain 1.0, az 0
    assert field.partials[1].gain == 1.0
    assert field.partials[1].spatial["az"] == 0.0
    # Band 5: gain 0.4, on=0
    assert field.partials[5].gain == 0.4
    assert field.partials[5].spatial["on"] is False


def test_migrate_v2_fixture():
    path = os.path.join(FIXTURES, "legacy_digital_beacon_v2.json")
    field = migrate_digital_beacon_v2(path)
    assert isinstance(field, HarmonicField)
    assert field.f1 == 40.0
    # 6 beacon bands: n=1,2,3,4,5,8
    # Shaper has 3 voices: n=1 (active), n=3 (active), n=5 (inactive)
    # Active shaper voices override existing beacon bands; inactive is skipped
    assert len(field.partials) == 6
    # Voice 1: shaper overrides with pan/phase
    p1 = field.partials[1]
    assert p1.gain == 0.6
    assert p1.pan == -0.3
    assert p1.phase == 0.0
    assert p1.spatial["active"] is True
    assert p1.envelope["attack_s"] == 0.01
    # Voice 5 is inactive in shaper, but exists as beacon band (no envelope/pan override)
    assert 5 in field.partials
    assert field.partials[5].gain == 0.2  # beacon band gain, not shaper
    assert field.partials[5].pan == 0.0  # no shaper override


def test_migrate_beacon_spatial_fixture():
    path = os.path.join(FIXTURES, "legacy_beacon_spatial_13band.json")
    field = migrate_beacon_spatial(path)
    assert isinstance(field, HarmonicField)
    assert field.f1 == 55.0
    assert len(field.partials) == 13
    assert field.partials[1].gain == 1.0
    assert field.partials[1].spatial["az"] == 0.0
    assert field.partials[13].gain == 0.1
    assert field.partials[13].spatial["az"] == 180.0


def test_all_fixtures_produce_valid_spatial():
    """Every migrated fixture must have spatial dicts that satisfy the contract."""
    from nh_core import validate_spatial

    fixtures = [
        ("legacy_digital_beacon_v1.json", migrate_digital_beacon_v1),
        ("legacy_digital_beacon_v2.json", migrate_digital_beacon_v2),
        ("legacy_beacon_spatial_13band.json", migrate_beacon_spatial),
    ]
    for name, migrator in fixtures:
        path = os.path.join(FIXTURES, name)
        field = migrator(path)
        for n, p in field.partials.items():
            errors = validate_spatial(p.spatial)
            assert errors == [], f"{name} partial {n} spatial invalid: {errors}"
