"""Tests for scene-aware API routes (Phase 8)."""
import pytest
from fastapi.testclient import TestClient

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



def test_analysis_mock_and_apply_proposed_f1():
    from nh_ui import app
    from nh_ui.scene_api import set_scene_state

    scene = HarmonicScene(sources={"beacon": BeaconSource(source_id="beacon", f1=65.0)})
    set_scene_state(SceneState(scene=scene))
    client = TestClient(app)

    payload = {
        "audio_path": "demo.wav",
        "duration_s": 2.5,
        "f0_track": {"f0_mean": 110.0, "voiced_fraction": 0.8},
        "phideus": {"h_series": {"concentration": 0.7}},
        "proposed_f1": 55.0,
    }
    r = client.post("/nh/v2/analysis/mock", json=payload)
    assert r.status_code == 200
    assert r.json()["analysis"]["proposed_f1"] == 55.0

    r = client.get("/nh/v2/analysis")
    assert r.status_code == 200
    assert r.json()["analysis"]["f0_track"]["f0_mean"] == 110.0

    r = client.post("/nh/v2/analysis/apply-proposed-f1")
    assert r.status_code == 200
    assert r.json()["f1"] == 55.0
    scene = client.get("/nh/v2/scene").json()
    assert scene["sources"]["beacon"]["f1"] == 55.0

    set_scene_state(None)
