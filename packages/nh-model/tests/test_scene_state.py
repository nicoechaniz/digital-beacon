"""Tests for SceneState — multi-source runtime state."""
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
from nh_model import (
    SceneState,
    BeaconRuntime,
    ShaperRuntime,
    ActiveVoiceState,
    SampleRuntime,
    ProcessorRuntime,
)


# ── Construction ───────────────────────────────────────────────────────────────

def test_scene_state_default():
    ss = SceneState()
    assert ss.master_gain == 0.6
    assert isinstance(ss.scene, HarmonicScene)


def test_scene_state_populates_runtime():
    scene = HarmonicScene(
        sources={
            "beacon": BeaconSource(source_id="beacon", f1=55.0),
            "shaper": ShaperSource(source_id="shaper"),
            "frogs": SampleSource(source_id="frogs", audio_path="x.wav"),
        },
        processing_chain=ProcessingChain(
            processors=[ProcessorState("comb", "harmonic_comb", {})],
        ),
    )
    ss = SceneState(scene=scene)
    assert "beacon" in ss.beacons
    assert "shaper" in ss.shapers
    assert "frogs" in ss.samples
    assert "comb" in ss.processors
    assert ss.beacons["beacon"].vsrate == 1.0


# ── Path-addressed controls ────────────────────────────────────────────────────

def test_path_control_beacon_f1():
    scene = HarmonicScene(
        sources={"beacon": BeaconSource(source_id="beacon", f1=50.0)},
    )
    ss = SceneState(scene=scene)
    ss.apply_control({"path": "sources.beacon.f1_offset", "value": 10.0})
    assert ss.beacons["beacon"].f1_offset == 10.0


def test_path_control_shaper_gain():
    scene = HarmonicScene(
        sources={"shaper": ShaperSource(source_id="shaper")},
    )
    ss = SceneState(scene=scene)
    ss.apply_control({"path": "sources.shaper.gain", "value": 0.3})
    assert ss.shapers["shaper"].gain_offset == 0.3


def test_path_control_sample_play():
    scene = HarmonicScene(
        sources={"s1": SampleSource(source_id="s1", audio_path="x.wav")},
    )
    ss = SceneState(scene=scene)
    ss.apply_control({"path": "sources.s1.play", "value": True})
    assert ss.samples["s1"].playing is True


def test_path_control_processor_param():
    scene = HarmonicScene(
        processing_chain=ProcessingChain(
            processors=[ProcessorState("comb", "harmonic_comb", {"wet": 0.5})],
        ),
    )
    ss = SceneState(scene=scene)
    ss.apply_control({"path": "processors.comb.wet", "value": 0.9})
    assert ss.processors["comb"].param_overrides["wet"] == 0.9


def test_path_control_scene_master():
    ss = SceneState()
    ss.apply_control({"path": "scene.master_gain", "value": 0.2})
    assert ss.master_gain == 0.2


def test_path_control_unknown_path_is_noop():
    ss = SceneState()
    ss.apply_control({"path": "garbage.nonexistent.param", "value": 99.0})
    assert ss.master_gain == 0.6  # unchanged


# ── Type-based controls ────────────────────────────────────────────────────────

def test_type_master():
    ss = SceneState()
    ss.apply_control({"type": "master", "value": 0.25})
    assert ss.master_gain == 0.25


def test_type_beacon_f1():
    scene = HarmonicScene(
        sources={"beacon": BeaconSource(source_id="beacon", f1=65.0)},
    )
    ss = SceneState(scene=scene)
    ss.apply_control({"type": "beacon_f1", "value": {"source_id": "beacon", "offset": 5.0}})
    assert ss.beacons["beacon"].f1_offset == 5.0


# ── Pad events — only affect ShaperSource ──────────────────────────────────────

def test_pad_on_activates_shaper_voice():
    scene = HarmonicScene(
        sources={"shaper": ShaperSource(source_id="shaper")},
    )
    ss = SceneState(scene=scene)
    ss.apply_control({"type": "pad_on", "value": {"n": 3, "vel": 100}})
    sr = ss.shapers["shaper"]
    assert sr.voice_count() == 1
    assert sr.active_voices[3].gate is True
    assert sr.active_voices[3].envelope_phase == "attack"


def test_pad_off_releases_shaper_voice():
    scene = HarmonicScene(
        sources={"shaper": ShaperSource(source_id="shaper")},
    )
    ss = SceneState(scene=scene)
    ss.apply_control({"type": "pad_on", "value": {"n": 5}})
    ss.apply_control({"type": "pad_off", "value": {"n": 5}})
    sr = ss.shapers["shaper"]
    v = sr.active_voices[5]
    assert v.gate is False
    assert v.envelope_phase == "release"


def test_pad_toggle():
    scene = HarmonicScene(
        sources={"shaper": ShaperSource(source_id="shaper")},
    )
    ss = SceneState(scene=scene)
    ss.apply_control({"type": "pad_toggle", "value": {"n": 7, "active": True}})
    sr = ss.shapers["shaper"]
    assert sr.voice_count() == 1

    ss.apply_control({"type": "pad_toggle", "value": {"n": 7, "active": False}})
    v = sr.active_voices[7]
    assert v.gate is False


def test_pad_event_does_not_affect_beacon():
    """CRITICAL CONTRACT: Pads only affect ShaperSource, never BeaconSource."""
    scene = HarmonicScene(
        sources={
            "beacon": BeaconSource(source_id="beacon", f1=65.0,
                bands={1: SpatialBand(az=0.0, on=True)}),
            "shaper": ShaperSource(source_id="shaper"),
        },
    )
    ss = SceneState(scene=scene)
    # Press pad: shaper voice activates, beacon gain unchanged.
    ss.apply_control({"type": "pad_on", "value": {"n": 1, "vel": 127}})
    assert ss.shapers["shaper"].voice_count() == 1
    assert ss.beacons["beacon"].gain_offset == 1.0  # unchanged!


def test_pad_voice_on_path():
    """Path-based voice activation."""
    scene = HarmonicScene(
        sources={"shaper": ShaperSource(source_id="shaper")},
    )
    ss = SceneState(scene=scene)
    ss.apply_control({"path": "sources.shaper.voice_3_on", "value": 0.8})
    sr = ss.shapers["shaper"]
    assert sr.active_voices[3].velocity == 0.8


# ── ShaperRuntime voice lifecycle ──────────────────────────────────────────────

def test_shaper_runtime_voice_lifecycle():
    sr = ShaperRuntime(source_id="test")
    sr.voice_on(1, 0.7, clock=0.0)
    assert sr.voice_count() == 1
    v = sr.active_voices[1]
    assert v.velocity == 0.7
    assert v.gate is True
    assert v.envelope_phase == "attack"

    sr.voice_off(1, clock=0.5)
    v = sr.active_voices[1]
    assert v.gate is False
    assert v.envelope_phase == "release"

    sr.cleanup_released(max_age_s=0.0, clock=10.0)
    assert sr.voice_count() == 0


def test_shaper_runtime_steal():
    sr = ShaperRuntime(source_id="test", polyphony_mode="steal")
    for n in range(1, 34):  # 33 voices, only 32 survive
        sr.voice_on(n, clock=float(n))
    assert sr.voice_count() == 32
    # Oldest voice (n=1) should be stolen.
    assert 1 not in sr.active_voices


def test_shaper_runtime_toggle():
    sr = ShaperRuntime(source_id="test")
    assert sr.voice_toggle(5, clock=0.0) is True
    assert sr.voice_count() == 1
    assert sr.voice_toggle(5, clock=0.0) is False
    # Voice is still in dict but gated off.
    assert sr.active_voices[5].gate is False


def test_shaper_runtime_panic():
    sr = ShaperRuntime(source_id="test")
    sr.voice_on(1)
    sr.voice_on(3)
    assert sr.voice_count() == 2
    sr.panic()
    assert sr.voice_count() == 0


# ── SceneState snapshot ────────────────────────────────────────────────────────

def test_scene_snapshot_includes_runtime():
    scene = HarmonicScene(
        sources={
            "beacon": BeaconSource(source_id="beacon", f1=40.0,
                bands={1: SpatialBand(az=0.0, on=True)}),
            "shaper": ShaperSource(source_id="shaper"),
        },
    )
    ss = SceneState(scene=scene)
    ss.beacons["beacon"].f1_offset = 5.0
    ss.shapers["shaper"].voice_on(3, 0.5)

    snap = ss.scene_snapshot()
    assert snap["version"] == "2"
    assert snap["sources"]["beacon"]["runtime"]["effective_f1"] == 45.0
    assert snap["sources"]["shaper"]["runtime"]["voice_count"] == 1
    assert snap["master_gain"] == 0.6


def test_scene_snapshot_processor_overrides():
    scene = HarmonicScene(
        processing_chain=ProcessingChain(
            processors=[ProcessorState("comb", "harmonic_comb", {"wet": 0.3})],
        ),
    )
    ss = SceneState(scene=scene)
    ss.processors["comb"].param_overrides["wet"] = 0.9
    snap = ss.scene_snapshot()
    procs = snap["processing_chain"]["processors"]
    assert procs[0]["param_overrides"]["wet"] == 0.9


# ── base_field compat ─────────────────────────────────────────────────────────

def test_to_base_field():
    scene = HarmonicScene(
        sources={
            "beacon": BeaconSource(source_id="beacon", f1=42.0,
                bands={1: SpatialBand(az=0.0, on=True)}),
            "shaper": ShaperSource(source_id="shaper",
                voices={1: ShaperVoice(n=1, gain=0.7, active=True)}),
        },
    )
    ss = SceneState(scene=scene)
    field = ss.to_base_field()
    assert field.f1 == 42.0
    assert field.partials[1].gain == 0.7  # shaper override


# ── Panic and reset ────────────────────────────────────────────────────────────

def test_scene_state_panic():
    scene = HarmonicScene(
        sources={
            "beacon": BeaconSource(source_id="beacon"),
            "shaper": ShaperSource(source_id="shaper"),
            "s1": SampleSource(source_id="s1", audio_path="x.wav"),
        },
    )
    ss = SceneState(scene=scene)
    ss.shapers["shaper"].voice_on(1)
    ss.samples["s1"].playing = True
    ss.beacons["beacon"].gain_offset = 2.0

    ss.panic()
    assert ss.shapers["shaper"].voice_count() == 0
    assert ss.samples["s1"].playing is False
    assert ss.beacons["beacon"].gain_offset == 0.0


def test_scene_state_reset_modulations():
    scene = HarmonicScene(
        sources={"beacon": BeaconSource(source_id="beacon")},
    )
    ss = SceneState(scene=scene)
    ss.master_gain = 0.1
    ss.beacons["beacon"].f1_offset = 20.0

    ss.reset_modulations()
    assert ss.master_gain == 0.6
    assert ss.beacons["beacon"].f1_offset == 0.0


# ── Serialization ─────────────────────────────────────────────────────────────

def test_scene_state_round_trip():
    scene = HarmonicScene(
        sources={
            "beacon": BeaconSource(source_id="beacon", f1=55.0),
            "shaper": ShaperSource(source_id="shaper"),
        },
    )
    ss = SceneState(scene=scene, master_gain=0.8)
    d = ss.to_dict()
    restored = SceneState.from_dict(d)
    assert restored.master_gain == 0.8
    assert restored.scene.sources["beacon"].f1 == 55.0


def test_modelstate_still_works():
    """Legacy ModelState still functions."""
    from nh_core import HarmonicField, Partial
    from nh_model import ModelState

    field = HarmonicField(f1=65.0)
    field.partials[1] = Partial(n=1, gain=1.0)
    state = ModelState(base_field=field)
    snapshot = state.to_snapshot()
    assert snapshot.f1 == 65.0
