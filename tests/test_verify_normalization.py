"""Unit tests for golden synthetic signal generation and verification harness.

Run:
    /home/nicolas/Projects/digital-beacon/.venv/bin/python -m pytest tests/test_verify_normalization.py -v --tb=short
"""
from __future__ import annotations

import json
import sys
import tempfile
import wave
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.verify_normalization import (
    generate_golden_synthetic,
    verify_golden_pair,
    run_golden_synth_test,
    normalize_audio,
    compute_true_peak_dbtp,
    load_wav,
    _write_wav,
    _check_dc_suppression,
    _check_hp_flag,
    _check_resample_flag,
    _check_frame_exclusion_masks,
    _check_phone_bandwidth,
    _check_reverb_proxy,
    _check_loudness_crosscheck,
    _check_f0_octave_qc,
    _check_sidecar_completeness,
    _check_pipeline_jsonl,
    _check_idempotency,
    _check_config_validation,
    run_pipeline_integrity_checks,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def temp_dir() -> Path:
    """Provide a temporary directory that is cleaned up after the test."""
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


# ---------------------------------------------------------------------------
# generate_golden_synthetic
# ---------------------------------------------------------------------------

def test_golden_synthetic_basic_shape():
    """Generated signal must have correct length and be float64."""
    sr = 44100
    duration = 1.0
    y = generate_golden_synthetic(100.0, sr, duration)
    assert y.dtype == np.float64
    assert len(y) == int(sr * duration)


def test_golden_synthetic_harmonic_content():
    """FFT must show peaks at the expected harmonic frequencies."""
    sr = 44100
    f0 = 200.0
    y = generate_golden_synthetic(f0, sr, 2.0)
    # Hann-windowed FFT for clean peaks
    n = len(y)
    win = np.hanning(n)
    spec = np.fft.rfft(y * win)
    freqs = np.fft.rfftfreq(n, 1 / sr)
    mag = np.abs(spec)

    expected_harmonics = [1, 2, 3, 4, 5]
    expected_amps = [1.0, 0.5, 0.3, 0.2, 0.1]

    for h, expected_amp in zip(expected_harmonics, expected_amps):
        tgt = f0 * h
        idx = int(round(tgt / (sr / n)))
        # Search around the expected bin for the true peak
        search = mag[max(0, idx - 3) : min(len(mag), idx + 4)]
        peak_mag = search.max()
        # The peak should be clearly above the noise floor
        assert peak_mag > 0.1, f"Harmonic {h} at {tgt} Hz too weak"


def test_golden_synthetic_default_amps():
    """Default amplitudes must match the canonical H1-H5 values."""
    y = generate_golden_synthetic(100.0, 44100, 3.0)
    # The default harmonic_amps should be {1:1.0, 2:0.5, 3:0.3, 4:0.2, 5:0.1}
    # Check via FFT peak ratios
    n = len(y)
    win = np.hanning(n)
    spec = np.abs(np.fft.rfft(y * win))
    freqs = np.fft.rfftfreq(n, 1 / 44100)
    sr = 44100

    peaks = []
    for h in [1, 2, 3, 4, 5]:
        tgt = 100.0 * h
        idx = int(round(tgt / (sr / n)))
        search = spec[max(0, idx - 3) : min(len(spec), idx + 4)]
        peaks.append(search.max())

    ratios = [p / peaks[0] for p in peaks]
    expected = [1.0, 0.5, 0.3, 0.2, 0.1]
    for i, (r, e) in enumerate(zip(ratios, expected)):
        assert abs(r - e) < 0.05, f"H{i+1} ratio {r} != expected {e}"


# ---------------------------------------------------------------------------
# normalize_audio
# ---------------------------------------------------------------------------

def test_normalize_audio_scalar_only():
    """Simple gain-only transform must preserve the signal shape exactly."""
    y = np.sin(2.0 * np.pi * 100.0 * np.arange(44100) / 44100).astype(np.float64)
    y_norm, gain, dc = normalize_audio(y, target_peak=0.5, remove_dc=False)
    assert np.allclose(y_norm, y * gain, atol=1e-12)
    assert dc == 0.0


def test_normalize_audio_dc_removal():
    """DC removal must subtract the mean before scaling."""
    y = np.ones(1000, dtype=np.float64) * 0.1
    y_norm, gain, dc = normalize_audio(y, target_peak=0.5, remove_dc=True)
    assert abs(dc - 0.1) < 1e-12
    assert abs(y_norm.mean()) < 1e-12


def test_normalize_audio_peak_target():
    """Peak after normalization must match the target (within rounding)."""
    y = np.array([0.0, 0.5, -0.3, 0.2], dtype=np.float64)
    y_norm, gain, _ = normalize_audio(y, target_peak=0.8, remove_dc=False)
    assert abs(float(np.abs(y_norm).max()) - 0.8) < 1e-9


# ---------------------------------------------------------------------------
# compute_true_peak_dbtp
# ---------------------------------------------------------------------------

def test_true_peak_sine():
    """True peak of a full-scale sine should be ~0 dBTP (sample peaks already capture the maxima)."""
    sr = 44100
    t = np.arange(sr, dtype=np.float64) / sr
    y = np.sin(2.0 * np.pi * 1000.0 * t)
    tp = compute_true_peak_dbtp(y, sr)
    # Sample peak is 1.0 = 0 dBFS; true peak of a sine should be very close to 0 dBTP
    assert tp > -1.0
    assert tp < 1.0


def test_true_peak_silence():
    """Silent signal must report very low dBTP."""
    y = np.zeros(1000, dtype=np.float64)
    tp = compute_true_peak_dbtp(y, 44100)
    assert tp < -100.0


# ---------------------------------------------------------------------------
# verify_golden_pair
# ---------------------------------------------------------------------------

def test_verify_golden_pair_passes(temp_dir: Path):
    """A perfect scalar-gain pair must pass all golden assertions."""
    f0 = 150.0
    sr = 44100
    duration = 2.0
    gain_db = 6.0

    y_orig = generate_golden_synthetic(f0, sr, duration)
    pk = float(np.abs(y_orig).max()) or 1.0
    headroom = 0.25 / pk
    y_orig = y_orig * headroom

    orig_path = temp_dir / "orig.wav"
    norm_path = temp_dir / "norm.wav"
    _write_wav(y_orig, sr, orig_path)

    gain_lin = 10.0 ** (gain_db / 20.0)
    y_norm = y_orig * gain_lin
    _write_wav(y_norm, sr, norm_path)

    result = verify_golden_pair(orig_path, norm_path, f0, gain_db)
    assert result["overall_pass"] is True
    assert result["f0_hz"] == f0
    assert result["gain_db_applied"] == gain_db

    # All individual assertions must pass
    for a in result["assertions"]:
        assert a["pass"] is True, f"Assertion {a['id']} failed"


def test_verify_golden_pair_clipping_fail(temp_dir: Path):
    """If normalized signal clips, the no_clipping assertion must fail."""
    f0 = 100.0
    sr = 44100
    y_orig = generate_golden_synthetic(f0, sr, 1.0)
    # Full-scale original; any gain will clip
    pk = float(np.abs(y_orig).max()) or 1.0
    y_orig = y_orig / pk  # peak = 1.0

    orig_path = temp_dir / "orig.wav"
    norm_path = temp_dir / "norm.wav"
    _write_wav(y_orig, sr, orig_path)

    y_norm = y_orig * 2.0  # clip
    _write_wav(y_norm, sr, norm_path)

    result = verify_golden_pair(orig_path, norm_path, f0, 6.0)
    # overall may fail because of clipping
    clip_assertion = next(a for a in result["assertions"] if a["id"] == "no_clipping")
    assert clip_assertion["pass"] is False


# ---------------------------------------------------------------------------
# run_golden_synth_test (full harness)
# ---------------------------------------------------------------------------

def test_run_golden_synth_single_gain(temp_dir: Path):
    """Single gain setting must produce a report with 3 test cases and all pass."""
    report = run_golden_synth_test(
        gain_db=6.0,
        verification_dir=temp_dir,
        report_path=temp_dir / "report.json",
    )
    assert report["overall_pass"] is True
    assert len(report["test_cases"]) == 3
    assert report["gain_db_values"] == [6.0]
    for case in report["test_cases"]:
        assert case["overall_pass"] is True

    # Report file must exist and be valid JSON
    report_path = temp_dir / "report.json"
    assert report_path.exists()
    data = json.loads(report_path.read_text())
    assert data["test_suite"] == "golden_synthetic"


def test_run_golden_synth_multiple_gains(temp_dir: Path):
    """Multiple gain settings must produce a report with 3 f0 × N gain cases and all pass."""
    gains = [0.0, 6.0]
    report = run_golden_synth_test(
        gain_db=gains,
        verification_dir=temp_dir,
        report_path=temp_dir / "report_multi.json",
    )
    assert report["overall_pass"] is True
    assert len(report["test_cases"]) == 3 * len(gains)  # 3 f0 values per gain
    assert report["gain_db_values"] == gains
    for case in report["test_cases"]:
        assert case["overall_pass"] is True, f"Case {case['test_name']} at {case['gain_db_applied']} dB failed"

    # Verify report JSON structure
    data = json.loads((temp_dir / "report_multi.json").read_text())
    assert data["gain_db_values"] == gains
    assert data["overall_pass"] is True


def test_run_golden_synth_high_gain_no_clip(temp_dir: Path):
    """Even with high gain (12 dB), the harness must auto-scale headroom so no clipping occurs."""
    report = run_golden_synth_test(
        gain_db=[6.0, 12.0],
        verification_dir=temp_dir,
        report_path=temp_dir / "report_high.json",
    )
    assert report["overall_pass"] is True
    for case in report["test_cases"]:
        assert case["overall_pass"] is True


# ---------------------------------------------------------------------------
# load_wav / _write_wav roundtrip
# ---------------------------------------------------------------------------

def test_wav_roundtrip(temp_dir: Path):
    """Writing and reading a WAV must preserve the signal within quantization noise."""
    sr = 44100
    t = np.arange(sr, dtype=np.float64) / sr
    y = np.sin(2.0 * np.pi * 440.0 * t) * 0.5
    path = temp_dir / "roundtrip.wav"
    _write_wav(y, sr, path)
    y_back, sr_back = load_wav(path)
    assert sr_back == sr
    assert len(y_back) == len(y)
    # 16-bit quantization: peak error per sample is ~1/32767 ≈ 3e-5; allow margin for phase alignment
    assert np.max(np.abs(y_back - y)) < 6.0e-5


# ---------------------------------------------------------------------------
# Regression: exact-scalar-transform identity
# ---------------------------------------------------------------------------

def test_exact_scalar_transform_identity():
    """For a synthetic signal, normalization via scalar gain must be bit-exact reversible."""
    y = generate_golden_synthetic(200.0, 44100, 1.0)
    pk = float(np.abs(y).max()) or 1.0
    y = y / pk * 0.1  # small scale
    y_norm, gain, _ = normalize_audio(y, target_peak=0.5, remove_dc=False)
    y_recon = y_norm / gain
    assert np.allclose(y_recon, y, atol=1e-12, rtol=0)


# ---------------------------------------------------------------------------
# Pipeline integrity checks
# ---------------------------------------------------------------------------

@pytest.fixture
def synth_base():
    sr = 44100
    dur = 1.5
    t = np.arange(int(sr * dur), dtype=np.float64) / sr
    y = (
        0.08 * np.sin(2 * np.pi * 140 * t)
        + 0.04 * np.sin(2 * np.pi * 280 * t)
        + 0.02 * np.sin(2 * np.pi * 420 * t)
    )
    return y, sr


def test_check_dc_suppression(synth_base):
    y, sr = synth_base
    y_in = y + 0.0005
    result = _check_dc_suppression(y_in)
    assert result["pass"] is True
    assert result["dc_db_post"] < -60.0


def test_check_hp_flag(synth_base):
    y, sr = synth_base
    cfg = {
        "rumble_energy_ratio_thresh": 0.10,
        "hp_cutoff_hz": 25.0,
    }
    result = _check_hp_flag(y, sr, cfg)
    assert result["pass"] is True
    assert result["rumble_ratio_with_rumble"] > result["threshold"]
    assert result["rumble_ratio_clean"] < result["threshold"]


def test_check_resample_flag(synth_base):
    y, sr = synth_base
    result = _check_resample_flag(y, sr, sr)
    assert result["pass"] is True
    assert "same SR" in result.get("note", "")

    result2 = _check_resample_flag(y, sr, 48000)
    assert result2["pass"] is True
    assert result2["input_sr"] == 44100
    assert result2["output_sr"] == 48000


def test_check_frame_exclusion_masks(synth_base):
    y, sr = synth_base
    cfg = {
        "frame_hop_ms": 10,
        "frame_len_ms": 25,
        "sibilant_centroid_hz": 3200.0,
        "sibilant_zcr": 0.09,
        "sibilant_hf_ratio": 0.18,
    }
    result = _check_frame_exclusion_masks(y, sr, cfg)
    assert result["pass"] is True
    assert result["n_frames"] > 0
    assert result["mask_dtype"] == "bool"
    assert result["has_clipped_regions"] is True


def test_check_phone_bandwidth(synth_base):
    y, sr = synth_base
    cfg = {
        "phone_low_hz": 300,
        "phone_high_hz": 3400,
        "phone_energy_frac": 0.90,
    }
    result = _check_phone_bandwidth(y, sr, cfg)
    assert result["pass"] is True
    assert result["is_phone_detected"] is True


def test_check_reverb_proxy(synth_base):
    y, sr = synth_base
    result = _check_reverb_proxy(y, sr)
    assert result["pass"] is True
    assert result["reverb_proxy_value"] is not None
    assert result["reverb_proxy_value"] >= 0.0


def test_check_f0_octave_qc(synth_base):
    y, sr = synth_base
    result = _check_f0_octave_qc(y, sr)
    assert result["pass"] is True
    assert result["voiced_frames"] > 0
    assert result["octave_disagree_pct"] <= 5.0


def test_check_sidecar_completeness(temp_dir: Path):
    side = {
        "version": "1.0.0",
        "source": {"file": "test.wav"},
        "source_hash": "abc",
        "pipeline_version": "1.0.0",
        "config_hash": "cfg",
        "command_line": "test",
        "dependency_versions": {},
        "metrics": {
            "full_lufs": -23.0,
            "speech_lufs": -23.0,
            "lra": 0.0,
            "true_peak_dbtp": -3.0,
            "dc_offset": 0.0,
            "clipping_pct_voiced": 0.0,
            "speech_ratio": 1.0,
            "snr_db": 40.0,
            "noise_floor_db": -80.0,
            "bandwidth_hz": 8000.0,
            "reverb_proxy": 0.1,
            "voiced_duration_s": 1.5,
            "duration_s": 1.5,
            "gain_db": 0.0,
        },
        "decisions": {"qc": "accept"},
        "applied_gain_db": 0.0,
        "filter_coefficients": None,
    }
    p = temp_dir / "sidecar.json"
    p.write_text(json.dumps(side, indent=2))
    result = _check_sidecar_completeness(p)
    assert result["pass"] is True
    assert result["missing"] == []


def test_check_sidecar_completeness_missing_fields(temp_dir: Path):
    p = temp_dir / "bad.json"
    p.write_text(json.dumps({"version": "1.0.0"}, indent=2))
    result = _check_sidecar_completeness(p)
    assert result["pass"] is False
    assert len(result["missing"]) > 0


def test_check_pipeline_jsonl_no_file():
    result = _check_pipeline_jsonl(Path("/nonexistent/pipeline.jsonl"))
    assert result["pass"] is True
    assert "no pipeline.jsonl" in result["note"]


def test_check_idempotency(synth_base):
    y, sr = synth_base
    y_in = y + 0.0005
    result = _check_idempotency(y_in, cycles=2)
    assert result["pass"] is True
    assert result["cycles"] == 2
    assert result["all_outputs_close"] is True
    assert result["gains_match"] is True


def test_check_idempotency_three_cycles(synth_base):
    y, sr = synth_base
    y_in = y + 0.0005
    result = _check_idempotency(y_in, cycles=3)
    assert result["pass"] is True
    assert result["cycles"] == 3


def test_check_config_validation_no_file():
    result = _check_config_validation(Path("/nonexistent/config.yaml"))
    assert result["pass"] is True
    assert "no config.yaml" in result["note"]


def test_check_config_validation_good_file(temp_dir: Path):
    import yaml
    cfg = {
        "target_lufs": -23.0,
        "peak_ceiling_dbtp": -3.0,
        "hp_cutoff_hz": 25.0,
        "rumble_energy_ratio_thresh": 0.10,
        "phone_low_hz": 300,
        "phone_high_hz": 3400,
        "phone_energy_frac": 0.90,
        "clipping_pct_quarantine": 2.0,
        "clipping_pct_exclude": 8.0,
        "min_voiced_duration_s": 25.0,
        "snr_floor_db": 8.0,
        "target_sr": 44100,
    }
    p = temp_dir / "config.yaml"
    p.write_text(yaml.dump(cfg))
    result = _check_config_validation(p)
    assert result["pass"] is True
    assert result["missing_keys"] == []


def test_check_config_validation_missing_keys(temp_dir: Path):
    import yaml
    p = temp_dir / "bad_config.yaml"
    p.write_text(yaml.dump({"target_lufs": -23.0}))
    result = _check_config_validation(p)
    assert result["pass"] is False
    assert len(result["missing_keys"]) > 0


def test_run_pipeline_integrity_checks(temp_dir: Path):
    report = run_pipeline_integrity_checks(
        verification_dir=temp_dir,
        report_path=temp_dir / "integrity.json",
    )
    assert report["overall_pass"] is True
    assert report["suite"] == "pipeline_integrity"
    ids = {c["id"] for c in report["checks"]}
    expected = {
        "dc_suppression", "hp_flag", "resample_flag", "frame_exclusion_masks",
        "phone_bandwidth", "reverb_proxy", "loudness_crosscheck", "f0_octave_qc",
        "sidecar_completeness", "pipeline_jsonl", "idempotency", "config_validation",
    }
    assert expected.issubset(ids)
    assert (temp_dir / "integrity.json").exists()
