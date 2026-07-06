import numpy as np
import pytest

from nh_analysis import (
    compute_a4_16k,
    compute_h_series,
    compute_v4_linear,
    compute_v4_log,
)


def _sine_wave(freq, sr=16000, duration=0.5):
    t = np.linspace(0, duration, int(sr * duration), endpoint=False)
    return np.sin(2 * np.pi * freq * t).astype(np.float64)


def test_v4_linear_shape():
    f0 = np.array([100.0, 102.0, 101.0, 0.0, 100.0])
    voiced = np.array([True, True, True, False, True])
    desc = compute_v4_linear(f0, voiced, target_length=16)
    assert desc.shape == (1, 16, 4)


def test_v4_log_shape():
    f0 = np.array([100.0, 104.0, 102.0, 0.0, 100.0])
    voiced = np.array([True, True, True, False, True])
    desc = compute_v4_log(f0, voiced, target_length=16)
    assert desc.shape == (1, 16, 4)


def test_h_series_on_sine():
    sr = 16000
    f1 = 120.0
    audio = _sine_wave(f1, sr=sr, duration=0.4)
    f0 = np.full((1, 70), f1)
    voiced = np.ones((1, 70), dtype=bool)
    desc = compute_h_series(audio, f0, voiced, target_length=16, sr=sr)
    assert desc.shape == (1, 16, 8)
    # Harmonic concentration should be high for a pure sine; allow window leakage.
    assert np.mean(desc[:, :, 5]) > 0.2


def test_a4_16k_shape():
    sr = 16000
    audio = _sine_wave(440.0, sr=sr, duration=0.4)
    desc = compute_a4_16k(audio, target_length=16, sr=sr)
    assert desc.shape == (1, 16, 8)
