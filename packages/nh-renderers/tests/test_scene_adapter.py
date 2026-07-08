"""Regression tests for Phase 10 invariants.

1. Preset load does NOT collapse shaper into beacon drone.
2. Beacon drones while shaper is silent.
3. All sources play together without interference.
4. SC/ATK OSC adapter produces correct commands.
"""

import pytest

from nh_core import (
    HarmonicScene,
    BeaconSource,
    ShaperSource,
    ShaperVoice,
    SampleSource,
    SpatialBand,
)


def test_regression_shaper_independent_of_beacon():
    """Loading a preset with both beacon and shaper keeps them independent."""
    scene = HarmonicScene(
        sources={
            "beacon": BeaconSource(source_id="beacon", f1=40.0,
                bands={
                    1: SpatialBand(az=0.0, on=True),
                    2: SpatialBand(az=30.0, on=True),
                    3: SpatialBand(az=60.0, on=True),
                }),
            "shaper": ShaperSource(source_id="shaper",
                voices={
                    1: ShaperVoice(n=1, gain=0.6, active=True),
                    5: ShaperVoice(n=5, gain=0.4, active=True),
                }),
        },
    )

    # Beacon has 3 bands.
    beacon = scene.sources["beacon"]
    assert isinstance(beacon, BeaconSource)
    assert len(beacon.bands) == 3
    assert beacon.bands[1].on is True

    # Shaper has 2 active voices, independent of beacon.
    shaper = scene.sources["shaper"]
    assert isinstance(shaper, ShaperSource)
    assert len(shaper.voices) == 2

    # Projection: shaper overrides beacon for matching harmonics.
    field = scene.project_to_base_field()
    assert field.partials[1].gain == 0.6  # shaper wins
    assert field.partials[2].gain == 0.8  # beacon band 2 (master_gain * on)
    assert field.partials[3].gain == 0.8  # beacon band 3


def test_regression_beacon_drones_while_shaper_silent():
    """Beacon continues to drone even when all shaper voices are off."""
    scene = HarmonicScene(
        sources={
            "beacon": BeaconSource(source_id="beacon", f1=50.0,
                bands={1: SpatialBand(az=0.0, on=True)}),
            "shaper": ShaperSource(source_id="shaper",
                voices={1: ShaperVoice(n=1, gain=0.6, active=False)}),
        },
    )

    field = scene.project_to_base_field()
    # Band 1 exists because of beacon — shaper inactive doesn't delete it.
    assert 1 in field.partials
    assert field.partials[1].gain == 0.8  # beacon gain (master_gain * on)


def test_regression_all_sources_together():
    """All three source types coexist without interference."""
    scene = HarmonicScene(
        sources={
            "beacon": BeaconSource(source_id="beacon", f1=55.0,
                bands={1: SpatialBand(az=0.0, on=True)}),
            "shaper": ShaperSource(source_id="shaper",
                voices={3: ShaperVoice(n=3, gain=0.7, active=True)}),
            "frogs": SampleSource(source_id="frogs", audio_path="/tmp/frogs.wav",
                                 loop=True),
        },
    )

    # All three sources present.
    assert isinstance(scene.sources["beacon"], BeaconSource)
    assert isinstance(scene.sources["shaper"], ShaperSource)
    assert isinstance(scene.sources["frogs"], SampleSource)

    # Projection doesn't crash with all three.
    field = scene.project_to_base_field()
    assert field is not None
    assert field.f1 == 55.0


def test_scene_to_beacon_osc():
    from nh_renderers.scene_adapter import scene_to_beacon_osc

    scene = HarmonicScene(
        sources={
            "beacon": BeaconSource(source_id="beacon", f1=40.0, vsrate=1.5,
                bands={
                    1: SpatialBand(az=0.0, dist=1.0, q=0.5, on=True),
                    2: SpatialBand(az=30.0, dist=1.5, q=0.3, on=False),
                }),
        },
    )
    cmds = scene_to_beacon_osc(scene)
    # We should have commands for f1, vsrate, master, and per-band.
    addrs = [c[0] for c in cmds]
    assert "/beacon/beacon/f1" in addrs
    assert "/beacon/beacon/vsrate" in addrs
    assert "/beacon/beacon/band/1/gain" in addrs
    assert "/beacon/beacon/band/2/on" in addrs


def test_scene_to_shaper_osc():
    from nh_renderers.scene_adapter import scene_to_shaper_osc

    scene = HarmonicScene(
        sources={
            "shaper": ShaperSource(source_id="shaper",
                voices={
                    1: ShaperVoice(n=1, gain=0.6, active=True,
                                   envelope={"attack_s": 0.01, "release_s": 0.15}),
                    2: ShaperVoice(n=2, gain=0.0, active=False),
                }),
        },
    )
    cmds = scene_to_shaper_osc(scene)
    addrs = [c[0] for c in cmds]
    assert "/shaper/shaper/voice/1/on" in addrs
    assert "/shaper/shaper/voice/1/attack" in addrs
    assert "/shaper/shaper/voice/2/off" in addrs


def test_scene_to_all_osc():
    from nh_renderers.scene_adapter import scene_to_all_osc

    scene = HarmonicScene(
        sources={
            "beacon": BeaconSource(source_id="beacon", f1=50.0,
                bands={1: SpatialBand(on=True)}),
            "shaper": ShaperSource(source_id="shaper",
                voices={1: ShaperVoice(n=1, active=True)}),
            "s1": SampleSource(source_id="s1", audio_path="x.wav"),
        },
    )
    cmds = scene_to_all_osc(scene)
    addrs = [c[0] for c in cmds]
    assert "/beacon/beacon/f1" in addrs
    assert "/shaper/shaper/master_gain" in addrs
    assert "/sample/s1/path" in addrs
    assert "/global/panic" in addrs
