"""Analyze temporal stability of tonal center in field recordings.

Splits each recording into overlapping windows, runs the same harmonic_f1_search
used by analyze_field_recordings.py, and reports how stable the proposed f1 is
over time. This helps choose a global tuning for the beacon vs per-section tuning.

Usage:
    .venv/bin/python tools/analyze_temporal_stability.py
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import numpy as np

log = logging.getLogger("temporal_stability")

sys.path.insert(0, str(Path(__file__).resolve().parent))

from analyze_field_recordings import (
    load_mono,
    long_term_spectrum,
    detect_peaks,
    spectral_metrics,
    harmonic_f1_search,
)

WINDOW_S = 30.0
HOP_S = 15.0
NFFT = 16384
HOP = 4096


def analyze_window(y, sr, start_s, end_s):
    y_win = y[int(start_s * sr):int(end_s * sr)]
    if len(y_win) < sr * 5:  # need at least 5 seconds
        return None
    freqs, power = long_term_spectrum(y_win, sr, n_fft=NFFT, hop=HOP)
    peaks = detect_peaks(freqs, power)
    f1, diag = harmonic_f1_search(peaks, freqs, power)
    metrics = spectral_metrics(freqs, power)
    return {
        "start_s": float(start_s),
        "end_s": float(end_s),
        "proposed_f1_hz": f1 if f1 is not None else None,
        "peaks_hz": peaks,
        "metrics": metrics,
    }


def analyze_file(wav_path: Path):
    log.info("Analyzing %s", wav_path.name)
    y, sr = load_mono(wav_path, target_sr=48000)
    duration = len(y) / sr

    windows = []
    start = 0.0
    while start + WINDOW_S <= duration:
        win = analyze_window(y, sr, start, start + WINDOW_S)
        if win:
            windows.append(win)
        start += HOP_S

    # tail window if it has enough data
    if duration > WINDOW_S and (duration - WINDOW_S) % HOP_S != 0:
        win = analyze_window(y, sr, duration - WINDOW_S, duration)
        if win:
            windows.append(win)

    f1s = [w["proposed_f1_hz"] for w in windows if w["proposed_f1_hz"] is not None]
    if f1s:
        f1s_arr = np.array(f1s)
        stats = {
            "mean_hz": float(np.mean(f1s_arr)),
            "median_hz": float(np.median(f1s_arr)),
            "std_hz": float(np.std(f1s_arr)),
            "min_hz": float(np.min(f1s_arr)),
            "max_hz": float(np.max(f1s_arr)),
            "range_hz": float(np.max(f1s_arr) - np.min(f1s_arr)),
            "cv": float(np.std(f1s_arr) / np.mean(f1s_arr)) if np.mean(f1s_arr) > 0 else 0.0,
        }
    else:
        stats = {}

    return {
        "label": wav_path.stem,
        "duration_s": duration,
        "windows": windows,
        "stats": stats,
    }


def propose_global_f1(results):
    """Propose a global f1 that minimizes detuning effort across all stable samples."""
    all_f1s = []
    weights = []
    for r in results:
        f1s = [w["proposed_f1_hz"] for w in r["windows"] if w["proposed_f1_hz"] is not None]
        if not f1s:
            continue
        # Weight by inverse coefficient of variation (more stable = more weight)
        cv = r["stats"].get("cv", 1.0)
        weight = 1.0 / max(0.05, cv)
        for f in f1s:
            all_f1s.append(f)
            weights.append(weight)

    if not all_f1s:
        return {}

    all_f1s = np.array(all_f1s)
    weights = np.array(weights)
    weighted_mean = float(np.sum(all_f1s * weights) / np.sum(weights))
    weighted_std = float(np.sqrt(np.sum(weights * (all_f1s - weighted_mean) ** 2) / np.sum(weights)))

    # Also report simple median and common rounded values
    return {
        "weighted_mean_hz": weighted_mean,
        "weighted_std_hz": weighted_std,
        "median_hz": float(np.median(all_f1s)),
        "min_hz": float(np.min(all_f1s)),
        "max_hz": float(np.max(all_f1s)),
        "n_windows": len(all_f1s),
    }


def main():
    ap = argparse.ArgumentParser(description="Temporal stability analysis for field recordings")
    ap.add_argument("--wav-dir", default=str(Path.home() / "Music" / "field-recordings" / "wav"))
    ap.add_argument("--out", default=str(Path.home() / "Music" / "field-recordings" / "analysis" / "temporal_stability.json"))
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")

    wav_dir = Path(args.wav_dir).expanduser().resolve()
    wavs = sorted(wav_dir.glob("*.wav"))
    if not wavs:
        log.error("No WAV files found in %s", wav_dir)
        sys.exit(1)

    results = []
    for wav in wavs:
        try:
            results.append(analyze_file(wav))
        except Exception as exc:
            log.error("Failed on %s: %s", wav.name, exc, exc_info=True)

    global_f1 = propose_global_f1(results)

    output = {
        "window_s": WINDOW_S,
        "hop_s": HOP_S,
        "files": results,
        "proposed_global_f1": global_f1,
    }

    out_path = Path(args.out).expanduser().resolve()
    out_path.write_text(json.dumps(output, indent=2, default=str))
    log.info("Saved: %s", out_path)

    # Console summary
    print("\n=== Temporal stability summary ===")
    for r in results:
        stats = r.get("stats", {})
        print(f"{r['label']:30s} dur={r['duration_s']:6.1f}s  windows={len(r['windows']):3d}  "
              f"f1_mean={stats.get('mean_hz', 0):6.1f}  std={stats.get('std_hz', 0):5.1f}  "
              f"range={stats.get('range_hz', 0):5.1f}Hz  cv={stats.get('cv', 0):.2f}")
    if global_f1:
        print(f"\nProposed global f1: {global_f1['weighted_mean_hz']:.2f} Hz  "
              f"(weighted std={global_f1['weighted_std_hz']:.2f}, "
              f"median={global_f1['median_hz']:.2f}, range=[{global_f1['min_hz']:.1f}, {global_f1['max_hz']:.1f}])")


if __name__ == "__main__":
    main()
