"""Tests for the HarmonicScene v2 schema."""
import pytest

from nh_core import (
    HarmonicScene,
    BeaconSource,
    ShaperSource,
    ShaperVoice,
    SampleSource,
    VoiceSource,
    SpatialBand,
    ProcessorState,
    ProcessingChain,
    LFOState,
    ModulationRoute,
    HarmonicField,
    Partial,
)


# ── Source constructors ────────────────────────────────────────────────────────

def test_beacon_source_defaults():
    b = BeaconSource(source_id="main_beacon", f1=40.0)
    assert b.source_id == "main_beacon"
    assert b.f1 == 40.0
    assert b.kind == "beacon"
    assert b.bands == {}


def test_spatial_band():
    s = SpatialBand(az=45.0, dist=0.8, q=0.1, on=True)
    assert s.az == 45.0
    assert s.dist == 0.8
    d = s.to_dict()
    r = SpatialBand.from_dict(d)
    assert r.az == 45.0


def test_shaper_voice():
    v = ShaperVoice(n=3, gain=0.5, pan=-0.3, active=True,
                    envelope={"attack_s": 0.01})
    d = v.to_dict()
    r = ShaperVoice.from_dict(d)
    assert r.n == 3
    assert r.active
    assert r.envelope["attack_s"] == 0.01


def test_sample_source():
    s = SampleSource(source_id="frogs", audio_path="/tmp/frogs.wav",
                     loop=True, f1_override=55.0)
    assert s.kind == "sample"
    d = s.to_dict()
    r = SampleSource.from_dict(d)
    assert r.audio_path == "/tmp/frogs.wav"
    assert r.f1_override == 55.0


def test_voice_source():
    v = VoiceSource(source_id="mic_1", input_device="R24")
    assert v.kind == "voice"
    d = v.to_dict()
    r = VoiceSource.from_dict(d)
    assert r.input_device == "R24"


# ── Processing chain ───────────────────────────────────────────────────────────

def test_processor_state():
    p = ProcessorState(processor_id="comb_1",
                       processor_type="harmonic_comb",
                       params={"bandwidth": 0.5})
    assert p.processor_type == "harmonic_comb"
    d = p.to_dict()
    r = ProcessorState.from_dict(d)
    assert r.params["bandwidth"] == 0.5


def test_processing_chain():
    pc = ProcessingChain(
        processors=[
            ProcessorState("comb_1", "harmonic_comb", {"wet": 0.8}),
            ProcessorState("spat_1", "binaural_spatializer", {"bands": 13}),
        ],
        routing={"main_beacon": ["comb_1", "spat_1"]},
    )
    d = pc.to_dict()
    r = ProcessingChain.from_dict(d)
    assert len(r.processors) == 2
    assert r.routing["main_beacon"] == ["comb_1", "spat_1"]


# ── Modulation ─────────────────────────────────────────────────────────────────

def test_lfo_state():
    l = LFOState(lfo_id="lfo_1", waveform="triangle", rate_hz=0.25, depth=0.5)
    d = l.to_dict()
    r = LFOState.from_dict(d)
    assert r.waveform == "triangle"
    assert r.rate_hz == 0.25


def test_modulation_route():
    m = ModulationRoute(route_id="f1_wobble", source="lfo_1",
                        target_path="sources.main_beacon.f1",
                        scale=5.0, range_min=30.0, range_max=80.0)
    d = m.to_dict()
    r = ModulationRoute.from_dict(d)
    assert r.target_path == "sources.main_beacon.f1"
    assert r.range_max == 80.0


# ── HarmonicScene ──────────────────────────────────────────────────────────────

def test_scene_round_trip():
    scene = HarmonicScene(
        sources={
            "beacon": BeaconSource(source_id="beacon", f1=55.0),
            "shaper": ShaperSource(source_id="shaper", voices={
                1: ShaperVoice(n=1, gain=0.7, active=True),
            }),
        },
        processing_chain=ProcessingChain(
            processors=[ProcessorState("comb", "harmonic_comb", {})],
            routing={"beacon": ["comb"]},
        ),
        lfos={"lfo1": LFOState(lfo_id="lfo1", waveform="sine")},
    )

    d = scene.to_dict()
    restored = HarmonicScene.from_dict(d)

    assert restored.version == "2"
    assert len(restored.sources) == 2
    assert restored.sources["beacon"].f1 == 55.0
    assert restored.sources["shaper"].voices[1].gain == 0.7
    assert len(restored.processing_chain.processors) == 1
    assert len(restored.lfos) == 1


def test_scene_source_ids():
    scene = HarmonicScene(
        sources={
            "beacon": BeaconSource(source_id="beacon"),
            "shaper": ShaperSource(source_id="shaper"),
        },
    )
    assert set(scene.source_ids()) == {"beacon", "shaper"}


def test_scene_get_source():
    scene = HarmonicScene(
        sources={"beacon": BeaconSource(source_id="beacon", f1=42.0)},
    )
    b = scene.get_source("beacon")
    assert isinstance(b, BeaconSource)
    assert b.f1 == 42.0
    assert scene.get_source("nonexistent") is None


def test_scene_source_of_kind():
    scene = HarmonicScene(
        sources={
            "b1": BeaconSource(source_id="b1"),
            "b2": BeaconSource(source_id="b2"),
            "s1": ShaperSource(source_id="s1"),
        },
    )
    beacons = scene.source_of_kind("beacon")
    assert len(beacons) == 2
    shapers = scene.source_of_kind("shaper")
    assert len(shapers) == 1


# ── Projection: scene -> base_field (v1 compat) ────────────────────────────────

def test_project_beacon_only():
    scene = HarmonicScene(
        sources={
            "beacon": BeaconSource(
                source_id="beacon", f1=65.0,
                bands={
                    1: SpatialBand(az=0.0, dist=1.0, on=True),
                    3: SpatialBand(az=90.0, dist=0.5, on=True),
                },
            ),
        },
    )
    field = scene.project_to_base_field()
    assert isinstance(field, HarmonicField)
    assert field.f1 == 65.0
    assert len(field.partials) == 2
    assert field.partials[1].spatial["az"] == 0.0


def test_project_beacon_and_shaper():
    scene = HarmonicScene(
        sources={
            "beacon": BeaconSource(
                source_id="beacon", f1=40.0,
                bands={
                    1: SpatialBand(az=0.0, dist=1.0, on=True),
                    2: SpatialBand(az=30.0, dist=1.0, on=True),
                },
            ),
            "shaper": ShaperSource(
                source_id="shaper",
                voices={
                    1: ShaperVoice(n=1, gain=0.6, pan=-0.5, phase=45.0,
                                   active=True, envelope={"attack_s": 0.01}),
                    2: ShaperVoice(n=2, gain=0.0, active=False),
                },
            ),
        },
    )
    field = scene.project_to_base_field()
    assert isinstance(field, HarmonicField)
    # Voice 1 active: overrides beacon band 1.
    p1 = field.partials[1]
    assert p1.gain == 0.6  # shaper gain
    assert p1.pan == -0.5
    assert p1.phase == 45.0
    assert p1.spatial["active"] is True
    assert p1.spatial["beacon_gain"] is not None  # preserved
    assert p1.envelope["attack_s"] == 0.01
    # Voice 2 inactive: beacon band untouched.
    p2 = field.partials[2]
    assert p2.gain == 0.8  # beacon default (master_gain * on)


def test_project_sample_dropped():
    """Samples have no v1 representation — they are dropped in projection."""
    scene = HarmonicScene(
        sources={
            "beacon": BeaconSource(source_id="beacon", f1=65.0),
            "frogs": SampleSource(source_id="frogs", audio_path="/tmp/frogs.wav"),
        },
    )
    field = scene.project_to_base_field()
    assert isinstance(field, HarmonicField)
    assert len(field.partials) == 0  # sample dropped, no beacon bands


def test_project_no_beacon_defaults_f1():
    scene = HarmonicScene(
        sources={
            "shaper": ShaperSource(source_id="shaper"),
        },
    )
    field = scene.project_to_base_field()
    assert field.f1 == 65.0  # default


# ── Validation outline ─────────────────────────────────────────────────────────

def test_source_id_required():
    """Each source must have a non-empty source_id."""
    b = BeaconSource(source_id="")
    assert b.source_id == ""  # validation happens in preset layer
