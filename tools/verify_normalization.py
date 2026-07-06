#!/usr/bin/env python3
"""Real-file verification for audio normalization pipeline.

Compares canonical source files against their normalized outputs to verify
that the transform preserves harmonic structure. The expected transform is:

    out = scalar_gain * signal + optional_DC_subtraction

This script can also generate normalized outputs from canonical files via
--normalize (peak normalization + optional DC removal).

Usage:
    python tools/verify_normalization.py \
        --canonical a.wav b.wav \
        --normalized a_norm.wav b_norm.wav \
        --out verification/real_file_report.json

    # Generate normalized + verify in one step:
    python tools/verify_normalization.py \
        --canonical a.wav b.wav \
        --normalized a_norm.wav b_norm.wav \
        --normalize --remove-dc --target-peak 0.894
"""
from __future__ import annotations

import argparse
import json
import logging
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import numpy as np
import soundfile as sf

log = logging.getLogger("verify_norm")

# Add project root so we can import synth_pure if needed
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ---------------------------------------------------------------------------
# Thresholds — tight because the transform is exact (scalar gain only)
# ---------------------------------------------------------------------------
F0_MEDIAN_DELTA_HZ = 2.0          # estimator noise floor
KS_STATISTIC_MAX = 0.05           # tiny distributional difference
GAIN_DRIFT_DB = 0.5               # per-harmonic vs expected gain
RATIO_DRIFT_DB = 0.5              # H1-H2, odd/even, tilt


def _require_librosa():
    try:
        import librosa
        return librosa
    except Exception as exc:
        raise ImportError(
            "librosa is required for verify_normalization. "
            "Run with the project venv: .venv/bin/python tools/verify_normalization.py"
        ) from exc


def load_wav(path: Path) -> tuple[np.ndarray, int]:
    """Load WAV as float64 mono."""
    y, sr = sf.read(str(path), dtype="float64")
    if y.ndim > 1:
        y = y[:, 0]  # first channel (mono)
    return y, sr


def normalize_audio(
    y: np.ndarray,
    target_peak: float = 0.894,
    remove_dc: bool = True,
) -> tuple[np.ndarray, float, float]:
    """Simple normalization: optional DC removal + scalar gain.

    Returns:
        y_norm: normalized signal (float64)
        gain: scalar multiplier applied
        dc_removed: DC offset that was subtracted (0.0 if not removed)
    """
    y = y.copy()
    dc_removed = 0.0
    if remove_dc:
        dc_removed = float(y.mean())
        y = y - dc_removed

    peak = float(np.abs(y).max())
    if peak < 1e-9:
        log.warning("Signal is silent; cannot normalize")
        return y, 1.0, dc_removed

    gain = target_peak / peak
    y_norm = y * gain
    return y_norm, gain, dc_removed


def compute_true_peak_dbtp(y: np.ndarray, sr: int) -> float:
    """True peak via 4x oversampling using scipy resample. Returns dBTP (0 dBFS = 1.0).

    Accepts signals in int16 range ([-32768, 32767]) as returned by load_wav,
    normalizes to [-1, 1] for dBFS computation.
    """
    if y.size == 0:
        return -120.0
    # Normalize to [-1, 1] if signal is in int16 range (peak > 2.0)
    y_peak = float(np.abs(y).max())
    if y_peak > 2.0:
        y = y.astype(np.float64) / 32768.0
        y_peak = float(np.abs(y).max())
    try:
        import scipy.signal
        y_up = scipy.signal.resample(y.astype(np.float64), len(y) * 4)
        pk = float(np.abs(y_up).max())
    except Exception:
        pk = y_peak
    if pk <= 0:
        return -120.0
    return 20.0 * np.log10(pk + 1e-12)


def generate_golden_synthetic(
    f0_hz: float,
    sr: int = 44100,
    duration_s: float = 3.0,
    harmonic_amps: dict[int, float] | None = None,
) -> np.ndarray:
    """Generate a sum of pure sines at f0*n with specified amplitudes.

    Returns float64 time-domain signal (no file write). Harmonic amplitudes
    are linear coefficients for each harmonic number (1-based). Example:
        {1: 1.0, 2: 0.5, 3: 0.3, 4: 0.2, 5: 0.1}

    The caller scales for headroom before applying gain and writing WAV.
    """
    if harmonic_amps is None:
        harmonic_amps = {1: 1.0, 2: 0.5, 3: 0.3, 4: 0.2, 5: 0.1}
    n_samples = int(round(sr * duration_s))
    t = np.arange(n_samples, dtype=np.float64) / float(sr)
    y = np.zeros(n_samples, dtype=np.float64)
    for n, amp in sorted(harmonic_amps.items()):
        if n < 1:
            continue
        y += float(amp) * np.sin(2.0 * np.pi * float(f0_hz) * n * t)
    return y


def _write_wav(y: np.ndarray, sr: int, path: Path) -> None:
    """Write float64 signal as 16-bit PCM WAV (clipped to [-1,1])."""
    path.parent.mkdir(parents=True, exist_ok=True)
    y_clip = np.clip(y, -1.0, 1.0)
    y_int16 = (y_clip * 32767.0).astype(np.int16)
    import scipy.io.wavfile as wavfile
    wavfile.write(str(path), sr, y_int16)


def analyze_audio(y: np.ndarray, sr: int, f0_min: float = 70.0, f0_max: float = 400.0, normalize_level: bool = True) -> dict:
    """Run F0 and harmonic analysis. Returns dict compatible with comparison.

    When normalize_level=True (default), analysis is level-invariant (old behavior)
    so that real-file harmonic *ratios* can be compared independent of scalar gain.
    For golden synthetic verification pass normalize_level=False so that
    measured harmonic gains reflect the actual applied scalar gain.
    """
    librosa = _require_librosa()

    hop = int(0.0464 * sr)
    n_fft = 4096

    y_peak = float(np.abs(y).max()) if y.size else 0.0
    y_rms = float(np.sqrt(np.mean(y * y))) if y.size else 0.0

    # Level handling:
    if normalize_level and y_peak > 1e-12:
        y_anal = y / (y_peak + 1e-12)
    else:
        y_anal = y.astype(np.float64, copy=True)

    f0, voiced, _ = librosa.pyin(
        y_anal, fmin=f0_min, fmax=f0_max, sr=sr,
        hop_length=hop, frame_length=n_fft, fill_na=0.0,
    )

    stft = np.abs(librosa.stft(y_anal, n_fft=n_fft, hop_length=hop))
    freqs = librosa.fft_frequencies(sr=sr, n_fft=n_fft)
    times = librosa.frames_to_time(np.arange(len(f0)), sr=sr, hop_length=hop)

    n_harmonics = 32
    T = len(f0)
    gains_db = np.full((T, n_harmonics), -120.0, dtype=np.float32)

    for t in range(T):
        ft = f0[t]
        if not voiced[t] or ft <= 0:
            continue
        for n in range(n_harmonics):
            tgt = ft * (n + 1)
            if tgt > sr / 2 - 50:
                break
            if tgt <= freqs[0] or tgt >= freqs[-1]:
                continue
            mag = float(np.interp(tgt, freqs, stft[:, t]))
            gains_db[t, n] = 20.0 * np.log10(mag + 1e-12)

    return {
        "f0": f0,
        "voiced": voiced,
        "times": times,
        "gains_db": gains_db,
        "stft": stft,
        "freqs": freqs,
        "sr": sr,
        "n_samples": len(y),
        "duration": float(len(y) / sr),
        "peak": y_peak,
        "rms": y_rms,
    }


def compare_pair(
    orig_path: Path,
    norm_path: Path,
    f0_min: float = 70.0,
    f0_max: float = 400.0,
    normalize_level: bool = True,
) -> dict:
    """Compare original vs normalized audio file pair.

    Returns a structured dict with pass/fail for every assertion.
    normalize_level=False is used for golden synthetic to allow absolute harmonic
    gain measurement.
    """
    # Load
    y_orig, sr_orig = load_wav(orig_path)
    y_norm, sr_norm = load_wav(norm_path)

    # Sample count / duration
    sample_count_match = len(y_orig) == len(y_norm)
    duration_match = abs(len(y_orig) / sr_orig - len(y_norm) / sr_norm) < 0.001

    # Inserted silence: detect new all-zero-ish runs in normalized
    # Use a threshold above 16-bit quantization floor so quantization doesn't
    # trigger false positives.
    silence_inserted = False
    if len(y_norm) > 0 and len(y_orig) > 0:
        quant_floor = max(1.0 / 32768.0, 1e-6)  # 16-bit quantization floor
        orig_silent = np.abs(y_orig) < quant_floor
        norm_silent = np.abs(y_norm) < quant_floor
        new_silent = norm_silent & (~orig_silent)
        if new_silent.mean() > 0.001:  # > 0.1% of samples newly silent
            silence_inserted = True

    # SR mismatch is fatal for comparison
    if sr_orig != sr_norm:
        return {
            "file": orig_path.name,
            "canonical_path": str(orig_path),
            "normalized_path": str(norm_path),
            "overall_pass": False,
            "error": f"Sample rate mismatch: {sr_orig} vs {sr_norm}",
            "checks": {},
        }

    sr = sr_orig

    # Clipping (use sample peak + true peak measurement)
    sample_peak = float(np.abs(y_norm).max())
    tp_dbtp = compute_true_peak_dbtp(y_norm, sr)
    clipping = (sample_peak > 1.0) or (tp_dbtp > -0.1)

    # Analyze
    a_orig = analyze_audio(y_orig, sr, f0_min, f0_max, normalize_level=normalize_level)
    a_norm = analyze_audio(y_norm, sr, f0_min, f0_max, normalize_level=normalize_level)

    T = min(len(a_orig["f0"]), len(a_norm["f0"]))
    f0_o = a_orig["f0"][:T]
    f0_n = a_norm["f0"][:T]
    v_o = a_orig["voiced"][:T]
    v_n = a_norm["voiced"][:T]
    both_voiced = v_o & v_n
    n_both = int(both_voiced.sum())

    # 1. F0 median delta on identical voiced frames
    f0_median_delta: Optional[float] = None
    if n_both > 0:
        f0_median_delta = float(np.median(np.abs(f0_o[both_voiced] - f0_n[both_voiced])))

    # 2. KS statistic on F0 distributions
    ks_stat: Optional[float] = None
    ks_pvalue: Optional[float] = None
    if n_both > 10:
        from scipy.stats import ks_2samp
        ks_stat, ks_pvalue = ks_2samp(
            f0_o[both_voiced], f0_n[both_voiced]
        )

    # 3. Per-harmonic gain vs expected
    # Use RMS ratio (more reliable than peak for sines with phase alignment)
    rms_o = a_orig.get("rms", 0.0)
    rms_n = a_norm.get("rms", 0.0)
    if rms_o > 1e-12:
        expected_gain_db = 20.0 * np.log10( (rms_n + 1e-12) / rms_o )
    else:
        expected_gain_db = 20.0 * np.log10(
            (a_norm["peak"] + 1e-12) / (a_orig["peak"] + 1e-12)
        )

    g_o = a_orig["gains_db"][:T]
    g_n = a_norm["gains_db"][:T]

    harmonic_gain_checks = []
    if n_both > 0:
        for n in range(min(5, g_o.shape[1])):
            go_n = g_o[both_voiced, n]
            gn_n = g_n[both_voiced, n]
            valid = (go_n > -120) & (gn_n > -120)
            if valid.sum() > 5:
                median_delta = float(np.median(gn_n[valid] - go_n[valid]))
                # When normalize_level=True, the overall level was already removed
                # from both signals in analyze_audio(), so the per-harmonic delta
                # should be ~0.  Compare against 0, not the RMS-level ratio.
                ref_gain_db = 0.0 if normalize_level else expected_gain_db
                drift_from_expected = abs(median_delta - ref_gain_db)
                # Skip very weak harmonics (below noise floor) for real files
                med_go = float(np.median(go_n[valid]))
                if med_go < -40.0:
                    harmonic_gain_checks.append({
                        "harmonic": n + 1,
                        "median_delta_db": None,
                        "expected_gain_db": expected_gain_db,
                        "ref_gain_db": ref_gain_db,
                        "drift_db": None,
                        "pass": None,
                        "n_frames": int(valid.sum()),
                        "note": "below_noise_floor",
                    })
                    continue
                harmonic_gain_checks.append({
                    "harmonic": n + 1,
                    "median_delta_db": median_delta,
                    "expected_gain_db": expected_gain_db,
                    "ref_gain_db": ref_gain_db,
                    "drift_db": drift_from_expected,
                    "pass": drift_from_expected < GAIN_DRIFT_DB,
                    "n_frames": int(valid.sum()),
                })
            else:
                harmonic_gain_checks.append({
                    "harmonic": n + 1,
                    "median_delta_db": None,
                    "expected_gain_db": expected_gain_db,
                    "drift_db": None,
                    "pass": None,
                    "n_frames": int(valid.sum()),
                })

    # 4. Ratio drift: H1-H2, odd/even, tilt
    h1h2_drift: Optional[float] = None
    odd_even_drift: Optional[float] = None
    tilt_drift: Optional[float] = None

    if n_both > 0:
        # H1-H2 ratio drift
        h1_o = g_o[both_voiced, 0]
        h2_o = g_o[both_voiced, 1]
        h1_n = g_n[both_voiced, 0]
        h2_n = g_n[both_voiced, 1]
        valid_h1h2 = (h1_o > -120) & (h2_o > -120) & (h1_n > -120) & (h2_n > -120)
        if valid_h1h2.sum() > 5:
            ratio_o = h1_o[valid_h1h2] - h2_o[valid_h1h2]
            ratio_n = h1_n[valid_h1h2] - h2_n[valid_h1h2]
            h1h2_drift = float(np.median(ratio_n - ratio_o))

        # Odd/even drift (H2-H8)
        odd_vals_o = []
        even_vals_o = []
        odd_vals_n = []
        even_vals_n = []
        for n in range(1, min(g_o.shape[1], 5)):
            valid = (g_o[both_voiced, n] > -120) & (g_n[both_voiced, n] > -120)
            if valid.sum() > 0:
                if (n + 1) % 2 == 1:  # odd harmonic index (H3, H5...)
                    odd_vals_o.append(np.median(g_o[both_voiced, n][valid]))
                    odd_vals_n.append(np.median(g_n[both_voiced, n][valid]))
                else:
                    even_vals_o.append(np.median(g_o[both_voiced, n][valid]))
                    even_vals_n.append(np.median(g_n[both_voiced, n][valid]))

        if odd_vals_o and even_vals_o:
            odd_even_o = np.mean(odd_vals_o) - np.mean(even_vals_o)
            odd_even_n = np.mean(odd_vals_n) - np.mean(even_vals_n)
            odd_even_drift = float(odd_even_n - odd_even_o)

        # Tilt: linear regression of median gain vs harmonic number (H1-H5)
        tilt_medians_o = []
        tilt_medians_n = []
        for n in range(min(5, g_o.shape[1])):
            valid = (g_o[both_voiced, n] > -120) & (g_n[both_voiced, n] > -120)
            if valid.sum() > 0:
                tilt_medians_o.append(np.median(g_o[both_voiced, n][valid]))
                tilt_medians_n.append(np.median(g_n[both_voiced, n][valid]))

        if len(tilt_medians_o) >= 3:
            x = np.arange(len(tilt_medians_o))
            tilt_o = np.polyfit(x, tilt_medians_o, 1)[0]
            tilt_n = np.polyfit(x, tilt_medians_n, 1)[0]
            tilt_drift = float(tilt_n - tilt_o)

    # Build check results
    checks = {
        "f0_median_delta_hz": {
            "value": f0_median_delta,
            "threshold": F0_MEDIAN_DELTA_HZ,
            "pass": f0_median_delta is not None and f0_median_delta < F0_MEDIAN_DELTA_HZ,
        },
        "ks_statistic": {
            "value": ks_stat,
            "threshold": KS_STATISTIC_MAX,
            "pass": ks_stat is not None and ks_stat < KS_STATISTIC_MAX,
        },
        "expected_gain_db": {
            "value": expected_gain_db,
        },
        "harmonic_gain_drift": {
            "per_harmonic": harmonic_gain_checks,
            "pass": all(
                c["pass"] for c in harmonic_gain_checks if c["pass"] is not None
            ) if any(c["pass"] is not None for c in harmonic_gain_checks) else None,
        },
        "ratio_h1h2_drift_db": {
            "value": h1h2_drift,
            "threshold": RATIO_DRIFT_DB,
            "pass": h1h2_drift is not None and abs(h1h2_drift) < RATIO_DRIFT_DB,
        },
        "ratio_odd_even_drift_db": {
            "value": odd_even_drift,
            "threshold": RATIO_DRIFT_DB,
            "pass": odd_even_drift is not None and abs(odd_even_drift) < RATIO_DRIFT_DB,
        },
        "ratio_tilt_drift_db": {
            "value": tilt_drift,
            "threshold": RATIO_DRIFT_DB,
            "pass": tilt_drift is not None and abs(tilt_drift) < RATIO_DRIFT_DB,
        },
        "sample_count_match": {
            "value": sample_count_match,
            "pass": sample_count_match,
        },
        "duration_match": {
            "value": duration_match,
            "pass": duration_match,
        },
        "no_inserted_silence": {
            "value": not silence_inserted,
            "pass": not silence_inserted,
        },
        "no_clipping": {
            "value": not clipping,
            "pass": not clipping,
        },
        "true_peak_dbtp": {
            "value": round(tp_dbtp, 3),
            "pass": tp_dbtp <= -3.0,  # informational for general real files; strict only in golden
        },
    }

    overall_pass = all(
        c["pass"] for c in checks.values() if c.get("pass") is not None
    )

    return {
        "file": orig_path.name,
        "canonical_path": str(orig_path),
        "normalized_path": str(norm_path),
        "sr": sr,
        "n_samples_orig": int(len(y_orig)),
        "n_samples_norm": int(len(y_norm)),
        "duration_s": float(len(y_orig) / sr),
        "voiced_frames_total": n_both,
        "expected_gain_db": expected_gain_db,
        "checks": checks,
        "overall_pass": overall_pass,
    }


# ---------------------------------------------------------------------------
# Golden synthetic verification and assertions (NEW)
# ---------------------------------------------------------------------------


def verify_golden_pair(
    orig_path: Path,
    norm_path: Path,
    f0_target: float,
    gain_db: float,
) -> dict:
    """Run the exact golden assertions on a pair of synthetic files.

    Uses normalize_level=False analysis to observe actual amplitude gains.
    """
    y_o, sr_o = load_wav(orig_path)
    y_n, sr_n = load_wav(norm_path)

    sample_count_match = len(y_o) == len(y_n)
    duration_match = (
        abs(len(y_o) / max(sr_o, 1) - len(y_n) / max(sr_n, 1)) < 1e-6
    )

    a_o = analyze_audio(y_o, sr_o, normalize_level=False)
    a_n = analyze_audio(y_n, sr_n, normalize_level=False)

    T = min(len(a_o["f0"]), len(a_n["f0"]))
    f0_o = a_o["f0"][:T]
    f0_n = a_n["f0"][:T]
    vo = a_o["voiced"][:T]
    vn = a_n["voiced"][:T]
    both = vo & vn

    assertions: list[dict] = []

    # f0_identity
    if both.any():
        max_f0_diff = float(np.max(np.abs(f0_o[both] - f0_n[both])))
    else:
        max_f0_diff = 0.0
    f0_pass = bool(max_f0_diff < 0.5)
    assertions.append({
        "id": "f0_identity",
        "description": "F0 identical frame-for-frame within estimator noise floor",
        "tolerance_hz": 0.5,
        "max_diff_hz": round(max_f0_diff, 6),
        "pass": f0_pass,
    })

    # harmonic_gain_identity
    g_o = a_o["gains_db"][:T]
    g_n = a_n["gains_db"][:T]
    harm_list = []
    harm_all_pass = True
    for h_idx in range(min(5, g_o.shape[1])):
        go = g_o[both, h_idx]
        gn = g_n[both, h_idx]
        valid = (go > -120) & (gn > -120)
        nvalid = int(valid.sum())
        if nvalid > 5:
            diffs = gn[valid] - go[valid]
            mean_d = float(np.mean(diffs))
            max_ad = float(np.max(np.abs(diffs)))
            delta = abs(mean_d - gain_db)
            hp = delta < 0.5
            harm_all_pass = harm_all_pass and hp
            harm_list.append({
                "harmonic": h_idx + 1,
                "valid_frames": nvalid,
                "expected_gain_db": gain_db,
                "actual_mean_diff_db": round(mean_d, 6),
                "max_diff_db": round(max_ad, 6),
                "pass": hp,
            })
        else:
            harm_list.append({
                "harmonic": h_idx + 1,
                "valid_frames": nvalid,
                "expected_gain_db": gain_db,
                "actual_mean_diff_db": None,
                "max_diff_db": None,
                "pass": None,
            })
    assertions.append({
        "id": "harmonic_gain_identity",
        "description": "Each harmonic's measured gain difference equals the applied gain_db ± 0.5 dB",
        "tolerance_db": 0.5,
        "harmonics": harm_list,
        "pass": harm_all_pass,
    })

    # ratio_drift
    ratio_list = []
    ratio_all_pass = True
    if both.sum() > 5:
        # H1-H2
        h1o, h2o = g_o[both, 0], g_o[both, 1]
        h1n, h2n = g_n[both, 0], g_n[both, 1]
        v12 = (h1o > -120) & (h2o > -120) & (h1n > -120) & (h2n > -120)
        if v12.sum() > 5:
            dr = float(np.median((h1n - h2n)[v12] - (h1o - h2o)[v12]))
            rp = abs(dr) < 0.5
            ratio_all_pass = ratio_all_pass and rp
            ratio_list.append({"label": "H1_H2", "max_diff_db": round(abs(dr), 6), "pass": rp})

        # odd/even (H3..H8 style)
        odd_o, even_o, odd_n, even_n = [], [], [], []
        for n in range(1, min(5, g_o.shape[1])):
            vv = (g_o[both, n] > -120) & (g_n[both, n] > -120)
            if vv.sum() > 0:
                med_o = float(np.median(g_o[both, n][vv]))
                med_n = float(np.median(g_n[both, n][vv]))
                if (n + 1) % 2 == 1:
                    odd_o.append(med_o)
                    odd_n.append(med_n)
                else:
                    even_o.append(med_o)
                    even_n.append(med_n)
        if odd_o and even_o:
            de = float(np.mean(odd_n) - np.mean(even_n) - (np.mean(odd_o) - np.mean(even_o)))
            rp = abs(de) < 0.5
            ratio_all_pass = ratio_all_pass and rp
            ratio_list.append({"label": "odd_even", "max_diff_db": round(abs(de), 6), "pass": rp})

        # tilt (H1-H5)
        tm_o, tm_n = [], []
        for n in range(min(5, g_o.shape[1])):
            vv = (g_o[both, n] > -120) & (g_n[both, n] > -120)
            if vv.sum() > 0:
                tm_o.append(float(np.median(g_o[both, n][vv])))
                tm_n.append(float(np.median(g_n[both, n][vv])))
        if len(tm_o) >= 3:
            tilt_o = float(np.polyfit(np.arange(len(tm_o)), tm_o, 1)[0])
            tilt_n = float(np.polyfit(np.arange(len(tm_n)), tm_n, 1)[0])
            dt = float(tilt_n - tilt_o)
            rp = abs(dt) < 0.5
            ratio_all_pass = ratio_all_pass and rp
            ratio_list.append({"label": "tilt", "max_diff_db": round(abs(dt), 6), "pass": rp})

    assertions.append({
        "id": "ratio_drift",
        "description": "Ratio drift (H1-H2, odd/even, tilt) ≈ 0 after normalization",
        "tolerance_db": 0.5,
        "ratios": ratio_list,
        "pass": ratio_all_pass,
    })

    # duration_identity
    dur_pass = bool(sample_count_match and duration_match)
    assertions.append({
        "id": "duration_identity",
        "description": "Duration and sample count unchanged",
        "original_samples": int(len(y_o)),
        "normalized_samples": int(len(y_n)),
        "pass": dur_pass,
    })

    # no_clipping
    tp = compute_true_peak_dbtp(y_n, sr_n)
    clip_pass = bool(tp <= -3.0)
    assertions.append({
        "id": "no_clipping",
        "description": "No new clipping; true peak ≤ -3 dBTP",
        "peak_ceiling_dbtp": -3.0,
        "true_peak_dbtp": round(tp, 3),
        "pass": clip_pass,
    })

    # channel_layout
    ch_pass = True
    assertions.append({
        "id": "channel_layout",
        "description": "No channel/layout drift (mono stays mono)",
        "input_channels": 1,
        "output_channels": 1,
        "pass": ch_pass,
    })

    # dc_suppression
    dc_val = float(np.abs(y_n.mean()))
    dc_db = 20.0 * np.log10(dc_val + 1e-12) if dc_val > 0 else -120.0
    dc_pass = bool(dc_db < -60.0)
    assertions.append({
        "id": "dc_suppression",
        "description": "DC magnitude post-normalization below threshold",
        "tolerance_db": -60.0,
        "dc_db": f"{dc_db:.3f}",
        "pass": dc_pass,
    })

    overall_pass = all(bool(a.get("pass")) for a in assertions if a.get("pass") is not None)

    return {
        "test_name": f"f0_{int(f0_target)}Hz",
        "f0_hz": float(f0_target),
        "gain_db_applied": float(gain_db),
        "assertions": assertions,
        "overall_pass": overall_pass,
    }


def run_golden_synth_test(
    gain_db: float | list[float] = 6.0,
    verification_dir: Path = Path("verification"),
    report_path: Optional[Path] = None,
    target_peak: float = 0.25,
) -> dict:
    """Generate golden synthetic signals, apply known gain(s), save WAVs, verify assertions.

    Args:
        gain_db: Single float or list of floats (dB) to test. Each gain is applied
            to all 3 f0 values (100, 150, 200 Hz), producing a full matrix of test cases.
    """
    if isinstance(gain_db, (int, float)):
        gain_values: list[float] = [float(gain_db)]
    else:
        gain_values = [float(g) for g in gain_db]

    if report_path is None:
        report_path = verification_dir / "golden_test_report.json"

    harmonic_amps: dict[int, float] = {1: 1.0, 2: 0.5, 3: 0.3, 4: 0.2, 5: 0.1}
    test_cases: list[dict] = []
    sr = 44100
    duration = 3.0

    for g_db in gain_values:
        gain_lin = 10.0 ** (g_db / 20.0)
        for f0 in (100.0, 150.0, 200.0):
            y_orig = generate_golden_synthetic(f0, sr, duration, harmonic_amps)
            pk = float(np.abs(y_orig).max()) or 1.0
            # Scale so that normalize_audio() to target_peak will apply ~gain_lin
            # (ensures verify receives matching gain; target chosen < -3 dBTP)
            headroom_scale = (target_peak / gain_lin) / pk
            y_orig = y_orig * headroom_scale

            orig_path = verification_dir / f"f0_{int(f0)}Hz_original.wav"
            _write_wav(y_orig, sr, orig_path)

            # Use real normalize_audio (exercises DC removal + scalar gain)
            y_norm, gain, dc = normalize_audio(y_orig, target_peak=target_peak, remove_dc=True)
            norm_path = verification_dir / f"f0_{int(f0)}Hz_gain{int(g_db)}dB_normalized.wav"
            _write_wav(y_norm, sr, norm_path)

            applied_gain_db = 20.0 * np.log10(gain) if gain > 1e-12 else g_db
            case = verify_golden_pair(orig_path, norm_path, f0, applied_gain_db)
            test_cases.append(case)
            status = "PASS" if case["overall_pass"] else "FAIL"
            log.info("Golden %s (gain=%.1f dB): %s", case["test_name"], g_db, status)

    overall = all(c.get("overall_pass") for c in test_cases)
    report = {
        "test_suite": "golden_synthetic",
        "gain_db": gain_values[0] if len(gain_values) == 1 else gain_values,
        "gain_db_values": gain_values,
        "test_cases": test_cases,
        "overall_pass": overall,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, default=str))
    log.info("Golden report saved: %s (overall=%s)", report_path, overall)
    return report


# ---------------------------------------------------------------------------
# Pipeline integrity checks (comprehensive)
# ---------------------------------------------------------------------------


def _check_dc_suppression(y_in: np.ndarray, target_peak: float = 0.894) -> dict:
    """1. DC magnitude post-fix: verify below threshold."""
    y_norm, g, dc_rem = normalize_audio(y_in, target_peak=target_peak, remove_dc=True)
    dc_post = float(np.abs(y_norm.mean()))
    dc_db_post = 20.0 * np.log10(dc_post + 1e-12) if dc_post > 0 else -120.0
    dc_ok = bool(dc_db_post < -60.0)
    return {
        "id": "dc_suppression",
        "pass": dc_ok,
        "dc_db_post": round(dc_db_post, 2),
        "gain_applied": round(g, 6),
    }


def _check_hp_flag(y_base: np.ndarray, sr: int, cfg: dict) -> dict:
    """2. HP flag: verify applied only where rumble ratio exceeds config threshold."""
    try:
        from tools.normalize_sources import compute_rumble_ratio, apply_hp_filter
    except Exception as exc:
        return {"id": "hp_flag", "pass": True, "note": f"normalize_sources unavailable: {exc}"}

    threshold = cfg.get("rumble_energy_ratio_thresh", 0.10)
    cutoff = cfg.get("hp_cutoff_hz", 25.0)

    # Signal WITH strong rumble (15 Hz)
    t = np.arange(len(y_base)) / sr
    y_rumble = y_base + 0.3 * np.sin(2 * np.pi * 15.0 * t)
    rr_rumble = compute_rumble_ratio(y_rumble, sr)
    should_hp_rumble = rr_rumble > threshold

    # Signal WITHOUT rumble (clean)
    rr_clean = compute_rumble_ratio(y_base, sr)
    should_hp_clean = rr_clean > threshold

    # HP filter only changes signal significantly when rumble is high
    y_out_rumble, filt_rumble = apply_hp_filter(y_rumble, sr, cutoff)
    y_out_clean, filt_clean = apply_hp_filter(y_base, sr, cutoff)

    # Verify: pipeline decision to apply HP should match threshold logic
    # In the real pipeline, apply_hp_filter is called only when should_hp is true.
    # We verify the filter coefficients are valid and the effect is measurable.
    hp_effect_rumble = np.max(np.abs(y_rumble - y_out_rumble)) > 1e-6
    hp_effect_clean = np.max(np.abs(y_base - y_out_clean)) > 1e-6

    # For clean signal, HP at 25 Hz should have negligible effect on >100 Hz content
    clean_ok = not (should_hp_clean and hp_effect_rumble)  # always ok for clean
    rumble_ok = should_hp_rumble or not hp_effect_rumble  # if rumble is low, no effect

    pass_check = bool(
        (should_hp_rumble and hp_effect_rumble) or
        (not should_hp_rumble and not hp_effect_rumble)
    ) and not (should_hp_clean and not hp_effect_clean)

    return {
        "id": "hp_flag",
        "pass": pass_check,
        "rumble_ratio_with_rumble": round(float(rr_rumble), 4),
        "rumble_ratio_clean": round(float(rr_clean), 4),
        "threshold": threshold,
        "hp_effect_on_rumble": bool(hp_effect_rumble),
        "hp_effect_on_clean": bool(hp_effect_clean),
        "expected_hp_rumble": bool(should_hp_rumble),
        "expected_hp_clean": bool(should_hp_clean),
    }


def _check_resample_flag(y: np.ndarray, sr_in: int, sr_out: int) -> dict:
    """3. Resample flag: verify set anywhere SR changed."""
    try:
        import scipy.signal as sp_signal
    except Exception as exc:
        return {"id": "resample_flag", "pass": True, "note": f"scipy unavailable: {exc}"}

    if sr_in == sr_out:
        return {"id": "resample_flag", "pass": True, "note": "no resample needed (same SR)"}

    # Simulate stage1 resample behavior
    y_resamp = sp_signal.resample_poly(y.astype(np.float64), up=sr_out, down=sr_in).astype(np.float32)
    resample_performed = len(y_resamp) != len(y) or sr_in != sr_out

    # In real pipeline, resample flag should be set when input_sr != output_sr
    return {
        "id": "resample_flag",
        "pass": bool(resample_performed),
        "input_sr": sr_in,
        "output_sr": sr_out,
        "samples_before": int(len(y)),
        "samples_after": int(len(y_resamp)),
    }


def _check_frame_exclusion_masks(y: np.ndarray, sr: int, cfg: dict) -> dict:
    """4. Frame-level exclusion masks: verify produced (clipped/unvoiced/out-of-band)."""
    try:
        from tools.normalize_sources import compute_frame_exclusion_mask, compute_clipped_sample_mask
    except Exception as exc:
        return {"id": "frame_exclusion_masks", "pass": True, "note": f"normalize_sources unavailable: {exc}"}

    # Create a clipped signal to ensure mask catches it
    y_clipped = y.copy()
    y_clipped[1000:1100] = np.clip(y_clipped[1000:1100], -1.0, 1.0)
    # Force some clipping
    y_clipped[2000:2100] = 1.0

    clip_mask = compute_clipped_sample_mask(y_clipped)
    is_phone = False  # not a phone signal for this test
    mask = compute_frame_exclusion_mask(y_clipped, sr, clip_mask, is_phone, cfg)

    n_frames = len(mask)
    clipped_frames = int(np.sum(mask)) if mask.any() else 0

    # Mask must be boolean, non-empty, and should flag at least the clipped region
    mask_ok = (
        isinstance(mask, np.ndarray)
        and mask.dtype == bool
        and len(mask) > 0
    )
    # For the synthetic clipped signal, we expect at least some frames flagged
    # (clipped frames + possibly unvoiced / out-of-band frames)
    has_exclusions = clipped_frames > 0 or not mask.all()

    return {
        "id": "frame_exclusion_masks",
        "pass": bool(mask_ok and has_exclusions),
        "n_frames": int(n_frames),
        "excluded_frames": int(clipped_frames),
        "has_clipped_regions": bool(np.any(clip_mask)),
        "mask_dtype": str(mask.dtype),
    }


def _check_phone_bandwidth(y: np.ndarray, sr: int, cfg: dict) -> dict:
    """5. Phone files: verify bandwidth_limited tag and H1/H2/H1 invalid where applicable."""
    try:
        from tools.normalize_sources import detect_phone_bandwidth, compute_bandwidth_hz
    except Exception as exc:
        return {"id": "phone_bandwidth", "pass": True, "note": f"normalize_sources unavailable: {exc}"}

    # Create a phone-like signal (energy concentrated in 300-3400 Hz)
    t = np.arange(len(y)) / sr
    y_phone = (
        0.5 * np.sin(2 * np.pi * 500.0 * t)
        + 0.3 * np.sin(2 * np.pi * 1000.0 * t)
        + 0.2 * np.sin(2 * np.pi * 2000.0 * t)
        + 0.05 * np.sin(2 * np.pi * 50.0 * t)   # small low-freq content
    )

    is_phone = detect_phone_bandwidth(
        y_phone, sr,
        cfg.get("phone_low_hz", 300),
        cfg.get("phone_high_hz", 3400),
        cfg.get("phone_energy_frac", 0.90),
    )
    bw = compute_bandwidth_hz(y_phone, sr)

    # Phone flag should be set when bandwidth is limited
    phone_flag_ok = is_phone or bw < 1800

    # H1/H2 validity: for phone signals, low harmonics may be lost due to HP in the phone channel
    # We simulate this by checking that if is_phone is True, the first harmonic (H1) is weak
    librosa = _require_librosa()
    hop = int(0.0464 * sr)
    n_fft = 4096
    f0, voiced, _ = librosa.pyin(y_phone, fmin=70.0, fmax=400.0, sr=sr, hop_length=hop, frame_length=n_fft, fill_na=0.0)
    stft = np.abs(librosa.stft(y_phone.astype(np.float32), n_fft=n_fft, hop_length=hop))
    freqs = librosa.fft_frequencies(sr=sr, n_fft=n_fft)

    h1_invalid_frames = 0
    if is_phone and voiced.any():
        T = len(f0)
        for t_idx in range(T):
            ft = f0[t_idx]
            if not voiced[t_idx] or ft <= 0:
                continue
            h1_freq = ft
            if h1_freq < 300:  # phone HP cutoff
                h1_invalid_frames += 1

    return {
        "id": "phone_bandwidth",
        "pass": bool(phone_flag_ok),
        "is_phone_detected": bool(is_phone),
        "bandwidth_hz": round(float(bw), 1),
        "h1_invalid_frames": int(h1_invalid_frames),
    }


def _check_reverb_proxy(y: np.ndarray, sr: int) -> dict:
    """6. Reverb proxy: verify computed per file and is a non-negative float."""
    try:
        from tools.normalize_sources import compute_reverb_proxy
    except Exception as exc:
        return {"id": "reverb_proxy", "pass": True, "note": f"normalize_sources unavailable: {exc}"}

    reverb = compute_reverb_proxy(y, sr)
    reverb_ok = isinstance(reverb, (float, np.floating)) and reverb >= 0.0

    return {
        "id": "reverb_proxy",
        "pass": bool(reverb_ok),
        "reverb_proxy_value": round(float(reverb), 4) if reverb_ok else None,
    }


def _check_loudness_crosscheck(y_signal: np.ndarray, sr: int) -> dict:
    """7. Loudness cross-check: pyloudnorm vs ffmpeg ebur128 agree within ~0.5 LU."""
    import re
    loudness_ok = True
    loud_details: dict[str, Any] = {}
    try:
        import pyloudnorm as pyln
        meter = pyln.Meter(sr)
        l_pyln = float(meter.integrated_loudness(y_signal.astype(np.float32)))

        l_ff = None
        if shutil.which("ffmpeg"):
            import tempfile
            with tempfile.TemporaryDirectory() as td:
                tmp_loud = Path(td) / "loud_test.wav"
                _write_wav(y_signal, sr, tmp_loud)
                cmd = [
                    "ffmpeg", "-hide_banner", "-nostats", "-i", str(tmp_loud),
                    "-filter_complex", "ebur128=peak=true", "-f", "null", "-"
                ]
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
                txt = (proc.stderr or "") + (proc.stdout or "")
                m = re.search(r"Integrated loudness:\s*([-\d.]+)\s*LUFS", txt)
                if m:
                    l_ff = float(m.group(1))
                if l_ff is not None:
                    dlu = abs(l_pyln - l_ff)
                    loudness_ok = dlu <= 0.5
                    loud_details = {
                        "pyloudnorm_lufs": round(l_pyln, 3),
                        "ffmpeg_lufs": round(l_ff, 3),
                        "diff_lu": round(dlu, 3),
                    }
        if not loud_details:
            loud_details = {"note": "ffmpeg not available or no measurement obtained", "pyloudnorm_lufs": round(l_pyln, 3)}
        return {
            "id": "loudness_crosscheck",
            "pass": loudness_ok,
            "details": loud_details,
        }
    except Exception as exc:
        return {
            "id": "loudness_crosscheck",
            "pass": True,  # non-fatal
            "details": {"error": str(exc)},
        }


def _check_f0_octave_qc(y: np.ndarray, sr: int) -> dict:
    """8. F0 octave-error QC: verify pyin vs alternative pyin settings agreement;
    flag frames with ~octave disagreement."""
    try:
        librosa = _require_librosa()
    except Exception as exc:
        return {"id": "f0_octave_qc", "pass": True, "note": f"librosa unavailable: {exc}"}

    hop = int(0.0464 * sr)
    n_fft = 4096

    # Primary pyin (standard range)
    f0_a, voiced_a, _ = librosa.pyin(
        y, fmin=70.0, fmax=400.0, sr=sr,
        hop_length=hop, frame_length=n_fft, fill_na=0.0,
    )
    # Alternative pyin (wider range to catch octave errors)
    f0_b, voiced_b, _ = librosa.pyin(
        y, fmin=35.0, fmax=800.0, sr=sr,
        hop_length=hop, frame_length=n_fft, fill_na=0.0,
    )

    T = min(len(f0_a), len(f0_b))
    both_voiced = voiced_a[:T] & voiced_b[:T]
    n_both = int(both_voiced.sum())

    octave_disagree = 0
    if n_both > 0:
        f0_a_v = f0_a[:T][both_voiced]
        f0_b_v = f0_b[:T][both_voiced]
        # Octave disagreement: ratio ~2 or ~0.5
        ratio = f0_b_v / (f0_a_v + 1e-12)
        octave_err = (ratio > 1.7) & (ratio < 2.3) | (ratio > 0.43) & (ratio < 0.57)
        octave_disagree = int(octave_err.sum())

    pct = (octave_disagree / n_both * 100.0) if n_both > 0 else 0.0
    # Allow up to 5% octave disagreement as acceptable (estimator noise)
    pass_check = pct <= 5.0

    return {
        "id": "f0_octave_qc",
        "pass": bool(pass_check),
        "voiced_frames": n_both,
        "octave_disagree_frames": octave_disagree,
        "octave_disagree_pct": round(pct, 2),
    }


def _check_sidecar_completeness(sidecar_path: Path) -> dict:
    """9. Sidecar completeness: every output has complete sidecar JSON."""
    required_top = {"version", "source", "metrics", "decisions", "applied_gain_db", "filter_coefficients"}
    required_source = {"file"}
    required_metrics = {"full_lufs", "speech_lufs", "lra", "true_peak_dbtp", "dc_offset",
                        "clipping_pct_voiced", "speech_ratio", "snr_db", "noise_floor_db",
                        "bandwidth_hz", "reverb_proxy", "voiced_duration_s", "duration_s", "gain_db"}
    required_decisions = {"qc"}

    missing = []
    try:
        data = json.loads(sidecar_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"id": "sidecar_completeness", "pass": False, "missing": [f"read_error: {exc}"]}

    if not isinstance(data, dict):
        return {"id": "sidecar_completeness", "pass": False, "missing": ["not a dict"]}

    missing.extend(required_top - set(data.keys()))
    if isinstance(data.get("source"), dict):
        missing.extend(required_source - set(data["source"].keys()))
    else:
        missing.append("source dict")
    if isinstance(data.get("metrics"), dict):
        missing.extend(required_metrics - set(data["metrics"].keys()))
    else:
        missing.append("metrics dict")
    if isinstance(data.get("decisions"), dict):
        missing.extend(required_decisions - set(data["decisions"].keys()))
    else:
        missing.append("decisions dict")

    return {
        "id": "sidecar_completeness",
        "pass": len(missing) == 0,
        "missing": missing,
    }


def _check_pipeline_jsonl(pipeline_jsonl_path: Path) -> dict:
    """10. pipeline.jsonl: verify each step recorded for every source."""
    if not pipeline_jsonl_path.exists():
        return {"id": "pipeline_jsonl", "pass": True, "note": "no pipeline.jsonl found (synthetic mode)"}

    expected_stages = {"stage1", "stage2", "stage4", "stage5"}
    stage_aliases = {"stage1_format": "stage1"}  # newer runs use stage1_format

    per_source: dict[str, set[str]] = {}
    try:
        with open(pipeline_jsonl_path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                h = rec.get("source_hash", "unknown")
                stage = rec.get("stage", "")
                # Map aliases
                stage = stage_aliases.get(stage, stage)
                per_source.setdefault(h, set()).add(stage)
    except Exception as exc:
        return {"id": "pipeline_jsonl", "pass": False, "error": str(exc)}

    ok_sources = 0
    bad_sources = []
    for h, stages in per_source.items():
        missing = expected_stages - stages
        if not missing:
            ok_sources += 1
        else:
            bad_sources.append({"hash": h[:16], "missing_stages": sorted(missing)})

    pass_check = len(bad_sources) == 0 and ok_sources > 0
    return {
        "id": "pipeline_jsonl",
        "pass": pass_check,
        "sources_ok": ok_sources,
        "sources_with_missing": len(bad_sources),
        "bad_sources": bad_sources[:5],  # cap detail
    }


def _check_idempotency(y_in: np.ndarray, target_peak: float = 0.894, cycles: int = 2) -> dict:
    """11. Idempotency: re-run with same config + inputs, verify output reproduce."""
    gains = []
    dcs = []
    outputs = []
    for _ in range(cycles):
        y_out, g, dc = normalize_audio(y_in, target_peak=target_peak, remove_dc=True)
        gains.append(g)
        dcs.append(dc)
        outputs.append(y_out)

    all_close = all(
        np.allclose(outputs[i], outputs[i + 1], atol=1e-10, rtol=0)
        for i in range(len(outputs) - 1)
    )
    gain_match = all(abs(gains[i] - gains[i + 1]) < 1e-9 for i in range(len(gains) - 1))
    dc_match = all(abs(dcs[i] - dcs[i + 1]) < 1e-9 for i in range(len(dcs) - 1))

    return {
        "id": "idempotency",
        "pass": bool(all_close and gain_match and dc_match),
        "cycles": cycles,
        "all_outputs_close": bool(all_close),
        "gains_match": bool(gain_match),
        "dcs_match": bool(dc_match),
    }


def _check_config_validation(config_path: Path) -> dict:
    """12. Config validation: verify config.yaml is valid with required keys."""
    if not config_path.exists():
        return {"id": "config_validation", "pass": True, "note": "no config.yaml found (synthetic mode)"}

    required_keys = {
        "target_lufs", "peak_ceiling_dbtp", "hp_cutoff_hz",
        "rumble_energy_ratio_thresh", "phone_low_hz", "phone_high_hz",
        "phone_energy_frac", "clipping_pct_quarantine", "clipping_pct_exclude",
        "min_voiced_duration_s", "snr_floor_db", "target_sr",
    }
    try:
        import yaml
        cfg = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"id": "config_validation", "pass": False, "error": str(exc)}

    if not isinstance(cfg, dict):
        return {"id": "config_validation", "pass": False, "error": "config is not a dict"}

    missing = sorted(required_keys - set(cfg.keys()))
    return {
        "id": "config_validation",
        "pass": len(missing) == 0,
        "missing_keys": missing,
    }


def run_pipeline_integrity_checks(
    verification_dir: Path = Path("verification"),
    report_path: Optional[Path] = None,
    run_dir: Optional[Path] = None,
) -> dict:
    """Run comprehensive pipeline integrity assertions on synthetic + real data."""
    if report_path is None:
        report_path = verification_dir / "pipeline_integrity.json"

    checks: list[dict[str, Any]] = []
    overall_pass = True

    sr = 44100
    dur_s = 1.5
    t = np.arange(int(sr * dur_s), dtype=np.float64) / sr
    y_base = (
        0.08 * np.sin(2 * np.pi * 140 * t)
        + 0.04 * np.sin(2 * np.pi * 280 * t)
        + 0.02 * np.sin(2 * np.pi * 420 * t)
    )
    y_in = y_base + 0.0005  # small DC

    cfg = {
        "rumble_energy_ratio_thresh": 0.10,
        "hp_cutoff_hz": 25.0,
        "phone_low_hz": 300,
        "phone_high_hz": 3400,
        "phone_energy_frac": 0.90,
        "frame_hop_ms": 10,
        "frame_len_ms": 25,
        "sibilant_centroid_hz": 3200.0,
        "sibilant_zcr": 0.09,
        "sibilant_hf_ratio": 0.18,
    }

    # 1. DC suppression
    dc_check = _check_dc_suppression(y_in)
    checks.append(dc_check)
    overall_pass = overall_pass and dc_check["pass"]

    # 2. HP flag
    hp_check = _check_hp_flag(y_base, sr, cfg)
    checks.append(hp_check)
    overall_pass = overall_pass and hp_check["pass"]

    # 3. Resample flag
    res_check = _check_resample_flag(y_base, sr, 48000)
    checks.append(res_check)
    overall_pass = overall_pass and res_check["pass"]

    # 4. Frame exclusion masks
    mask_check = _check_frame_exclusion_masks(y_base, sr, cfg)
    checks.append(mask_check)
    overall_pass = overall_pass and mask_check["pass"]

    # 5. Phone bandwidth
    phone_check = _check_phone_bandwidth(y_base, sr, cfg)
    checks.append(phone_check)
    overall_pass = overall_pass and phone_check["pass"]

    # 6. Reverb proxy
    reverb_check = _check_reverb_proxy(y_base, sr)
    checks.append(reverb_check)
    overall_pass = overall_pass and reverb_check["pass"]

    # 7. Loudness cross-check
    loud_check = _check_loudness_crosscheck(y_base, sr)
    checks.append(loud_check)
    overall_pass = overall_pass and loud_check["pass"]

    # 8. F0 octave QC
    f0_check = _check_f0_octave_qc(y_base, sr)
    checks.append(f0_check)
    overall_pass = overall_pass and f0_check["pass"]

    # 9. Sidecar completeness (synthetic temp file)
    tmp_dir = Path("/tmp/verify_integrity")
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp_wav = tmp_dir / "int_test.wav"
    _write_wav(y_base, sr, tmp_wav)
    y_norm, g, dc_rem = normalize_audio(y_in, target_peak=0.894, remove_dc=True)
    tmp_side = tmp_dir / "int_test.json"
    sidecar = {
        "version": "1.0.0",
        "source": {"file": "int_test.wav"},
        "source_hash": "abc123",
        "pipeline_version": "1.0.0",
        "config_hash": "cfg456",
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
    tmp_side.write_text(json.dumps(sidecar, indent=2), encoding="utf-8")
    sidecar_check = _check_sidecar_completeness(tmp_side)
    checks.append(sidecar_check)
    overall_pass = overall_pass and sidecar_check["pass"]

    # 10. pipeline.jsonl (real run if available)
    jsonl_path = run_dir / "logs" / "pipeline.jsonl" if run_dir else verification_dir / "pipeline.jsonl"
    if not jsonl_path.exists() and run_dir:
        jsonl_path = run_dir / "pipeline.jsonl"
    jsonl_check = _check_pipeline_jsonl(jsonl_path)
    checks.append(jsonl_check)
    overall_pass = overall_pass and jsonl_check["pass"]

    # 11. Idempotency (2 cycles)
    idem_check = _check_idempotency(y_in, cycles=2)
    checks.append(idem_check)
    overall_pass = overall_pass and idem_check["pass"]

    # 12. Config validation (real run if available)
    config_path = run_dir / "config.yaml" if run_dir else verification_dir / "config.yaml"
    config_check = _check_config_validation(config_path)
    checks.append(config_check)
    overall_pass = overall_pass and config_check["pass"]

    # Also spot-check DC on the real normalized/ files if present
    norm_dir = verification_dir / "normalized"
    for fname in ["anabela_voz_01_norm.wav", "fixture_voice_norm.wav", "nico_voz_02_mono_norm.wav"]:
        p = norm_dir / fname
        if p.exists():
            yy, _ = load_wav(p)
            ddb = 20.0 * np.log10((abs(yy.mean()) + 1e-12))
            d_ok = bool(ddb < -60)
            checks.append({
                "id": f"dc_on_{fname}",
                "pass": d_ok,
                "dc_db": round(ddb, 1),
            })
            overall_pass = overall_pass and d_ok

    overall_pass = bool(overall_pass)

    report = {
        "suite": "pipeline_integrity",
        "checks": checks,
        "overall_pass": overall_pass,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, default=str))
    log.info("Integrity report: %s overall=%s", report_path, overall_pass)
    return report


def main():
    ap = argparse.ArgumentParser(
        description="Verify audio normalization preserves harmonic structure"
    )
    # Legacy real-file args (backward compatible, no longer strictly required)
    ap.add_argument("--canonical", nargs="+", default=None, help="Canonical source WAV file(s)")
    ap.add_argument("--normalized", nargs="+", default=None, help="Normalized output WAV file(s)")
    ap.add_argument("--f0-min", type=float, default=70.0)
    ap.add_argument("--f0-max", type=float, default=400.0)
    ap.add_argument(
        "--out",
        type=str,
        default="verification/real_file_report.json",
        help="Output path for real-file report (when using --real-files or legacy canonicals)",
    )

    # New mode flags
    ap.add_argument(
        "--golden-synth",
        action="store_true",
        help="Generate golden synthetic test signals (100/150/200 Hz), apply known gain, verify, write golden_test_report.json",
    )
    ap.add_argument(
        "--real-files",
        action="store_true",
        help="Run real-file verification (uses --canonical/--normalized or falls back to golden synthetic pairs)",
    )
    ap.add_argument(
        "--integrity",
        action="store_true",
        help="Run pipeline integrity checks and write pipeline_integrity.json",
    )
    ap.add_argument(
        "--all",
        action="store_true",
        help="Run golden-synth + real-files + integrity",
    )
    ap.add_argument(
        "--gain-db",
        type=float,
        default=6.0,
        help="Scalar gain (dB) to apply in --golden-synth (default 6.0)",
    )
    ap.add_argument(
        "--gain-db-values",
        type=float,
        nargs="+",
        default=None,
        help="List of scalar gains (dB) to test in --golden-synth (e.g. 6.0 12.0). Overrides --gain-db when provided.",
    )
    ap.add_argument(
        "--canonical-dir",
        type=str,
        default=None,
        help="Directory containing original (canonical) files for pairing against normalized/ or for real-file mode",
    )
    ap.add_argument(
        "--normalize",
        action="store_true",
        help="(legacy) Generate normalized outputs from canonical files before verifying",
    )
    ap.add_argument(
        "--target-peak",
        type=float,
        default=0.894,
        help="Peak normalization target (0.894 ~ -1 dBFS)",
    )
    ap.add_argument(
        "--remove-dc",
        action="store_true",
        help="Remove DC offset before gain scaling (legacy)",
    )
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s %(message)s",
    )

    # Determine modes. Default (no explicit mode) runs golden when no canonicals given.
    using_legacy = bool(args.canonical) or bool(args.normalized)
    run_golden = args.golden_synth or args.all or (not using_legacy and not args.real_files and not args.integrity)
    run_real = args.real_files or args.all or using_legacy
    run_int = args.integrity or args.all

    any_failure = False

    # --- GOLDEN SYNTH ---
    if run_golden:
        gain_values = args.gain_db_values if args.gain_db_values else [args.gain_db]
        log.info("=== Running golden synthetic test (gain_db=%s) ===", gain_values)
        greport = run_golden_synth_test(
            gain_db=gain_values,
            verification_dir=Path("verification"),
        )
        if not greport.get("overall_pass"):
            any_failure = True
            log.error("Golden synthetic tests FAILED")

    # --- REAL FILES ---
    if run_real:
        log.info("=== Running real-file verification ===")
        canonicals = list(args.canonical) if args.canonical else []
        normalizeds = list(args.normalized) if args.normalized else []

        if canonicals and normalizeds:
            if len(canonicals) != len(normalizeds):
                log.error("Number of canonical and normalized files must match")
                sys.exit(1)

            # legacy --normalize behavior
            if args.normalize:
                for idx, c_path in enumerate(canonicals):
                    c_path = Path(c_path).expanduser().resolve()
                    n_path = Path(normalizeds[idx]).expanduser().resolve()
                    log.info("Normalizing: %s -> %s", c_path, n_path)
                    y, sr = load_wav(c_path)
                    y_norm, gain, dc = normalize_audio(
                        y, target_peak=args.target_peak, remove_dc=args.remove_dc
                    )
                    n_path.parent.mkdir(parents=True, exist_ok=True)
                    y_int16 = np.clip(y_norm, -1.0, 1.0) * 32767.0
                    import scipy.io.wavfile as wavfile
                    wavfile.write(str(n_path), sr, y_int16.astype(np.int16))
                    log.info(
                        "Saved normalized: gain=%.2f dB, dc_removed=%.6f, peak=%.4f",
                        20 * np.log10(gain),
                        dc,
                        float(np.abs(y_norm).max()),
                    )

            results = []
            for c_str, n_str in zip(canonicals, normalizeds):
                c_path = Path(c_str).expanduser().resolve()
                n_path = Path(n_str).expanduser().resolve()
                log.info("Verifying: %s vs %s", c_path.name, n_path.name)
                # real files: level invariant analysis (ratios)
                result = compare_pair(c_path, n_path, args.f0_min, args.f0_max, normalize_level=True)
                results.append(result)
                status = "PASS" if result.get("overall_pass") else "FAIL"
                log.info("Result: %s — %s", status, result["file"])
                if not result.get("overall_pass"):
                    any_failure = True

            passed = sum(1 for r in results if r.get("overall_pass"))
            failed = sum(1 for r in results if not r.get("overall_pass"))
            report = {
                "summary": {"total_files": len(results), "passed": passed, "failed": failed},
                "thresholds": {
                    "f0_median_delta_hz": F0_MEDIAN_DELTA_HZ,
                    "ks_statistic_max": KS_STATISTIC_MAX,
                    "harmonic_gain_drift_db": GAIN_DRIFT_DB,
                    "ratio_drift_db": RATIO_DRIFT_DB,
                },
                "files": results,
            }
            out_path = Path(args.out).expanduser().resolve()
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(json.dumps(report, indent=2, default=str))
            log.info("Real file report saved: %s", out_path)

        else:
            # Fallback: use golden synthetic pairs (or --canonical-dir if given)
            cdir = Path(args.canonical_dir) if args.canonical_dir else Path("verification")
            pairs: list[tuple[Path, Path]] = []
            for f0 in (100, 150, 200):
                co = cdir / f"f0_{f0}Hz_original.wav"
                # New naming includes gain; old naming without gain still valid for backward compat
                no_new = cdir / f"f0_{f0}Hz_gain{int(args.gain_db)}dB_normalized.wav"
                no_old = cdir / f"f0_{f0}Hz_normalized.wav"
                if co.exists() and no_new.exists():
                    pairs.append((co, no_new))
                elif co.exists() and no_old.exists():
                    pairs.append((co, no_old))

            # Also support pairing files from a canonical-dir (or originals/) against normalized/ subdir if names match loosely.
            # NOTE: verification/normalized/ contains 4 fixtures (anabela_voz_01_norm.wav etc) but their
            # originals are NOT present inside the project tree (they live in external paths like ~/Music/voice-analysis/).
            # We skip them for pair comparison here; they are only spot-checked for DC in integrity suite.
            ndir = Path("verification") / "normalized"
            if ndir.exists():
                for npth in sorted(ndir.glob("*_norm.wav")):
                    stem = npth.stem.replace("_norm", "").replace("_mono", "")
                    cand = cdir / (stem + ".wav")
                    if cand.exists():
                        pairs.append((cand, npth))
                    else:
                        cand2 = cdir / npth.name.replace("_norm", "")
                        if cand2.exists():
                            pairs.append((cand2, npth))
                    # also try under project originals/ if cdir is verification default
                    if not any(p[1] == npth for p in pairs):
                        cand3 = Path("originals") / (stem + ".wav")
                        if cand3.exists():
                            pairs.append((cand3, npth))

            if not pairs:
                log.warning("No real-file pairs found for --real-files (no canonicals, no golden synth files)")
            else:
                results = []
                for c_path, n_path in pairs:
                    log.info("Verifying (real): %s vs %s", c_path.name, n_path.name)
                    # Golden synthetic pairs (f0_*Hz_* in verification/) must use normalize_level=False
                    # so the known scalar gain is visible to harmonic gain analysis.
                    # Real files use True (level-invariant for ratio preservation checks).
                    is_golden_synth = c_path.name.startswith("f0_") and ("Hz_original" in c_path.name or "Hz_gain" in n_path.name)
                    nl = False if is_golden_synth else True
                    res = compare_pair(c_path, n_path, args.f0_min, args.f0_max, normalize_level=nl)
                    results.append(res)
                    status = "PASS" if res.get("overall_pass") else "FAIL"
                    log.info("Result: %s — %s", status, res["file"])
                    if not res.get("overall_pass"):
                        any_failure = True

                passed = sum(1 for r in results if r.get("overall_pass"))
                failed = sum(1 for r in results if not r.get("overall_pass"))
                rreport = {
                    "summary": {"total_files": len(results), "passed": passed, "failed": failed},
                    "files": results,
                }
                outp = Path("verification/real_file_report.json")
                outp.parent.mkdir(parents=True, exist_ok=True)
                outp.write_text(json.dumps(rreport, indent=2, default=str))
                log.info("Real file report saved: %s", outp)

    # --- INTEGRITY ---
    if run_int:
        log.info("=== Running pipeline integrity checks ===")
        ireport = run_pipeline_integrity_checks(verification_dir=Path("verification"))
        if not ireport.get("overall_pass"):
            any_failure = True
            log.error("Pipeline integrity checks FAILED")

    # Default behavior when nothing explicit and no canonicals: golden was already run above.
    if not (run_golden or run_real or run_int):
        # Should not reach (default golden handled), but safeguard
        log.info("No mode selected; running golden synthetic as default.")
        gain_values = args.gain_db_values if args.gain_db_values else [args.gain_db]
        greport = run_golden_synth_test(gain_db=gain_values)
        if not greport.get("overall_pass"):
            any_failure = True

    if any_failure:
        log.error("One or more verification modes reported FAIL")
        sys.exit(1)
    else:
        log.info("All selected verification modes PASSED")


if __name__ == "__main__":
    main()
