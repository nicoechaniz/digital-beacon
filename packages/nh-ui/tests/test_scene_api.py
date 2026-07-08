"""Tests for scene-aware API routes (Phase 8)."""
import pytest

from nh_core import (
    HarmonicScene,
    BeaconSource,
    ShaperSource,
    ShaperVoice,
    SampleSource,
    SpatialBand,
    ProcessorState,
    ProcessingChain,
)
from nh_model import SceneState


def test_scene_api_routes_registered():
    """scene_api module loads without errors."""
    from nh_ui.scene_api import register_scene_routes, set_scene_state, get_scene_state
    assert callable(register_scene_routes)
    assert get_scene_state() is None


def test_set_scene_state():
    from nh_ui.scene_api import set_scene_state, get_scene_state

    scene = HarmonicScene(
        sources={
            "beacon": BeaconSource(source_id="beacon", f1=50.0),
            "shaper": ShaperSource(source_id="shaper"),
        },
    )
    state = SceneState(scene=scene)
    set_scene_state(state)
    assert get_scene_state() is state
    assert get_scene_state().scene.sources["beacon"].f1 == 50.0

    # Clean up.
    set_scene_state(None)
    assert get_scene_state() is None


def test_scene_state_snapshot_has_all_sources():
    scene = HarmonicScene(
        sources={
            "beacon": BeaconSource(source_id="beacon", f1=42.0,
                bands={1: SpatialBand(az=0.0, on=True)}),
            "shaper": ShaperSource(source_id="shaper",
                voices={3: ShaperVoice(n=3, gain=0.5, active=True)}),
            "frogs": SampleSource(source_id="frogs", audio_path="/tmp/frogs.wav"),
        },
        processing_chain=ProcessingChain(
            processors=[ProcessorState("comb", "harmonic_comb", {"wet": 0.5})],
            routing={"beacon": ["comb"]},
        ),
    )
    state = SceneState(scene=scene)
    snap = state.scene_snapshot()

    assert "beacon" in snap["sources"]
    assert "shaper" in snap["sources"]
    assert "frogs" in snap["sources"]
    assert len(snap["processing_chain"]["processors"]) == 1
    assert snap["processing_chain"]["routing"]["beacon"] == ["comb"]


def test_scene_snapshot_shaper_voices():
    scene = HarmonicScene(
        sources={"shaper": ShaperSource(source_id="shaper")},
    )
    state = SceneState(scene=scene)
    state.shapers["shaper"].voice_on(5, 0.8)
    state.shapers["shaper"].voice_on(7, 0.6)

    snap = state.scene_snapshot()
    shaper_snap = snap["sources"]["shaper"]
    assert shaper_snap["runtime"]["voice_count"] == 2
    assert "5" in shaper_snap["runtime"]["active_voices"]
