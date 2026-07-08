"""Tests for AnalysisResult data model (Phase 6)."""
import pytest

from nh_analysis.result import (
    AnalysisResult,
    F0Track,
    SpectralMetrics,
    PhideusDescriptors,
    EmotionResult,
    SpeakerResult,
)


def test_f0_track():
    t = F0Track(
        times=[0.0, 0.01, 0.02],
        f0_hz=[120.0, 121.0, 122.0],
        voiced=[True, True, True],
        confidence=[0.9, 0.95, 0.92],
        f0_mean=121.0,
        voiced_fraction=1.0,
    )
    d = t.to_dict()
    r = F0Track.from_dict(d)
    assert r.f0_mean == 121.0
    assert len(r.times) == 3


def test_spectral_metrics():
    m = SpectralMetrics(centroid_hz=800.0, rms_db=-12.0, dynamic_range_db=30.0)
    d = m.to_dict()
    r = SpectralMetrics.from_dict(d)
    assert r.centroid_hz == 800.0


def test_phideus_descriptors():
    p = PhideusDescriptors(
        h_series={"h2_h1": -3.0, "h3_h1": -6.0, "concentration": 0.8},
        v4_log={"dim0": 0.5},
    )
    d = p.to_dict()
    r = PhideusDescriptors.from_dict(d)
    assert r.h_series["h2_h1"] == -3.0


def test_emotion_result():
    e = EmotionResult(primary="joy", confidence=0.85, valence=0.7, arousal=0.6,
                      all_scores={"joy": 0.85, "sadness": 0.1})
    d = e.to_dict()
    r = EmotionResult.from_dict(d)
    assert r.primary == "joy"
    assert r.all_scores["joy"] == 0.85


def test_speaker_result():
    s = SpeakerResult(speaker_id="nico", confidence=0.92)
    d = s.to_dict()
    r = SpeakerResult.from_dict(d)
    assert r.speaker_id == "nico"


def test_analysis_result_round_trip():
    ar = AnalysisResult(
        audio_path="/tmp/test.wav",
        duration_s=2.5,
        f0_track=F0Track(f0_mean=200.0),
        spectral=SpectralMetrics(centroid_hz=750.0),
        proposed_f1=55.0,
        proposed_bands={1: {"az": 0.0, "dist": 1.0}},
        computed_at="2026-07-07T12:00:00Z",
    )
    d = ar.to_dict()
    r = AnalysisResult.from_dict(d)
    assert r.audio_path == "/tmp/test.wav"
    assert r.f0_track.f0_mean == 200.0
    assert r.proposed_f1 == 55.0
    assert r.computed_at == "2026-07-07T12:00:00Z"


def test_analysis_result_partial():
    """Partial analysis (F0 only) is valid."""
    ar = AnalysisResult(
        audio_path="/tmp/test.wav",
        duration_s=5.0,
        f0_track=F0Track(f0_mean=150.0),
    )
    assert ar.spectral is None
    assert ar.emotion is None
    d = ar.to_dict()
    r = AnalysisResult.from_dict(d)
    assert r.spectral is None
