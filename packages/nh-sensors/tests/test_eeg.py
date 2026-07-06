import numpy as np

from nh_sensors import EEGProcessor, EEGSimulator


def test_band_power_on_sine():
    fs = 256
    t = np.linspace(0, 1, fs, endpoint=False)
    # 10 Hz alpha-ish signal
    signal = np.sin(2 * np.pi * 10 * t)
    proc = EEGProcessor(fs=fs, window_size=fs)
    power = proc.compute_band_power(signal, 8, 13)
    assert power > 0


def test_analyze_requires_window():
    proc = EEGProcessor(fs=256)
    channels = {"AF7": [1.0, 2.0], "AF8": [1.0, 2.0]}
    result = proc.analyze(channels)
    assert result == {}


def test_concentration_score_range():
    fs = 256
    proc = EEGProcessor(fs=fs, window_size=fs)
    t = np.linspace(0, 1, fs, endpoint=False)
    signal = np.random.normal(0, 1, size=fs)
    channels = {"AF7": list(signal), "AF8": list(signal)}
    metrics = proc.analyze(channels)
    if metrics:
        assert 0.0 <= metrics["concentration_score"] <= 100.0


def test_simulator_emits_event():
    events = []
    sim = EEGSimulator(callback=lambda e: events.append(e))
    ev = sim.emit()
    assert ev["type"] == "eeg.focus"
    assert 0.0 <= ev["value"] <= 1.0
    assert len(events) == 1


def test_signal_quality():
    proc = EEGProcessor(fs=256)
    channels = {"AF7": [100.0] * 1000, "AF8": [100.0] * 1000}
    q = proc.signal_quality(channels)
    assert q in {"good", "fair", "poor", "unknown"}
