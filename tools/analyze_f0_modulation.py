"""Analyze F0 modulation dynamics from voice recordings.

Reuses the synth_pure F0 pipeline (pyin + median + Gaussian + bridging)
so the analysis is identical to what the synthesis chain sees.

For each WAV, produces:
  - F0 statistics (mean, min, max, std, voiced %)
  - KDE-based cluster analysis (peaks, bandwidth, population per cluster)
  - Modulation dynamics (ΔF0 distribution, micro vs macro classification)
  - 4-panel PNG visualization
  - JSON summary

Usage:
    python tools/analyze_f0_modulation.py ~/Music/voice-analysis/nico_voz_sample_02.wav
    python tools/analyze_f0_modulation.py ~/Music/voice-analysis/  # all WAVs
    python tools/analyze_f0_modulation.py --f0-min 50 --f0-max 600 file.wav
"""
from __future__ import annotations

import argparse
import io
import json
import logging
import sys
import wave
from pathlib import Path
from typing import Optional

import numpy as np

log = logging.getLogger("analyze_f0")

# ---------------------------------------------------------------------------
# Reuse the synth_pure pipeline
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.synth_pure import prepare_analysis, N_HARMONICS as _NH

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    _HAVE_MPL = True
except Exception:
    _HAVE_MPL = False

try:
    from scipy.stats import gaussian_kde
    from scipy.signal import find_peaks
    _HAVE_SCIPY = True
except Exception:
    gaussian_kde = None  # type: ignore[assignment]
    find_peaks = None  # type: ignore[assignment]
    _HAVE_SCIPY = False


# ---------------------------------------------------------------------------
# Cluster analysis
# ---------------------------------------------------------------------------

def f0_clusters(f0: np.ndarray, voiced: np.ndarray, bw_method: float = 0.15) -> dict:
    """KDE-based cluster detection on F0 values.

    Args:
        f0: (T,) F0 in Hz (smoothed, bridged)
        voiced: (T,) boolean voiced mask
        bw_method: KDE bandwidth factor (lower = more peaks, higher = smoother).
                  0.15 is tuned for voice F0 (~100-400 Hz range).

    Returns dict with:
        peaks_hz: list of F0 values at KDE peaks (cluster centers)
        bandwidths: list of FWHM in Hz for each peak
        populations: list of fraction of voiced frames in each cluster
        kde_x, kde_y: KDE curve arrays (for plotting)
    """
    f0_v = f0[voiced]
    if len(f0_v) < 10 or not _HAVE_SCIPY:
        return {"peaks_hz": [], "bandwidths": [], "populations": [],
                "kde_x": np.array([]), "kde_y": np.array([])}

    # KDE on linear Hz (preserves Nico's 40Hz / 15Hz observation units)
    kde = gaussian_kde(f0_v, bw_method=bw_method)  # type: ignore[misc]
    x = np.linspace(max(20, f0_v.min() * 0.7), f0_v.max() * 1.2, 512)
    y = kde(x)

    # Peak detection
    peaks_idx, props = find_peaks(y, prominence=0.03 * y.max(), distance=15)  # type: ignore[misc]
    peaks_hz = x[peaks_idx].tolist()

    # FWHM per peak
    half_max = y[peaks_idx] / 2.0
    bandwidths = []
    for i, pk in enumerate(peaks_idx):
        hm = half_max[i]
        # left crossing
        left = pk
        while left > 0 and y[left] > hm:
            left -= 1
        # right crossing
        right = pk
        while right < len(y) - 1 and y[right] > hm:
            right += 1
        fwhm = x[right] - x[left]
        bandwidths.append(float(fwhm))

    # Population per cluster: fraction of voiced frames nearest to each peak
    if peaks_hz:
        peak_arr = np.array(peaks_hz)
        assignments = np.argmin(np.abs(f0_v[:, None] - peak_arr[None, :]), axis=1)
        populations = [float((assignments == i).mean()) for i in range(len(peaks_hz))]
    else:
        populations = []

    return {
        "peaks_hz": peaks_hz,
        "bandwidths": bandwidths,
        "populations": populations,
        "kde_x": x.tolist(),
        "kde_y": y.tolist(),
    }


# ---------------------------------------------------------------------------
# Modulation dynamics
# ---------------------------------------------------------------------------

def modulation_stats(f0: np.ndarray, voiced: np.ndarray, frame_duration_s: float) -> dict:
    """Compute ΔF0 statistics and micro/macro modulation classification.

    Args:
        f0: (T,) F0 in Hz
        voiced: (T,) boolean
        frame_duration_s: time between analysis frames (hop_length / sr)

    Returns dict with:
        df0_hz_per_s: (T-1,) array of ΔF0 in Hz/s (only where both frames voiced)
        micro_frac, macro_frac, mid_frac: fraction of transitions in each class
        micro_thresh, macro_thresh: thresholds used (Hz/s)
    """
    df0 = np.diff(f0)  # Hz per frame
    df0_rate = df0 / frame_duration_s  # Hz/s

    # Only consider transitions where both frames are voiced
    valid = voiced[:-1] & voiced[1:]
    df0_valid = np.abs(df0_rate[valid])

    # Thresholds in Hz/s (empirical, based on 40Hz jumps over ~100-200ms)
    micro_thresh = 60.0   # gentle within-state variation
    macro_thresh = 180.0  # emotional state shift

    micro = (df0_valid <= micro_thresh).sum()
    macro = (df0_valid >= macro_thresh).sum()
    mid = len(df0_valid) - micro - macro
    total = max(len(df0_valid), 1)

    return {
        "df0_hz_per_s_mean": float(np.mean(df0_valid)) if len(df0_valid) else 0.0,
        "df0_hz_per_s_std": float(np.std(df0_valid)) if len(df0_valid) else 0.0,
        "df0_hz_per_s_max": float(np.max(df0_valid)) if len(df0_valid) else 0.0,
        "micro_frac": float(micro / total),
        "macro_frac": float(macro / total),
        "mid_frac": float(mid / total),
        "micro_thresh_hz_s": micro_thresh,
        "macro_thresh_hz_s": macro_thresh,
        "df0_hz_per_s": df0_rate.tolist() if len(df0_rate) < 10000 else [],
        "valid_mask": valid.tolist() if len(valid) < 10000 else [],
    }


# ---------------------------------------------------------------------------
# Visualization
# ---------------------------------------------------------------------------

def plot_analysis(label: str, times: np.ndarray, f0: np.ndarray, voiced: np.ndarray,
                  clusters: dict, mod: dict, out_png: Path) -> None:
    """4-panel visualization: F0 timeseries, histogram+KDE, ΔF0 timeseries, ΔF0 histogram."""
    if not _HAVE_MPL:
        log.warning("matplotlib not available; skipping plot for %s", label)
        return

    fig, axes = plt.subplots(2, 2, figsize=(16, 10))
    fig.suptitle(f"F0 Modulation Analysis — {label}", fontsize=13, fontweight="bold")

    # ---- Panel 1: F0 over time ----
    ax = axes[0, 0]
    ax.plot(times, f0, alpha=0.35, color="steelblue", linewidth=0.6)
    f0_v = f0[voiced]
    t_v = times[voiced]
    ax.scatter(t_v, f0_v, s=2, alpha=0.5, color="steelblue", label="voiced F0")
    # Cluster bands
    if clusters["peaks_hz"]:
        for pk, bw in zip(clusters["peaks_hz"], clusters["bandwidths"]):
            ax.axhspan(pk - bw / 2, pk + bw / 2, alpha=0.08, color="orange")
        for pk in clusters["peaks_hz"]:
            ax.axhline(pk, color="darkorange", linestyle="--", linewidth=0.8, alpha=0.6)
    ax.set_xlabel("Time (s)")
    ax.set_ylabel("F0 (Hz)")
    ax.set_title("F0 Trajectory")
    ax.legend(fontsize=7)

    # ---- Panel 2: Histogram + KDE ----
    ax = axes[0, 1]
    if len(f0_v) > 0:
        ax.hist(f0_v, bins=80, alpha=0.4, color="steelblue", density=True)
        if len(clusters["kde_x"]) > 0:
            ax.plot(clusters["kde_x"], clusters["kde_y"], color="darkorange", linewidth=1.5, label="KDE")
        for pk in clusters["peaks_hz"]:
            ax.axvline(pk, color="darkorange", linestyle="--", linewidth=0.8, alpha=0.7)
            ax.annotate(f"{pk:.0f} Hz", (pk, ax.get_ylim()[1] * 0.85),
                        fontsize=8, ha="center", color="darkorange")
    ax.set_xlabel("F0 (Hz)")
    ax.set_ylabel("Density")
    ax.set_title("F0 Distribution + KDE")
    if clusters["peaks_hz"]:
        ax.legend(fontsize=7)

    # ---- Panel 3: ΔF0 over time ----
    ax = axes[1, 0]
    if len(mod.get("df0_hz_per_s", [])) > 0:
        df0 = np.array(mod["df0_hz_per_s"])
        valid = np.array(mod.get("valid_mask", [True] * len(df0)))
        t_df0 = times[1:]
        # Color-code by class
        abs_df0 = np.abs(df0)
        micro_msk = valid & (abs_df0 <= mod["micro_thresh_hz_s"])
        macro_msk = valid & (abs_df0 >= mod["macro_thresh_hz_s"])
        mid_msk = valid & ~micro_msk & ~macro_msk
        ax.scatter(t_df0[micro_msk], df0[micro_msk], s=1, alpha=0.4, color="green", label="micro")
        ax.scatter(t_df0[mid_msk], df0[mid_msk], s=1, alpha=0.4, color="gray", label="mid")
        ax.scatter(t_df0[macro_msk], df0[macro_msk], s=3, alpha=0.7, color="red", label="macro")
        ax.axhline(mod["micro_thresh_hz_s"], color="green", linestyle=":", linewidth=0.6, alpha=0.4)
        ax.axhline(-mod["micro_thresh_hz_s"], color="green", linestyle=":", linewidth=0.6, alpha=0.4)
        ax.axhline(mod["macro_thresh_hz_s"], color="red", linestyle=":", linewidth=0.6, alpha=0.4)
        ax.axhline(-mod["macro_thresh_hz_s"], color="red", linestyle=":", linewidth=0.6, alpha=0.4)
    ax.set_xlabel("Time (s)")
    ax.set_ylabel("ΔF0 (Hz/s)")
    ax.set_title("ΔF0 Rate (micro < green < macro > red)")
    ax.legend(fontsize=6, loc="upper right")

    # ---- Panel 4: ΔF0 histogram ----
    ax = axes[1, 1]
    if len(mod.get("df0_hz_per_s", [])) > 0:
        df0_v = np.abs(np.array(mod["df0_hz_per_s"])[np.array(mod.get("valid_mask", [True] * len(mod["df0_hz_per_s"])))])
        # Log bins to separate micro from macro
        if len(df0_v) > 0:
            bins = np.logspace(np.log10(max(0.5, df0_v.min())), np.log10(max(df0_v.max(), 1)), 60)
            ax.hist(df0_v, bins=bins, alpha=0.6, color="steelblue")
            ax.axvline(mod["micro_thresh_hz_s"], color="green", linestyle=":", linewidth=0.8, label=f"micro ≤{mod['micro_thresh_hz_s']}")
            ax.axvline(mod["macro_thresh_hz_s"], color="red", linestyle=":", linewidth=0.8, label=f"macro ≥{mod['macro_thresh_hz_s']}")
            ax.legend(fontsize=7)
        ax.set_xscale("log")
    ax.set_xlabel("|ΔF0| (Hz/s)")
    ax.set_ylabel("Count")
    ax.set_title("|ΔF0| Distribution (log bins)")

    plt.tight_layout()
    out_png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_png, dpi=150, bbox_inches="tight")
    plt.close(fig)
    log.info("Plot saved: %s", out_png)


# ---------------------------------------------------------------------------
# Main analysis
# ---------------------------------------------------------------------------

def analyze_wav(wav_path: Path, f0_min: float = 70.0, f0_max: float = 400.0,
                viz_dir: Optional[Path] = None) -> dict:
    """Run full F0 modulation analysis on a WAV file.

    Returns a dict with all stats (also printed as JSON when run standalone).
    """
    # Load audio
    with wave.open(str(wav_path), "rb") as wf:
        sr = wf.getframerate()
        n_frames = wf.getnframes()
        raw = wf.readframes(n_frames)
        dtype = np.int16 if wf.getsampwidth() == 2 else np.float32
        y = np.frombuffer(raw, dtype=dtype).astype(np.float32)
        nch = wf.getnchannels()
        if nch > 1:
            y = y.reshape(-1, nch)
            # mono via loudest channel
            mags = np.abs(y).max(axis=0)
            best = int(np.argmax(mags))
            y = y[:, best]
        y = y.ravel()

    # Normalize to peak 0.95 (same as synth_pure)
    peak = float(np.abs(y).max()) if y.size else 0.0
    if peak > 1e-12:
        y = y / peak * 0.95

    label = wav_path.stem

    # Reuse synth_pure pipeline
    prepared = prepare_analysis(y, float(sr), f0_min=f0_min, f0_max=f0_max)
    f0 = np.asarray(prepared["f0"])
    voiced = np.asarray(prepared["voiced"])
    times = np.asarray(prepared["times"])

    # Frame duration for ΔF0 in Hz/s
    if len(times) >= 2:
        frame_dur = float(times[1] - times[0])
    else:
        frame_dur = 0.0464  # default hop

    # Cluster analysis
    clusters = f0_clusters(f0, voiced)

    # Modulation dynamics
    mod = modulation_stats(f0, voiced, frame_dur)

    # Summary stats
    f0_v = f0[voiced]
    stats = {
        "label": label,
        "path": str(wav_path),
        "duration_s": float(prepared["duration"]),
        "sr": int(sr),
        "n_frames": len(f0),
        "voiced_frac": float(voiced.mean()),
        "f0_mean_hz": float(np.mean(f0_v)) if len(f0_v) else 0.0,
        "f0_min_hz": float(np.min(f0_v)) if len(f0_v) else 0.0,
        "f0_max_hz": float(np.max(f0_v)) if len(f0_v) else 0.0,
        "f0_std_hz": float(np.std(f0_v)) if len(f0_v) else 0.0,
        "f0_range_hz": float(np.max(f0_v) - np.min(f0_v)) if len(f0_v) else 0.0,
        "f0_q25_hz": float(np.percentile(f0_v, 25)) if len(f0_v) else 0.0,
        "f0_q50_hz": float(np.percentile(f0_v, 50)) if len(f0_v) else 0.0,
        "f0_q75_hz": float(np.percentile(f0_v, 75)) if len(f0_v) else 0.0,
        "f0_iqr_hz": float(np.percentile(f0_v, 75) - np.percentile(f0_v, 25)) if len(f0_v) else 0.0,
        "clusters": {
            "n_peaks": len(clusters["peaks_hz"]),
            "peaks_hz": clusters["peaks_hz"],
            "bandwidths_hz": clusters["bandwidths"],
            "populations": clusters["populations"],
            "peak_separations_hz": _peak_separations(clusters["peaks_hz"]),
        },
        "modulation": {
            "df0_mean_hz_s": mod["df0_hz_per_s_mean"],
            "df0_std_hz_s": mod["df0_hz_per_s_std"],
            "df0_max_hz_s": mod["df0_hz_per_s_max"],
            "micro_frac": mod["micro_frac"],
            "mid_frac": mod["mid_frac"],
            "macro_frac": mod["macro_frac"],
            "micro_thresh_hz_s": mod["micro_thresh_hz_s"],
            "macro_thresh_hz_s": mod["macro_thresh_hz_s"],
        },
    }

    # Plot
    if viz_dir and _HAVE_MPL:
        png_path = viz_dir / f"{label}_f0_analysis.png"
        plot_analysis(label, times, f0, voiced, clusters, mod, png_path)
        stats["viz_png"] = str(png_path)

    return stats


def _peak_separations(peaks: list[float]) -> list[float]:
    """Sorted peak-to-peak separations (useful for checking ~40Hz hypothesis)."""
    if len(peaks) < 2:
        return []
    sorted_peaks = sorted(peaks)
    return [float(sorted_peaks[i + 1] - sorted_peaks[i]) for i in range(len(sorted_peaks) - 1)]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Analyze F0 modulation dynamics from voice WAVs")
    ap.add_argument("paths", nargs="+", help="WAV file(s) or directory of WAVs")
    ap.add_argument("--f0-min", type=float, default=70.0, help="Minimum F0 in Hz (default: 70)")
    ap.add_argument("--f0-max", type=float, default=400.0, help="Maximum F0 in Hz (default: 400)")
    ap.add_argument("--viz-dir", type=str, default=None,
                    help="Directory for PNG output (default: ~/Music/voice-analysis/viz)")
    ap.add_argument("--json-out", type=str, default=None, help="Save JSON summary to this file")
    ap.add_argument("--no-plot", action="store_true", help="Skip PNG generation")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")

    # Resolve paths
    wav_files = []
    for p in args.paths:
        pp = Path(p).expanduser().resolve()
        if pp.is_dir():
            wav_files.extend(sorted(pp.glob("*.wav")))
        elif pp.is_file() and pp.suffix.lower() == ".wav":
            wav_files.append(pp)
        else:
            log.warning("Skipping non-WAV: %s", pp)

    if not wav_files:
        log.error("No WAV files found")
        sys.exit(1)

    # Skip derivative files (same suffix list as build_voice_compare_v3)
    SKIP = ("_synth", "_orig", "_mono", "_clean", "_filt", "_0-9s", "_side_by_side", "_f0_analysis")
    wav_files = [f for f in wav_files if not any(s in f.stem for s in SKIP)]
    log.info("Analyzing %d WAVs", len(wav_files))

    viz_dir = None
    if not args.no_plot:
        viz_dir = Path(args.viz_dir).expanduser().resolve() if args.viz_dir else \
                  Path.home() / "Music" / "voice-analysis" / "viz"

    all_stats = []
    for wf in wav_files:
        log.info("Analyzing: %s", wf.name)
        try:
            st = analyze_wav(wf, f0_min=args.f0_min, f0_max=args.f0_max, viz_dir=viz_dir)
            all_stats.append(st)
        except Exception as exc:
            log.error("Failed on %s: %s", wf.name, exc, exc_info=True)

    # Print summary to stdout
    if all_stats:
        print(json.dumps(all_stats, indent=2, default=str))

    if args.json_out:
        out_p = Path(args.json_out).expanduser().resolve()
        out_p.parent.mkdir(parents=True, exist_ok=True)
        out_p.write_text(json.dumps(all_stats, indent=2, default=str))
        log.info("JSON saved: %s", out_p)


if __name__ == "__main__":
    main()
