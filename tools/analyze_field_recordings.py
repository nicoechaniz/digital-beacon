"""Analyze field recordings for tonal center and spectral landmarks.

Computes long-term average spectrum, detects prominent peaks, and proposes a
tonal center that can be used to tune the Harmonic Beacon (f1). Designed for
ambient nature recordings (water, frogs, insects) rather than voiced speech.

Usage:
    .venv/bin/python tools/analyze_field_recordings.py ~/Music/field-recordings/wav
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import numpy as np

log = logging.getLogger("analyze_field")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    import librosa
    import soundfile as sf
    from scipy.signal import find_peaks
    from scipy.ndimage import gaussian_filter1d
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    _HAVE_DEPS = True
except Exception as exc:
    _HAVE_DEPS = False
    _DEPS_ERROR = exc

NFFT = 16384
HOP = 4096
TOP_PEAKS = 8


def load_mono(wav_path: Path, target_sr: int = 48000):
    info = sf.info(wav_path)
    if info.samplerate != target_sr or info.channels != 1:
        log.info("Resampling/converting %s: sr=%d ch=%d", wav_path.name, info.samplerate, info.channels)
    y, sr = librosa.load(wav_path, sr=target_sr, mono=True)
    return y.astype(np.float32), sr


def long_term_spectrum(y, sr, n_fft=NFFT, hop=HOP):
    S = np.abs(librosa.stft(y, n_fft=n_fft, hop_length=hop))
    power = (S ** 2).mean(axis=1)
    freqs = librosa.fft_frequencies(sr=sr, n_fft=n_fft)
    return freqs, power


def detect_peaks(freqs, power, n_top=TOP_PEAKS, min_hz=20, max_hz=8000, prominence_db=6):
    """Find prominent spectral peaks in dB power spectrum."""
    power_db = 10.0 * np.log10(power + 1e-12)
    smooth_db = gaussian_filter1d(power_db, sigma=1.0)

    mask = (freqs >= min_hz) & (freqs <= max_hz)
    freqs_m = freqs[mask]
    db_m = smooth_db[mask]
    mask_idx = np.where(mask)[0]

    peaks, props = find_peaks(db_m, prominence=prominence_db, distance=5)
    if len(peaks) == 0:
        return []

    # Sort by prominence descending, keep top N
    order = np.argsort(props["prominences"])[::-1]
    selected = peaks[order[:n_top]]
    selected_db = db_m[selected]

    result = []
    for idx, db in zip(selected, selected_db):
        result.append({
            "hz": float(freqs_m[idx]),
            "db": float(db),
            "bin": int(mask_idx[idx]),
        })
    result.sort(key=lambda p: p["hz"])
    return result


def spectral_metrics(freqs, power):
    power = np.asarray(power)
    total = power.sum()
    if total <= 0:
        return {}
    centroid = float(np.sum(freqs * power) / total)
    # median
    cum = np.cumsum(power)
    median = float(freqs[np.searchsorted(cum, total / 2.0)])
    # bandwidth (std)
    bandwidth = float(np.sqrt(np.sum(((freqs - centroid) ** 2) * power) / total))
    # rolloff 85%
    rolloff = float(freqs[np.searchsorted(cum, total * 0.85)])
    # energy in low/mid/high bands
    bands = {
        "sub_80": float(power[freqs < 80].sum() / total),
        "80_250": float(power[(freqs >= 80) & (freqs < 250)].sum() / total),
        "250_1000": float(power[(freqs >= 250) & (freqs < 1000)].sum() / total),
        "1000_4000": float(power[(freqs >= 1000) & (freqs < 4000)].sum() / total),
        "4000_plus": float(power[freqs >= 4000].sum() / total),
    }
    return {
        "centroid_hz": centroid,
        "median_hz": median,
        "bandwidth_hz": bandwidth,
        "rolloff_85_hz": rolloff,
        "band_energy": bands,
    }


def harmonic_f1_search(peaks, freqs, power, f1_min=20.0, f1_max=200.0, n_harmonics=32, tolerance_hz=15.0, tolerance_frac=0.03):
    """Find f1 in [f1_min, f1_max] that best explains detected spectral peaks.

    Strategy: generate a dense grid of candidate f1 values. For each, count how
    many of the top peaks align with integer harmonics n·f1, weighted by peak
    power. Also reward f1 candidates whose harmonics land near high-energy bins.
    Returns the best f1 and a diagnostic dict.
    """
    if not peaks:
        return None, {}

    peak_hz = np.array([p["hz"] for p in peaks])
    peak_db = np.array([p["db"] for p in peaks])
    # normalize peak weights to 0..1
    peak_w = np.maximum(peak_db - peak_db.min(), 0)
    if peak_w.sum() > 0:
        peak_w = peak_w / peak_w.max()
    else:
        peak_w = np.ones_like(peak_hz)

    power_db = 10.0 * np.log10(power + 1e-12)
    power_db = power_db - power_db.min()

    # Grid: every FFT bin in [f1_min, f1_max] plus sub-grid interpolation
    mask = (freqs >= f1_min) & (freqs <= f1_max)
    candidates = freqs[mask]
    if len(candidates) < 2:
        candidates = np.linspace(f1_min, f1_max, 200)

    scores = []
    for f1 in candidates:
        tol = max(tolerance_hz, f1 * tolerance_frac)
        score = 0.0
        matched = 0
        for hz, w in zip(peak_hz, peak_w):
            # find nearest integer harmonic
            n = max(1, int(round(hz / f1)))
            # allow multiple harmonics per peak (e.g. 2f1 and 3f1 both hit region)
            for nn in range(max(1, n - 2), n + 3):
                expected = nn * f1
                if expected > freqs[-1]:
                    break
                err = abs(hz - expected)
                if err <= tol:
                    score += w * (1 - err / tol)
                    matched += 1
                    break
        # Also reward energy at the harmonic bins themselves (global spectrum)
        for n in range(1, min(n_harmonics + 1, int(freqs[-1] / f1) + 1)):
            expected = n * f1
            idx = int(np.searchsorted(freqs, expected))
            idx = max(0, min(idx, len(freqs) - 1))
            # nearest few bins
            window = slice(max(0, idx - 1), min(len(freqs), idx + 2))
            local_db = power_db[window].max()
            score += local_db * 0.01  # small continuous reward
        scores.append(score)

    scores = np.array(scores)
    best_idx = int(np.argmax(scores))
    best_f1 = float(candidates[best_idx])

    # Build match details for the chosen f1
    tol = max(tolerance_hz, best_f1 * tolerance_frac)
    matches = []
    for p in peaks:
        hz = p["hz"]
        n = max(1, int(round(hz / best_f1)))
        matched_n = None
        for nn in range(max(1, n - 2), n + 3):
            if abs(hz - nn * best_f1) <= tol:
                matched_n = nn
                break
        matches.append({
            "hz": hz,
            "n": matched_n,
            "expected_hz": matched_n * best_f1 if matched_n else None,
        })

    return best_f1, {
        "candidates_hz": candidates.tolist(),
        "scores": scores.tolist(),
        "matches": matches,
    }


def analyze_file(wav_path: Path, out_dir: Path):
    log.info("Analyzing %s", wav_path.name)
    y, sr = load_mono(wav_path)
    duration = len(y) / sr

    freqs, power = long_term_spectrum(y, sr)
    metrics = spectral_metrics(freqs, power)
    peaks = detect_peaks(freqs, power)

    # Fallback tonal center search on raw power if harmonic search fails
    f1_candidate, f1_diag = harmonic_f1_search(peaks, freqs, power)
    reason = "harmonic_peak_alignment"
    if f1_candidate is None:
        power_db = 10.0 * np.log10(power + 1e-12)
        mask = (freqs >= 20.0) & (freqs <= 200.0)
        if mask.any():
            idx = np.argmax(power_db[mask])
            f1_candidate = float(freqs[mask][idx])
            reason = "strongest_bin_in_f1_range"
        else:
            f1_candidate = 40.0
            reason = "default_40hz"

    # Plot
    out_png = out_dir / f"{wav_path.stem}_spectrum.png"
    fig, ax = plt.subplots(figsize=(14, 6))
    power_db = 10.0 * np.log10(power + 1e-12)
    ax.plot(freqs, power_db, color="steelblue", linewidth=0.8)
    for p in peaks:
        ax.axvline(p["hz"], color="darkorange", linestyle="--", linewidth=0.8, alpha=0.7)
        ax.annotate(f"{p['hz']:.0f}", (p["hz"], p["db"]),
                    textcoords="offset points", xytext=(0, 8), ha="center",
                    fontsize=7, color="darkorange")
    ax.axvline(f1_candidate, color="green", linestyle="-", linewidth=1.5, alpha=0.8, label=f"proposed f1={f1_candidate:.1f} Hz")
    ax.set_xlim(20, 4000)
    ax.set_xscale("log")
    ax.set_xlabel("Frequency (Hz)")
    ax.set_ylabel("Power (dB)")
    ax.set_title(f"{wav_path.stem} — duration {duration:.1f}s")
    ax.legend()
    fig.tight_layout()
    fig.savefig(out_png, dpi=150)
    plt.close(fig)

    return {
        "label": wav_path.stem,
        "path": str(wav_path),
        "duration_s": duration,
        "sr": sr,
        "proposed_f1_hz": f1_candidate,
        "proposed_f1_reason": reason,
        "f1_harmonic_matches": f1_diag.get("matches", []),
        "peaks_hz": peaks,
        "metrics": metrics,
        "viz_png": str(out_png),
    }


def main():
    ap = argparse.ArgumentParser(description="Analyze field recordings for tonal center")
    ap.add_argument("path", help="Directory of WAV files or single WAV")
    ap.add_argument("--out-dir", default=str(Path.home() / "Music" / "field-recordings" / "analysis"))
    ap.add_argument("--json-out", default=str(Path.home() / "Music" / "field-recordings" / "analysis" / "field_analysis.json"))
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")

    if not _HAVE_DEPS:
        log.error("Missing dependencies: %s", _DEPS_ERROR)
        sys.exit(1)

    path = Path(args.path).expanduser().resolve()
    out_dir = Path(args.out_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    if path.is_dir():
        wavs = sorted(path.glob("*.wav"))
    elif path.is_file() and path.suffix.lower() == ".wav":
        wavs = [path]
    else:
        log.error("Path must be a WAV file or directory of WAVs: %s", path)
        sys.exit(1)

    if not wavs:
        log.error("No WAV files found")
        sys.exit(1)

    results = []
    for wav in wavs:
        try:
            results.append(analyze_file(wav, out_dir))
        except Exception as exc:
            log.error("Failed on %s: %s", wav.name, exc, exc_info=True)

    if results:
        print(json.dumps(results, indent=2, default=str))
        json_out = Path(args.json_out).expanduser().resolve()
        json_out.write_text(json.dumps(results, indent=2, default=str))
        log.info("JSON saved: %s", json_out)


if __name__ == "__main__":
    main()
