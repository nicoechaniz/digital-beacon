import numpy as np
import pytest

from nh_analysis import (
    LibrosaPyinEstimator,
    harmonic_f1_search,
    harmonicity_score,
    harmonic_mask,
)


def _sine_wave(freq, sr=16000, duration=1.0):
    t = np.linspace(0, duration, int(sr * duration), endpoint=False)
    return np.sin(2 * np.pi * freq * t).astype(np.float32)


def test_harmonicity_pure_tone():
    sr = 16000
    f1 = 100.0
    audio = _sine_wave(f1, sr=sr, duration=0.5)
    score = harmonicity_score(audio, sr=sr, f1=f1, n_harmonics=5)
    assert score > 0.8


def test_f1_search_finds_tone():
    sr = 16000
    f1 = 80.0
    audio = _sine_wave(f1, sr=sr, duration=1.0)
    result = harmonic_f1_search(audio, sr=sr, fmin=50.0, fmax=150.0, n_harmonics=8)
    assert 70.0 <= result["f1"] <= 95.0


def test_harmonic_mask_shape():
    sr = 16000
    f1 = 100.0
    audio = _sine_wave(f1, sr=sr, duration=0.5)
    result = harmonic_mask(audio, sr=sr, f1=f1, n_harmonics=5)
    assert result["harmonic_audio"].shape == audio.shape
    assert result["residual_audio"].shape == audio.shape


def test_pyin_estimator():
    sr = 16000
    f1 = 120.0
    audio = _sine_wave(f1, sr=sr, duration=1.0)
    est = LibrosaPyinEstimator(fmin=80.0, fmax=200.0, hop_length=160)
    f0, voiced = est.estimate(audio, sr)
    assert len(f0) > 0
    voiced_f0 = f0[voiced]
    if len(voiced_f0) > 0:
        assert np.median(voiced_f0) == pytest.approx(f1, rel=0.05)
