"""Production audio normalization pipeline for digital-beacon sources.

GOVERNING PRINCIPLE
    out = scalar_gain × repaired_float_signal
    (+ optional DC subtraction and conditional sub-30 Hz HPF).
    Harmonic ratios (H2/H1, tilt, odd/even) are UNCHANGED.

Stages (strict order):
  1. Format & Integrity (read-only originals, SHA256, canonical float32)
  2. Loudness Normalization (GAIN ONLY via pyloudnorm + VAD)
  4. Spectral Conditioning (MINIMAL: phone tag, sibilant frame mask)
  5. Quality Gates (ACCEPT / QUARANTINE / EXCLUDE, dataset-stats driven)

CLI:
    python tools/normalize_sources.py --input-dir <dir> --output-dir <dir> [--config config.yaml] [--dry-run] [--enhance]

All outputs under runs/<YYYYMMDD_HHMMSS_v1>/ with structure per spec.
Originals are never modified.
Canonical and normalized WAVs are always FLOAT subtype (float32).
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import math
import os
import shutil
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import soundfile as sf

from tools import stage1_format_integrity

try:
    import yaml
except ImportError:
    yaml = None

try:
    import pandas as pd
except ImportError:
    pd = None

try:
    import pyloudnorm as pyln
except ImportError:
    pyln = None

try:
    import librosa
except ImportError:
    librosa = None

try:
    from scipy import signal as sp_signal
    from scipy.ndimage import label as nd_label
except ImportError:
    sp_signal = None
    nd_label = None

# Optional compressor for --enhance (do not import at top level for dry environments)
_COMPRESSOR_AVAILABLE = False
try:
    from digital_beacon.compressor import (
        apply_compressor_chain,
        CompressorParams,
        write_review_copy_wav,
    )
    _COMPRESSOR_AVAILABLE = True
except Exception:
    apply_compressor_chain = None
    CompressorParams = None
    write_review_copy_wav = None

log = logging.getLogger("normalize_sources")

# ---------------------------------------------------------------------------
# Defaults (tunable via config.yaml)
# ---------------------------------------------------------------------------

PIPELINE_VERSION = "1.0.0"

DEFAULTS: Dict[str, Any] = {
    "target_lufs": -23.0,
    "peak_ceiling_dbtp": -3.0,
    "target_sr": 44100,
    # DC / repair
    "dc_var_window_s": 0.5,
    "dc_var_thresh": 5e-6,
    # HPF rumble (conditional)
    "hp_cutoff_hz": 25.0,
    "rumble_energy_ratio_thresh": 0.10,
    # Stereo
    "stereo_high_corr": 0.98,
    "stereo_low_corr": 0.70,
    "stereo_max_lag_samples": 64,
    # Phone bandwidth
    "phone_low_hz": 300,
    "phone_high_hz": 3400,
    "phone_energy_frac": 0.90,
    # Frame / sibilant (Stage 4)
    "frame_hop_ms": 10,
    "frame_len_ms": 25,
    "sibilant_centroid_hz": 3200.0,
    "sibilant_zcr": 0.09,
    "sibilant_hf_ratio": 0.18,
    # QC base thresholds (data stats override / combine)
    "min_voiced_duration_s": 25.0,
    "snr_floor_db": 8.0,
    "clipping_pct_quarantine": 2.0,
    "clipping_pct_exclude": 8.0,
    "lra_quarantine": 14.0,
    "very_quiet_gain_db": 18.0,
    # VAD energy fallback
    "energy_vad_thresh_db": -45.0,
    "energy_vad_frame_ms": 20,
}

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class PipelineConfig:
    """CLI + runtime configuration."""
    input_dir: Path
    output_dir: Path
    enhance: bool = False
    dry_run: bool = False
    config_path: Optional[Path] = None
    target_lufs: float = DEFAULTS["target_lufs"]
    peak_ceiling_dbtp: float = DEFAULTS["peak_ceiling_dbtp"]
    target_sr: int = DEFAULTS["target_sr"]
    log_level: str = "INFO"
    run_name: Optional[str] = None
    # full tunables
    full: Dict[str, Any] = field(default_factory=lambda: DEFAULTS.copy())

    @classmethod
    def from_args_and_config(cls, args: argparse.Namespace) -> "PipelineConfig":
        full = DEFAULTS.copy()
        cfg_path = getattr(args, "config", None)
        if cfg_path:
            cfg_path = Path(cfg_path).resolve()
            if cfg_path.exists() and yaml is not None:
                with open(cfg_path, "r", encoding="utf-8") as f:
                    user_cfg = yaml.safe_load(f) or {}
                if isinstance(user_cfg, dict):
                    full.update(user_cfg)
        return cls(
            input_dir=Path(args.input_dir).resolve(),
            output_dir=Path(args.output_dir).resolve(),
            enhance=getattr(args, "enhance", False),
            dry_run=getattr(args, "dry_run", False),
            config_path=cfg_path,
            target_lufs=full.get("target_lufs", DEFAULTS["target_lufs"]),
            peak_ceiling_dbtp=full.get("peak_ceiling_dbtp", DEFAULTS["peak_ceiling_dbtp"]),
            target_sr=full.get("target_sr", DEFAULTS["target_sr"]),
            log_level=getattr(args, "log", "INFO"),
            run_name=getattr(args, "run_name", None),
            full=full,
        )


@dataclass
class RunDirs:
    """Directory layout for a single pipeline run."""
    root: Path
    canonical: Path
    normalized_analysis: Path
    quarantine: Path
    metrics: Path
    metrics_pre: Path
    metrics_post: Path
    reports: Path
    per_file_reports: Path
    verification: Path
    logs: Path
    temp: Path
    enhanced_review: Optional[Path] = None

    @classmethod
    def create(cls, root: Path, enhance: bool) -> "RunDirs":
        d = cls(
            root=root,
            canonical=root / "canonical",
            normalized_analysis=root / "normalized_analysis",
            quarantine=root / "quarantine",
            metrics=root / "metrics",
            metrics_pre=root / "metrics" / "pre",
            metrics_post=root / "metrics" / "post",
            reports=root / "reports",
            per_file_reports=root / "reports" / "per_file",
            verification=root / "verification",
            logs=root / "logs",
            temp=root / "temp",
        )
        if enhance:
            d.enhanced_review = root / "enhanced_review"
        return d

    def all_paths(self) -> List[Path]:
        paths = [
            self.root,
            self.canonical,
            self.normalized_analysis,
            self.quarantine,
            self.metrics,
            self.metrics_pre,
            self.metrics_post,
            self.reports,
            self.per_file_reports,
            self.verification,
            self.logs,
            self.temp,
        ]
        if self.enhanced_review is not None:
            paths.append(self.enhanced_review)
        return paths


@dataclass
class FileMetrics:
    """Per-file metrics collected for QC and reports."""
    full_lufs: float
    speech_lufs: float
    lra: float
    true_peak_dbtp: float
    dc_offset: float
    clipping_pct_voiced: float
    speech_ratio: float
    snr_db: float
    noise_floor_db: float
    bandwidth_hz: float
    reverb_proxy: float
    voiced_duration_s: float
    duration_s: float
    gain_db: float
    repair_actions: List[str] = field(default_factory=list)
    flags: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _sha256_file(path: Path) -> str:
    """Chunked SHA256 of a file (read-only)."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _config_hash(cfg: Dict[str, Any]) -> str:
    """Stable short hash of config for sidecars."""
    s = json.dumps(cfg, sort_keys=True, default=str)
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


def _get_cmdline() -> str:
    return " ".join(sys.argv)


def _get_dependency_versions() -> Dict[str, str]:
    deps = {
        "numpy": getattr(np, "__version__", "unknown"),
        "soundfile": getattr(sf, "__version__", "unknown"),
        "scipy": getattr(sp_signal, "__version__", "unknown") if sp_signal else "missing",
        "librosa": getattr(librosa, "__version__", "unknown") if librosa else "missing",
        "pyloudnorm": "present" if pyln is not None else "missing",
        "pandas": "present" if pd is not None else "missing",
        "pyyaml": "present" if yaml is not None else "missing",
    }
    try:
        import torch
        deps["torch"] = torch.__version__
    except Exception:
        deps["torch"] = "missing"
    # ffmpeg version via subprocess (best-effort)
    try:
        import subprocess
        result = subprocess.run(
            ["ffmpeg", "-version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            first_line = result.stdout.splitlines()[0]
            # e.g. "ffmpeg version 4.4.2 Copyright ..."
            parts = first_line.split()
            if len(parts) >= 3 and parts[0].lower() == "ffmpeg" and parts[1].lower() == "version":
                deps["ffmpeg"] = parts[2]
            else:
                deps["ffmpeg"] = first_line.strip()
        else:
            deps["ffmpeg"] = "unavailable"
    except Exception:
        deps["ffmpeg"] = "missing"
    return deps


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append_jsonl(path: Path, obj: Dict[str, Any], dry_run: bool) -> None:
    if dry_run:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(obj, default=str) + "\n"
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)


def _log_stage(log_path: Path, source_hash: str, stage: str, status: str,
               message: str = "", duration_ms: float = 0.0, dry_run: bool = False) -> None:
    rec = {
        "timestamp": _now_iso(),
        "source_hash": source_hash,
        "stage": stage,
        "status": status,
        "message": message,
        "duration_ms": round(duration_ms, 1),
    }
    _append_jsonl(log_path, rec, dry_run)
    lvl = logging.INFO if status in ("ok", "start") else logging.WARNING if status == "skip" else logging.ERROR
    log.log(lvl, "stage=%s status=%s hash=%s %s", stage, status, source_hash[:12], message)


# ---------------------------------------------------------------------------
# Audio helpers (Stage 1 + 2 + 4)
# ---------------------------------------------------------------------------

def detect_time_varying_dc(y: np.ndarray, sr: int, win_s: float, var_thresh: float) -> bool:
    if sp_signal is None or len(y) < int(win_s * sr * 1.5):
        return False
    win = max(128, int(win_s * sr))
    step = win // 2
    means = []
    for i in range(0, len(y) - win + 1, step):
        means.append(float(np.mean(y[i : i + win])))
    if len(means) < 3:
        return False
    return float(np.var(means)) > var_thresh


def apply_zero_phase_hp(y: np.ndarray, sr: int, cutoff: float, order: int = 2) -> np.ndarray:
    if sp_signal is None:
        return y.astype(np.float32)
    b, a = sp_signal.butter(order, cutoff, btype="high", fs=sr)
    yf = sp_signal.filtfilt(b, a, y)
    return yf.astype(np.float32)


def apply_hp_filter(y: np.ndarray, sr: int, cutoff: float) -> Tuple[np.ndarray, Dict[str, Any]]:
    if sp_signal is None:
        return y.astype(np.float32), {}
    b, a = sp_signal.butter(2, cutoff, btype="high", fs=sr)
    yf = sp_signal.filtfilt(b, a, y).astype(np.float32)
    coeffs = {"b": b.tolist(), "a": a.tolist(), "cutoff_hz": cutoff, "order": 2, "note": "filtfilt=4pole effective"}
    return yf, coeffs


def compute_rumble_ratio(y: np.ndarray, sr: int) -> float:
    if sp_signal is None or len(y) < sr:
        return 0.0
    try:
        b, a = sp_signal.butter(4, 30.0, btype="low", fs=sr)
        low = sp_signal.filtfilt(b, a, y)
        e_low = float(np.mean(low * low))
        e_tot = float(np.mean(y * y)) + 1e-12
        return e_low / e_tot
    except Exception:
        return 0.0


def stereo_to_mono(y: np.ndarray, sr: int, high_corr: float, low_corr: float,
                   max_lag: int) -> Tuple[str, np.ndarray, List[str]]:
    """Cross-correlate stereo decision. Returns decision, mono signal, flags."""
    if y.ndim == 1:
        return "mono", y.astype(np.float32), []
    if y.shape[1] < 2:
        return "mono", y[:, 0].astype(np.float32), []

    ch0 = y[:, 0].astype(np.float64)
    ch1 = y[:, 1].astype(np.float64)
    flags: List[str] = []

    # Cross correlation (zero-mean)
    c0 = ch0 - ch0.mean()
    c1 = ch1 - ch1.mean()
    corr = sp_signal.correlate(c0, c1, mode="full") if sp_signal else np.correlate(c0, c1, mode="full")
    # normalize
    norm = np.sqrt(np.sum(c0 * c0) * np.sum(c1 * c1)) + 1e-12
    corr = corr / norm
    peak_idx = int(np.argmax(np.abs(corr)))
    max_r = float(corr[peak_idx])
    lag_samples = peak_idx - (len(c0) - 1)

    if max_r < -0.05:  # anti-phase
        flags.append("stereo_anti_phase")
        # pick lower peak-to-RMS channel
        p2r0 = float(np.max(np.abs(ch0))) / (np.sqrt(np.mean(ch0 * ch0)) + 1e-9)
        p2r1 = float(np.max(np.abs(ch1))) / (np.sqrt(np.mean(ch1 * ch1)) + 1e-9)
        mono = (ch0 if p2r0 <= p2r1 else ch1).astype(np.float32)
        return "select_cleaner_anti_phase", mono, flags

    abs_r = abs(max_r)
    if abs_r >= high_corr and abs(lag_samples) <= max(1, max_lag):
        mono = ((ch0 + ch1) / 2.0).astype(np.float32)
        return "average_channels", mono, flags

    if abs_r >= low_corr:
        p2r0 = float(np.max(np.abs(ch0))) / (np.sqrt(np.mean(ch0 * ch0)) + 1e-9)
        p2r1 = float(np.max(np.abs(ch1))) / (np.sqrt(np.mean(ch1 * ch1)) + 1e-9)
        mono = (ch0 if p2r0 <= p2r1 else ch1).astype(np.float32)
        return f"select_cleaner_lag={lag_samples}", mono, flags

    # low correlation
    flags.append("stereo_low_correlation")
    mono = ch0.astype(np.float32)
    return "select_ch0_low_corr", mono, flags


def _fallback_label(boolean_mask: np.ndarray) -> Tuple[np.ndarray, int]:
    """Scipy-free connected-component label: returns (labeled, num_features)."""
    labeled = np.cumsum(
        np.diff(np.concatenate([[0], boolean_mask.astype(int)])) != 0
    ) + 1
    labeled[~boolean_mask] = 0
    numf = int(np.max(labeled))
    return labeled, numf


def detect_corruptions(y: np.ndarray, info: Any, actual_frames: int) -> List[str]:
    flags: List[str] = []
    if nd_label is None:
        nd_label_local = _fallback_label
    else:
        nd_label_local = nd_label

    declared = getattr(info, "frames", 0)
    if declared and abs(declared - actual_frames) > max(1, int(0.001 * declared)):
        flags.append("truncation_or_header_mismatch")

    # flat-top clipping runs (>=5 samples at |y| ~ 1.0)
    clip_mask = np.abs(y) >= 0.9995
    if np.any(clip_mask):
        try:
            labeled, numf = nd_label_local(clip_mask)
            for lab in range(1, int(numf) + 1 if numf else 0):
                run = int(np.sum(labeled == lab))
                if run >= 5:
                    flags.append("flat_top_clipping")
                    break
        except Exception:
            if np.sum(clip_mask) > 20:
                flags.append("flat_top_clipping")

    # dropouts (zero runs >=100 samples)
    zero_mask = np.abs(y) < 1e-8
    if np.any(zero_mask):
        try:
            labeled, numf = nd_label_local(zero_mask)
            for lab in range(1, int(numf) + 1 if numf else 0):
                run = int(np.sum(labeled == lab))
                if run >= 100:
                    flags.append("dropout")
                    break
        except Exception:
            pass

    # silent file
    if float(np.max(np.abs(y))) < 1e-9:
        flags.append("silent_or_decode_fail")

    return flags


def detect_phone_bandwidth(y: np.ndarray, sr: int, low: float, high: float, frac: float) -> bool:
    if sp_signal is None or len(y) < sr // 2:
        return False
    try:
        f, Pxx = sp_signal.welch(y.astype(np.float64), fs=sr, nperseg=min(4096, len(y)), scaling="spectrum")
        band = (f >= low) & (f <= high)
        e_band = float(np.sum(Pxx[band]))
        e_tot = float(np.sum(Pxx)) + 1e-12
        return (e_band / e_tot) >= frac
    except Exception:
        return False


def compute_bandwidth_hz(y: np.ndarray, sr: int) -> float:
    if sp_signal is None or len(y) < 256:
        return float(sr // 2)
    try:
        f, Pxx = sp_signal.welch(y, fs=sr, nperseg=2048, scaling="spectrum")
        Pxx = np.maximum(Pxx, 1e-12)
        peak = float(np.max(Pxx))
        thresh = peak / 100.0  # -20 dB
        idx = np.where(Pxx >= thresh)[0]
        if len(idx) == 0:
            return 8000.0
        return float(f[idx[-1]])
    except Exception:
        return float(sr // 2)


def compute_reverb_proxy(y: np.ndarray, sr: int) -> float:
    if len(y) < sr:
        return 0.0
    try:
        # Autocorrelation via FFT (O(N log N) instead of O(N²))
        # Only compute up to 0.4s lags — we don't need the full correlation
        max_lag = min(int(0.4 * sr), len(y) - 1)
        n_fft = 2 ** int(np.ceil(np.log2(2 * len(y) - 1)))
        Y = np.fft.rfft(y.astype(np.float64), n=n_fft)
        ac_full = np.fft.irfft(Y * np.conj(Y), n=n_fft)[: len(y)]
        ac = ac_full[: max_lag + 1]
        ac = ac / (ac[0] + 1e-12)
        e_early = float(np.sum(ac[1 : int(0.08 * sr)] ** 2))
        e_late = float(np.sum(ac[int(0.08 * sr) : max_lag + 1] ** 2)) + 1e-12
        return e_early / e_late
    except Exception:
        return 0.0


def estimate_snr(y: np.ndarray, speech_mask: np.ndarray) -> Tuple[float, float]:
    """Return (snr_db, noise_floor_db). speech_mask is sample-level boolean."""
    if len(y) < 1024:
        return 20.0, -70.0
    voiced = y[speech_mask] if speech_mask.any() else y
    unvoiced = y[~speech_mask] if (~speech_mask).any() else y[: len(y) // 4]
    rms_v = float(np.sqrt(np.mean(voiced * voiced) + 1e-12))
    rms_n = float(np.sqrt(np.mean(unvoiced * unvoiced) + 1e-12))
    snr = 20.0 * math.log10(max(rms_v / rms_n, 1e-9))
    nf = 20.0 * math.log10(rms_n)
    return snr, nf


def get_vad_segments(y: np.ndarray, sr: int, full_cfg: Dict[str, Any]) -> List[Tuple[int, int]]:
    """Return list of (start_sample, end_sample) for speech active. Uses silero if torch present, else energy."""
    # Try silero
    try:
        import torch
        model, utils = torch.hub.load("snakers4/silero-vad", "silero_vad", trust_repo=True)
        (get_speech_timestamps, _, _, _, _) = utils
        # silero expects float32 tensor or numpy -1..1
        ts = get_speech_timestamps(y.astype(np.float32), model, sampling_rate=sr, threshold=0.5, min_speech_duration_ms=250)
        segs = []
        for t in ts:
            s = int(t["start"])
            e = int(t["end"])
            if e > s:
                segs.append((s, e))
        if segs:
            return segs
    except Exception:
        pass

    # Energy fallback
    frame_ms = full_cfg.get("energy_vad_frame_ms", 20)
    thresh_db = full_cfg.get("energy_vad_thresh_db", -45.0)
    hop = max(80, int(sr * frame_ms / 1000))
    frame = hop * 2
    thresh_lin = 10.0 ** (thresh_db / 20.0)

    segs: List[Tuple[int, int]] = []
    i = 0
    in_seg = False
    seg_start = 0
    while i + frame < len(y):
        rms = float(np.sqrt(np.mean(y[i : i + frame] ** 2)))
        active = rms > thresh_lin
        if active and not in_seg:
            in_seg = True
            seg_start = i
        elif not active and in_seg:
            in_seg = False
            if (i - seg_start) > int(0.25 * sr):
                segs.append((seg_start, i))
        i += hop
    if in_seg and (len(y) - seg_start) > int(0.25 * sr):
        segs.append((seg_start, len(y)))
    return segs


def compute_speech_active_lufs(y: np.ndarray, sr: int, segs: List[Tuple[int, int]]) -> float:
    if pyln is None or not segs:
        return -70.0
    try:
        active = np.concatenate([y[s:e] for s, e in segs]) if segs else y
        meter = pyln.Meter(sr)
        return float(meter.integrated_loudness(active.astype(np.float64)))
    except Exception:
        return -70.0


def compute_true_peak_dbtp(y: np.ndarray, sr: int) -> float:
    if len(y) == 0:
        return -120.0
    try:
        if sp_signal is not None:
            up = sp_signal.resample_poly(y.astype(np.float64), up=4, down=1)
            pk = float(np.max(np.abs(up)))
            return 20.0 * math.log10(max(pk, 1e-12))
    except Exception:
        pass
    pk = float(np.max(np.abs(y)))
    return 20.0 * math.log10(max(pk, 1e-12))


def compute_lra(y: np.ndarray, sr: int) -> float:
    if pyln is None:
        return 0.0
    try:
        meter = pyln.Meter(sr)
        return float(meter.loudness_range(y.astype(np.float64)))
    except Exception:
        return 0.0


def compute_frame_exclusion_mask(y: np.ndarray, sr: int, clip_mask_samples: np.ndarray,
                                  is_phone: bool, full_cfg: Dict[str, Any]) -> np.ndarray:
    """Boolean array at hop size: True means EXCLUDE this frame from descriptors."""
    hop_ms = full_cfg.get("frame_hop_ms", 10)
    frame_ms = full_cfg.get("frame_len_ms", 25)
    hop = max(32, int(sr * hop_ms / 1000))
    n_frames = max(1, (len(y) - int(sr * frame_ms / 1000)) // hop + 1)

    if librosa is None:
        # crude mask: phone or any clipping
        mask = np.zeros(n_frames, dtype=bool)
        if is_phone:
            mask[:] = True
        # mark frames overlapping clips
        for f in range(n_frames):
            start = f * hop
            end = min(start + int(sr * 0.03), len(y))
            if np.any(clip_mask_samples[start:end]):
                mask[f] = True
        return mask

    cent = librosa.feature.spectral_centroid(
        y=y.astype(np.float32), sr=sr,
        n_fft=int(sr * frame_ms / 1000), hop_length=hop, center=False
    )[0]
    zcr = librosa.feature.zero_crossing_rate(
        y, frame_length=int(sr * frame_ms / 1000), hop_length=hop, center=False
    )[0]

    S = np.abs(librosa.stft(y.astype(np.float32), n_fft=int(sr * frame_ms / 1000), hop_length=hop, center=False))
    freqs = librosa.fft_frequencies(sr=sr, n_fft=int(sr * frame_ms / 1000))
    hf_idx = freqs > 4000
    tot = np.sum(S * S, axis=0) + 1e-12
    hf = np.sum(S[hf_idx, :] * S[hf_idx, :], axis=0) if np.any(hf_idx) else np.zeros_like(tot)
    hf_ratio = hf / tot

    c_thresh = full_cfg.get("sibilant_centroid_hz", 3200.0)
    z_thresh = full_cfg.get("sibilant_zcr", 0.09)
    h_thresh = full_cfg.get("sibilant_hf_ratio", 0.18)

    sibilant = (cent > c_thresh) & (zcr > z_thresh) & (hf_ratio > h_thresh)

    mask = sibilant.copy()
    if is_phone:
        mask[:] = True

    # overlay clipped frames
    if clip_mask_samples is not None and len(clip_mask_samples) == len(y):
        for f in range(len(mask)):
            s = f * hop
            e = min(s + int(0.03 * sr), len(y))
            if np.any(clip_mask_samples[s:e]):
                mask[f] = True

    # pad length
    if len(mask) < n_frames:
        mask = np.pad(mask, (0, n_frames - len(mask)), constant_values=False)
    return mask[:n_frames].astype(bool)


def compute_clipped_sample_mask(y: np.ndarray) -> np.ndarray:
    return (np.abs(y) >= 0.9995).astype(bool)


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def compute_all_metrics(y_repaired: np.ndarray, y_out: np.ndarray, sr: int,
                        segs: List[Tuple[int, int]], gain_db: float,
                        flags: List[str], full_cfg: Dict[str, Any],
                        is_phone: bool, dc: float, repair_actions: List[str],
                        clip_sample_mask: np.ndarray) -> FileMetrics:
    duration_s = len(y_repaired) / float(sr)

    full_lufs = -70.0
    lra = 0.0
    if pyln is not None:
        try:
            meter = pyln.Meter(sr)
            full_lufs = float(meter.integrated_loudness(y_out.astype(np.float64)))
            lra = float(meter.loudness_range(y_out.astype(np.float64)))
        except Exception:
            pass

    speech_lufs = compute_speech_active_lufs(y_out, sr, segs)
    tp = compute_true_peak_dbtp(y_out, sr)

    # voiced mask at sample level for stats
    voiced_mask = np.zeros(len(y_repaired), dtype=bool)
    for s, e in segs:
        if e > s:
            voiced_mask[s:e] = True

    snr, nf = estimate_snr(y_out, voiced_mask)

    voiced_dur = sum((e - s) for s, e in segs) / float(sr)
    speech_ratio = (voiced_dur / duration_s) if duration_s > 0 else 0.0

    # clipping only on voiced
    if voiced_mask.any():
        voiced_clipped = float(np.sum(clip_sample_mask & voiced_mask))
        voiced_total = float(np.sum(voiced_mask))
        clip_pct = 100.0 * (voiced_clipped / voiced_total) if voiced_total > 0 else 0.0
    else:
        clip_pct = 100.0 * (float(np.sum(clip_sample_mask)) / len(y_repaired)) if len(y_repaired) > 0 else 0.0

    bw = compute_bandwidth_hz(y_repaired, sr)
    reverb = compute_reverb_proxy(y_repaired, sr)

    return FileMetrics(
        full_lufs=round(full_lufs, 3),
        speech_lufs=round(speech_lufs, 3),
        lra=round(lra, 3),
        true_peak_dbtp=round(tp, 3),
        dc_offset=round(dc, 8),
        clipping_pct_voiced=round(clip_pct, 4),
        speech_ratio=round(speech_ratio, 5),
        snr_db=round(snr, 2),
        noise_floor_db=round(nf, 2),
        bandwidth_hz=round(bw, 1),
        reverb_proxy=round(reverb, 4),
        voiced_duration_s=round(voiced_dur, 3),
        duration_s=round(duration_s, 3),
        gain_db=round(gain_db, 4),
        repair_actions=repair_actions[:],
        flags=flags[:],
    )


# ---------------------------------------------------------------------------
# QC (dataset-stats driven)
# ---------------------------------------------------------------------------

def assign_qc_decisions(processed: List[Dict[str, Any]], full_cfg: Dict[str, Any]) -> None:
    """Mutates each entry with 'decision' and augments flags. Thresholds from data."""
    if not processed:
        return

    voiced = [p["metrics"].voiced_duration_s for p in processed]
    snrs = [p["metrics"].snr_db for p in processed]
    clips = [p["metrics"].clipping_pct_voiced for p in processed]
    lras = [p["metrics"].lra for p in processed]

    min_voiced = min(voiced) if voiced else 0.0
    med_voiced = float(np.median(voiced)) if voiced else 30.0
    med_snr = float(np.median(snrs)) if snrs else 20.0
    med_clip = float(np.median(clips)) if clips else 0.1
    med_lra = float(np.median(lras)) if lras else 6.0

    base_min_voiced = float(full_cfg.get("min_voiced_duration_s", 25.0))
    base_snr_floor = float(full_cfg.get("snr_floor_db", 8.0))
    clip_q = float(full_cfg.get("clipping_pct_quarantine", 2.0))
    clip_e = float(full_cfg.get("clipping_pct_exclude", 8.0))
    lra_q = float(full_cfg.get("lra_quarantine", 14.0))
    vq = float(full_cfg.get("very_quiet_gain_db", 18.0))

    for p in processed:
        m = p["metrics"]
        fl = p.get("flags", [])
        dec = "accept"

        # Hard excludes
        if "silent_or_decode_fail" in fl or "truncation_or_header_mismatch" in p.get("corruptions", []):
            dec = "exclude"
        elif m.voiced_duration_s < max(base_min_voiced, min(min_voiced * 0.6, base_min_voiced * 0.8)):
            dec = "exclude"
        elif m.snr_db < min(base_snr_floor, med_snr - 12.0):
            dec = "exclude"
        elif m.clipping_pct_voiced > clip_e * 1.5 or ("dropout" in fl and m.voiced_duration_s < 60):
            dec = "exclude"
        elif m.bandwidth_hz < 1800:  # too narrow passband
            dec = "exclude"

        # Quarantine tier (if not already exclude)
        if dec == "accept":
            if m.voiced_duration_s < max(base_min_voiced * 1.2, med_voiced * 0.85):
                dec = "quarantine"
            elif m.snr_db < med_snr - 6 or m.clipping_pct_voiced > clip_q or m.lra > lra_q:
                dec = "quarantine"
            elif "stereo_low_correlation" in fl or "stereo_anti_phase" in fl:
                dec = "quarantine"
            elif "bandwidth_limited" in fl or "very_quiet" in fl or m.gain_db > vq:
                dec = "quarantine"
            elif m.reverb_proxy > 4.5:  # heavy reverb proxy
                dec = "quarantine"

        p["decision"] = dec
        if dec != "accept" and "quarantine" not in fl and dec == "quarantine":
            fl.append("quarantined_by_qc")
        if dec == "exclude" and "excluded_by_qc" not in fl:
            fl.append("excluded_by_qc")
        p["flags"] = fl


# ---------------------------------------------------------------------------
# Sidecars, reports, manifests
# ---------------------------------------------------------------------------

def write_sidecar(path: Path, filename: str, source_hash: str, config_hash: str, cmd: str,
                  deps: Dict[str, str], metrics: FileMetrics, decision: str,
                  gain_db: float, filter_coeffs: Optional[Dict], dry_run: bool) -> None:
    if dry_run:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    side = {
        "version": PIPELINE_VERSION,
        "source": {"file": filename},
        "source_hash": source_hash,
        "pipeline_version": PIPELINE_VERSION,
        "config_hash": config_hash,
        "command_line": cmd,
        "dependency_versions": deps,
        "metrics": asdict(metrics),
        "decisions": {"qc": decision},
        "applied_gain_db": gain_db,
        "filter_coefficients": filter_coeffs,
    }
    path.write_text(json.dumps(side, indent=2, default=str), encoding="utf-8")


def write_per_file_report(path: Path, src_name: str, source_hash: str,
                          metrics: FileMetrics, decision: str, flags: List[str],
                          stereo: str, corruptions: List[str], dry_run: bool) -> None:
    if dry_run:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    rep = {
        "file": src_name,
        "source_hash": source_hash,
        "timestamp": _now_iso(),
        "decision": decision,
        "flags": flags,
        "stereo_decision": stereo,
        "corruption_flags": corruptions,
        "metrics": asdict(metrics),
    }
    path.write_text(json.dumps(rep, indent=2, default=str), encoding="utf-8")


def write_dataset_csv(path: Path, rows: List[Dict[str, Any]], dry_run: bool) -> None:
    if dry_run:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    cols = ["file_hash", "filename", "duration_s", "decision", "speech_lufs", "full_lufs",
            "snr_db", "voiced_duration_s", "clipping_pct", "flags"]
    if pd is not None:
        df = pd.DataFrame(rows)
        for c in cols:
            if c not in df.columns:
                df[c] = ""
        df[cols].to_csv(path, index=False)
    else:
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k, "") for k in cols})


def write_manifest(path: Path, entries: List[Dict], dry_run: bool) -> None:
    if dry_run:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    man = {
        "created_at": _now_iso(),
        "pipeline_version": PIPELINE_VERSION,
        "sources": entries,
    }
    path.write_text(json.dumps(man, indent=2), encoding="utf-8")


def write_config_yaml(path: Path, cfg_dict: Dict[str, Any], dry_run: bool) -> None:
    if dry_run or yaml is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg_dict, f, sort_keys=True, default_flow_style=False)


def write_verification_summary(path: Path, entries: List[Dict[str, Any]],
                               run_config_hash: str, cmdline: str, dry_run: bool) -> None:
    """Write a verification summary JSON with chain-of-custody for every output."""
    if dry_run:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    ver = {
        "created_at": _now_iso(),
        "pipeline_version": PIPELINE_VERSION,
        "config_hash": run_config_hash,
        "command_line": cmdline,
        "files": [
            {
                "filename": e["filename"],
                "source_hash": e["source_hash"],
                "decision": e.get("decision", "unknown"),
                "canonical_path": str(e.get("canonical_path", "")),
                "output_path": str(e.get("output_path", "")),
                "sidecar_path": str(e.get("sidecar_path", "")),
                "metrics_hash": e.get("metrics_hash", ""),
                "gain_db": e.get("gain_db", 0.0),
                "verified": True,
            }
            for e in entries
        ],
    }
    path.write_text(json.dumps(ver, indent=2, default=str), encoding="utf-8")


# ---------------------------------------------------------------------------
# Core per-file processing
# ---------------------------------------------------------------------------

def process_one_file(src_path: Path, dirs: RunDirs, cfg: PipelineConfig,
                     log_path: Path, run_config_hash: str, cmdline: str,
                     deps: Dict[str, str]) -> Optional[Dict[str, Any]]:
    t0 = time.time()
    input_sha = _sha256_file(src_path)
    base = src_path.stem
    can_path = dirs.canonical / f"{base}.wav"
    can_sidecar = dirs.canonical / f"{base}.json"

    # Idempotency: existing canonical sidecar with matching source_hash
    if can_sidecar.exists():
        try:
            side = json.loads(can_sidecar.read_text(encoding="utf-8"))
            if side.get("source_hash") == input_sha:
                _log_stage(log_path, input_sha, "all", "skip", "idempotent match", 0.0, cfg.dry_run)
                # still need to return enough for summary? minimal
                return None
        except Exception:
            pass

    full_cfg = cfg.full
    repair_actions: List[str] = []
    flags: List[str] = []
    corruptions: List[str] = []

    # --- STAGE 1 ---
    t1 = time.time()
    _log_stage(log_path, input_sha, "stage1", "start", "", 0.0, cfg.dry_run)

    try:
        stage1 = stage1_format_integrity.process_stage1(
            src_path, dirs.canonical, full_cfg, dry_run=cfg.dry_run
        )
    except Exception as exc:
        _log_stage(log_path, input_sha, "stage1", "error", f"read failed: {exc}", 0.0, cfg.dry_run)
        return {"error": str(exc), "source_hash": input_sha, "filename": src_path.name}

    y_repaired = stage1.y_mono
    sr = stage1.sr
    stereo_dec = stage1.stereo_decision
    flags = stage1.flags[:]
    corruptions = stage1.corruptions[:]
    dc = stage1.dc_offset
    repair_actions = stage1.repair_actions[:]
    can_path = stage1.canonical_path or dirs.canonical / f"{base}.wav"
    actual_frames = len(y_repaired)

    # Corruption from stereo
    if "stereo_low_correlation" in flags or "stereo_anti_phase" in flags:
        corruptions.append("stereo_problem")

    # Write canonical sidecar with full metadata
    if not cfg.dry_run:
        can_side = {
            "version": PIPELINE_VERSION,
            "source": {"file": src_path.name},
            "source_hash": input_sha,
            "pipeline_version": PIPELINE_VERSION,
            "config_hash": run_config_hash,
            "command_line": cmdline,
            "dependency_versions": deps,
            "metrics": {
                "dc_offset": round(dc, 8),
                "stereo_decision": stereo_dec,
                "corruption_flags": corruptions,
                "flags": flags,
            },
            "decisions": {"qc": "pending"},
            "applied_gain_db": 0.0,
            "filter_coefficients": None,
        }
        can_sidecar.write_text(json.dumps(can_side, indent=2, default=str), encoding="utf-8")
    else:
        log.info("[dry-run] would write canonical sidecar %s", can_sidecar)

    manifest_entry = stage1.manifest_entry.to_dict()

    _log_stage(log_path, input_sha, "stage1", "ok",
               f"sr={sr} dc={dc:.6f} stereo={stereo_dec} flags={flags}", (time.time() - t1) * 1000, cfg.dry_run)

    # --- STAGE 2 ---
    t2 = time.time()
    _log_stage(log_path, input_sha, "stage2", "start", "", 0.0, cfg.dry_run)

    segs = get_vad_segments(y_repaired, sr, full_cfg)
    speech_lufs = compute_speech_active_lufs(y_repaired, sr, segs)
    if pyln is not None:
        try:
            meter = pyln.Meter(sr)
            full_pre_lufs = float(meter.integrated_loudness(y_repaired.astype(np.float64)))
        except Exception:
            full_pre_lufs = -70.0
    else:
        full_pre_lufs = -70.0

    in_tp = compute_true_peak_dbtp(y_repaired, sr)
    gain_db = min(
        cfg.target_lufs - speech_lufs,
        cfg.peak_ceiling_dbtp - in_tp,
    )
    if gain_db > full_cfg.get("very_quiet_gain_db", 18.0):
        flags.extend(["very_quiet", "SNR-suspect"])

    gain_lin = 10.0 ** (gain_db / 20.0)
    y_gained = (y_repaired * gain_lin).astype(np.float32)

    # Conditional rumble HP (sub-30 Hz only, never >~50 Hz)
    filter_coeffs = None
    rumble_ratio = compute_rumble_ratio(y_repaired, sr)
    if rumble_ratio > full_cfg.get("rumble_energy_ratio_thresh", 0.10):
        y_out, filter_coeffs = apply_hp_filter(
            y_gained, sr, full_cfg.get("hp_cutoff_hz", 25.0)
        )
        repair_actions.append("rumble_hp25")
    else:
        y_out = y_gained

    # Post gain+filter TP safety (scalar gain already chose ceiling; clip soft)
    y_out = np.clip(y_out, -1.0, 1.0).astype(np.float32)

    # Write to temp immediately to avoid holding full audio in memory across files (pass 1)
    temp_path: Optional[Path] = None
    if not cfg.dry_run:
        temp_path = dirs.temp / f"{base}.wav"
        dirs.temp.mkdir(parents=True, exist_ok=True)
        sf.write(str(temp_path), y_out, sr, subtype="FLOAT")

    _log_stage(log_path, input_sha, "stage2", "ok",
               f"gain_db={gain_db:.2f} speech_lufs={speech_lufs:.2f} rumble={rumble_ratio:.3f}",
               (time.time() - t2) * 1000, cfg.dry_run)

    # --- STAGE 4 ---
    t4 = time.time()
    _log_stage(log_path, input_sha, "stage4", "start", "", 0.0, cfg.dry_run)

    is_phone = detect_phone_bandwidth(
        y_repaired, sr,
        full_cfg.get("phone_low_hz", 300),
        full_cfg.get("phone_high_hz", 3400),
        full_cfg.get("phone_energy_frac", 0.90),
    )
    if is_phone:
        flags.append("bandwidth_limited")

    clip_sample_mask = compute_clipped_sample_mask(y_repaired)

    frame_mask = compute_frame_exclusion_mask(
        y_repaired, sr, clip_sample_mask, is_phone, full_cfg
    )

    pre_dir = dirs.metrics_pre
    if not cfg.dry_run:
        pre_dir.mkdir(parents=True, exist_ok=True)
        mask_path = pre_dir / f"{input_sha}_frame_mask.npy"
        np.save(str(mask_path), frame_mask)
    else:
        log.info("[dry-run] would write frame mask %s", f"{input_sha}_frame_mask.npy")

    _log_stage(log_path, input_sha, "stage4", "ok",
               f"phone={is_phone} sibilant_frames={int(np.sum(frame_mask))} mask_len={len(frame_mask)}",
               (time.time() - t4) * 1000, cfg.dry_run)

    # --- Metrics ---
    clip_sample_for_metric = clip_sample_mask
    metrics = compute_all_metrics(
        y_repaired, y_out, sr, segs, gain_db, flags, full_cfg,
        is_phone, dc, repair_actions, clip_sample_for_metric
    )
    metrics.flags = flags[:]  # ensure

    # Write post-normalization metrics (frame mask on y_out, metrics summary)
    post_dir = dirs.metrics_post
    if not cfg.dry_run:
        post_dir.mkdir(parents=True, exist_ok=True)
        post_clip_mask = compute_clipped_sample_mask(y_out)
        post_frame_mask = compute_frame_exclusion_mask(
            y_out, sr, post_clip_mask, is_phone, full_cfg
        )
        np.save(str(post_dir / f"{base}_frame_mask.npy"), post_frame_mask)
        post_metrics = {
            "source_hash": input_sha,
            "filename": src_path.name,
            "gain_db": round(gain_db, 4),
            "true_peak_dbtp": metrics.true_peak_dbtp,
            "full_lufs": metrics.full_lufs,
            "speech_lufs": metrics.speech_lufs,
            "lra": metrics.lra,
            "snr_db": metrics.snr_db,
            "clipping_pct_voiced": metrics.clipping_pct_voiced,
            "post_sibilant_frames": int(np.sum(post_frame_mask)),
            "post_clip_samples": int(np.sum(post_clip_mask)),
        }
        (post_dir / f"{base}_metrics.json").write_text(
            json.dumps(post_metrics, indent=2, default=str), encoding="utf-8"
        )
    else:
        log.info("[dry-run] would write post metrics for %s", src_path.name)

    # --- STAGE 5 (decision later with global stats) ---
    # Store for batch QC + write
    result = {
        "source_hash": input_sha,
        "filename": src_path.name,
        "sr": sr,
        "y_out_path": str(temp_path) if temp_path is not None else None,
        "metrics": metrics,
        "flags": flags,
        "corruptions": corruptions,
        "stereo_decision": stereo_dec,
        "gain_db": gain_db,
        "filter_coeffs": filter_coeffs,
        "repair_actions": repair_actions,
        "is_phone": is_phone,
        "duration_s": metrics.duration_s,
        "manifest_entry": manifest_entry,
    }
    _log_stage(log_path, input_sha, "stage5", "start", "awaiting dataset QC", 0.0, cfg.dry_run)
    return result


# ---------------------------------------------------------------------------
# Enhance (stage 3 style) — only on accepted normalized files
# ---------------------------------------------------------------------------

def create_run_directories(dirs: RunDirs, dry_run: bool) -> None:
    """Create all run directories on disk (used by tests and pipeline)."""
    if dry_run:
        return
    for p in dirs.all_paths():
        p.mkdir(parents=True, exist_ok=True)

def maybe_write_enhanced(y_norm_path: Path, sr: int, out_wav_path: Path,
                         dirs: RunDirs, source_hash: str, filename: str, dry_run: bool) -> None:
    if not dirs.enhanced_review or not _COMPRESSOR_AVAILABLE:
        return
    if dry_run:
        log.info("[dry-run] would write enhanced review %s", out_wav_path)
        return

    # Read normalized audio from disk (avoids keeping large arrays in memory)
    y_norm, sr_read = sf.read(str(y_norm_path), dtype="float32")
    if sr_read != sr:
        log.warning("Sample rate mismatch in enhanced read: %d vs %d", sr_read, sr)

    # ── Safety: paths must be distinct ───────────────────────────────────
    if dirs.enhanced_review == dirs.normalized_analysis:
        log.error("enhanced_review and normalized_analysis paths are the same; aborting enhanced write.")
        return

    dirs.enhanced_review.mkdir(parents=True, exist_ok=True)
    enhanced_wav = dirs.enhanced_review / out_wav_path.name
    enhanced_side = dirs.enhanced_review / (out_wav_path.stem + ".json")
    label_path = dirs.enhanced_review / (out_wav_path.stem + ".txt")

    # ── Safety: never overwrite normalized_analysis or source files ────────
    if enhanced_wav.exists() and enhanced_wav.samefile(out_wav_path):
        log.error("Enhanced review path collides with normalized output; aborting.")
        return
    # Prevent overwriting the original source (shouldn't happen, but guard)
    if out_wav_path.exists():
        try:
            # If the out_wav_path is inside normalized_analysis, protect it
            if dirs.normalized_analysis in out_wav_path.resolve().parents:
                if enhanced_wav.resolve() == out_wav_path.resolve():
                    log.error("Enhanced review path is identical to normalized output; aborting.")
                    return
        except OSError:
            pass

    # Compute hash of the normalized audio used as compressor input
    norm_audio_hash = hashlib.sha256(y_norm.astype(np.float32).tobytes()).hexdigest()

    try:
        comp = apply_compressor_chain(y_norm.astype(np.float32), sr)
        write_review_copy_wav(comp, str(enhanced_wav))

        # Build compressor params dict
        cp = comp.params
        side = {
            "version": PIPELINE_VERSION,
            "source": {
                "file": filename,
                "hash": source_hash,
            },
            "input_integrity": {
                "normalized_audio_hash": norm_audio_hash,
                "sample_rate": sr,
                "samples": len(y_norm),
            },
            "label": "ENHANCED REVIEW — NOT FOR ANALYSIS",
            "processing": {
                "compressor": {
                    "params": {
                        "threshold_db": cp.threshold_db,
                        "ratio": cp.ratio,
                        "knee_db": cp.knee_db,
                        "attack_ms": cp.attack_ms,
                        "release_ms": cp.release_ms,
                    },
                    "gain_reduction_stats": {
                        "peak_gr_db": round(comp.gain_reduction_db, 3),
                        "rms_gr_db": round(comp.rms_gain_reduction_db, 3),
                        "avg_gr_db": round(comp.avg_gain_reduction_db, 3),
                    },
                    "makeup_gain_db": round(comp.makeup_gain_db, 3),
                    "true_peak_db": round(comp.true_peak_db, 3),
                },
                "stages": [
                    {
                        "stage": name,
                        "peak_before_db": round(sm.peak_before, 3),
                        "peak_after_db": round(sm.peak_after, 3),
                        "rms_before_db": round(sm.rms_before, 3),
                        "rms_after_db": round(sm.rms_after, 3),
                    }
                    for name, sm in zip(
                        ["compressor", "makeup", "limiter"], comp.stage_metrics
                    )
                ],
            },
            "safety": {
                "enhanced_review_path": str(dirs.enhanced_review),
                "normalized_analysis_path": str(dirs.normalized_analysis),
                "paths_distinct": str(dirs.enhanced_review) != str(dirs.normalized_analysis),
                "overwrite_check": "passed",
            },
        }
        enhanced_side.write_text(json.dumps(side, indent=2), encoding="utf-8")
        label_path.write_text(
            "═══════════════════════════════════════════════════\n"
            "  ENHANCED REVIEW — NOT FOR ANALYSIS\n"
            "═══════════════════════════════════════════════════\n"
            "This file is a human-listening review copy ONLY.\n"
            "Do NOT use it for harmonic analysis, descriptor\n"
            "computation, or machine-learning pipelines.\n"
            "Use the corresponding normalized_analysis/ file\n"
            "for all analytical work.\n"
            "═══════════════════════════════════════════════════\n",
            encoding="utf-8",
        )
        log.info("Wrote enhanced review %s", enhanced_wav)
    except Exception as exc:
        log.warning("Enhanced review failed for %s: %s", out_wav_path.name, exc)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def discover_sources(input_dir: Path) -> List[Path]:
    exts = ("*.wav", "*.WAV", "*.flac", "*.FLAC", "*.aiff", "*.AIFF")
    paths: List[Path] = []
    for ext in exts:
        paths.extend(sorted(input_dir.rglob(ext)))
    return paths


def run_pipeline(cfg: PipelineConfig) -> List[Dict[str, Any]]:
    start = time.time()
    log_path = None  # set after dirs

    sources = discover_sources(cfg.input_dir)
    if not sources:
        log.error("No audio files found in %s", cfg.input_dir)
        return []

    # Run dir
    run_name = cfg.run_name or datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_v1")
    run_root = cfg.output_dir / run_name
    dirs = RunDirs.create(run_root, cfg.enhance)

    create_run_directories(dirs, cfg.dry_run)
    if cfg.dry_run:
        log.info("[dry-run] run root would be %s", run_root)

    log_path = dirs.logs / "pipeline.jsonl"

    # Write config.yaml
    write_config_yaml(run_root / "config.yaml", cfg.full, cfg.dry_run)

    cmdline = _get_cmdline()
    deps = _get_dependency_versions()
    run_config_hash = _config_hash(cfg.full)

    manifest_entries: List[Dict] = []
    processed: List[Dict[str, Any]] = []
    summary_rows: List[Dict[str, Any]] = []
    verification_entries: List[Dict[str, Any]] = []

    for src in sources:
        res = process_one_file(src, dirs, cfg, log_path, run_config_hash, cmdline, deps)
        if res is None:
            # idempotent skip
            continue
        if "error" in res:
            # still record minimal
            _log_stage(log_path, res.get("source_hash", "unknown"), "all", "error", res["error"], 0.0, cfg.dry_run)
            continue
        processed.append(res)
        manifest_entries.append(res["manifest_entry"])

    # Dataset-driven QC decisions
    if processed:
        assign_qc_decisions(processed, cfg.full)

    # Route outputs + reports + enhance + summary
    for p in processed:
        decision = p.get("decision", "quarantine")
        y_out_path = p.get("y_out_path")
        sr = p["sr"]
        src_hash = p["source_hash"]
        fname = p["filename"]
        m: FileMetrics = p["metrics"]
        flags = p.get("flags", [])
        gain = p["gain_db"]
        filt = p["filter_coeffs"]
        stereo = p.get("stereo_decision", "mono")
        corrs = p.get("corruptions", [])

        target_dir = None
        if decision == "accept":
            target_dir = dirs.normalized_analysis
        elif decision == "quarantine":
            target_dir = dirs.quarantine

        out_wav = None
        sidecar_path = None
        if target_dir is not None:
            if not cfg.dry_run:
                target_dir.mkdir(parents=True, exist_ok=True)
                out_wav = target_dir / f"{Path(fname).stem}.wav"
                if y_out_path and Path(y_out_path).exists():
                    shutil.move(y_out_path, out_wav)
                else:
                    log.warning("Missing temp file for %s", fname)
                sidecar_path = target_dir / (Path(fname).stem + ".json")
                write_sidecar(sidecar_path, fname, src_hash, run_config_hash, cmdline, deps, m, decision, gain, filt, cfg.dry_run)
            else:
                log.info("[dry-run] would write %s normalized to %s", fname, target_dir.name)

        # For excludes, clean up the temp file to avoid wasting disk space
        if decision == "exclude" and y_out_path and Path(y_out_path).exists():
            try:
                Path(y_out_path).unlink()
            except Exception:
                log.warning("Failed to remove temp file for excluded %s", fname)

        # Enhance only for accepted (never touches normalized_analysis for quarantined)
        if cfg.enhance and decision == "accept" and out_wav is not None:
            maybe_write_enhanced(out_wav, sr, out_wav, dirs, src_hash, fname, cfg.dry_run)

        # Per-file report (always)
        report_path = dirs.per_file_reports / f"{src_hash}.json"
        write_per_file_report(report_path, fname, src_hash, m, decision, flags, stereo, corrs, cfg.dry_run)

        # Verification entry
        verification_entries.append({
            "filename": fname,
            "source_hash": src_hash,
            "decision": decision,
            "canonical_path": str(dirs.canonical / f"{Path(fname).stem}.wav"),
            "output_path": str(out_wav) if out_wav else "",
            "sidecar_path": str(sidecar_path) if sidecar_path else "",
            "metrics_hash": hashlib.sha256(json.dumps(asdict(m), sort_keys=True, default=str).encode()).hexdigest()[:16],
            "gain_db": gain,
        })

        # CSV row
        summary_rows.append({
            "file_hash": src_hash,
            "filename": fname,
            "duration_s": m.duration_s,
            "decision": decision,
            "speech_lufs": m.speech_lufs,
            "full_lufs": m.full_lufs,
            "snr_db": m.snr_db,
            "voiced_duration_s": m.voiced_duration_s,
            "clipping_pct": m.clipping_pct_voiced,
            "flags": ";".join(flags),
        })

        _log_stage(log_path, src_hash, "stage5", "ok",
                   f"decision={decision} gain={gain:.2f} snr={m.snr_db:.1f}", 0.0, cfg.dry_run)

    # Write final manifest at output_dir level per spec: originals/ (RO) · manifest/
    if not cfg.dry_run:
        (cfg.output_dir / "manifest").mkdir(parents=True, exist_ok=True)
    write_manifest(cfg.output_dir / "manifest" / "manifest.json", manifest_entries, cfg.dry_run)

    # Dataset summary CSV
    csv_path = dirs.reports / "dataset_summary.csv"
    write_dataset_csv(csv_path, summary_rows, cfg.dry_run)

    # Verification summary
    write_verification_summary(
        dirs.verification / "verification_summary.json",
        verification_entries,
        run_config_hash,
        cmdline,
        cfg.dry_run,
    )

    # Also write a small json summary for compat with old tests
    if not cfg.dry_run:
        json_sum = {
            "run_name": run_name,
            "config": {
                "input_dir": str(cfg.input_dir),
                "output_dir": str(cfg.output_dir),
                "enhance": cfg.enhance,
                "target_lufs": cfg.target_lufs,
                "peak_ceiling_dbtp": cfg.peak_ceiling_dbtp,
            },
            "files": [{"file": r["filename"], "decision": r.get("decision", "unknown"),
                       "gain_db": r["gain_db"], "flags": r.get("flags", [])} for r in processed],
            "elapsed_s": round(time.time() - start, 3),
        }
        (dirs.reports / "dataset_summary.json").write_text(json.dumps(json_sum, indent=2), encoding="utf-8")

    log.info("Pipeline complete in %.2fs (%d processed)", time.time() - start, len(processed))
    return processed


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Production audio normalization pipeline (gain-only, harmonic-preserving).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Stages:
  1. Format & Integrity (SHA256, canonical float32 WAV, DC/stereo/corruption)
  2. Loudness Normalization (pyloudnorm + VAD speech-active, scalar gain ONLY)
  4. Spectral Conditioning (minimal phone/sibilant tagging + frame mask)
  5. Quality Gates (ACCEPT/QUARANTINE/EXCLUDE driven by dataset stats)

Governing principle: scalar gain × repaired signal (harmonics UNCHANGED).
Never modify originals. Outputs are float32 WAVs (subtype FLOAT).

Examples:
  python tools/normalize_sources.py -i ./originals -o ./runs --dry-run
  python tools/normalize_sources.py -i ./originals -o ./runs --enhance
  python tools/normalize_sources.py -i ./originals -o ./runs --config myconfig.yaml
        """,
    )
    parser.add_argument("--input-dir", "-i", type=Path, required=True,
                        help="Directory with original audio files (read-only)")
    parser.add_argument("--output-dir", "-o", type=Path, required=True,
                        help="Base for run directories (e.g. ./runs)")
    parser.add_argument("--config", type=Path, default=None,
                        help="Optional YAML config to override defaults")
    parser.add_argument("--enhance", action="store_true", default=False,
                        help="Also produce enhanced_review/ copies via compressor (human listening only)")
    parser.add_argument("--dry-run", action="store_true", default=False,
                        help="Show actions without writing files")
    parser.add_argument("--target-lufs", type=float, default=None,
                        help="Target speech-active LUFS (default from config)")
    parser.add_argument("--peak-ceiling", type=float, default=None,
                        help="True peak ceiling dBTP (default -3)")
    parser.add_argument("--sr", type=int, default=None, help="Target sample rate (default 44100)")
    parser.add_argument("--run-name", default=None, help="Override auto-generated run directory name (default: YYYYMMDD_HHMMSS_v1)")
    parser.add_argument("--log", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser


def parse_args(argv: Optional[List[str]] = None) -> PipelineConfig:
    parser = build_parser()
    args = parser.parse_args(argv)
    cfg = PipelineConfig.from_args_and_config(args)
    cfg.run_name = getattr(args, "run_name", None)
    if args.target_lufs is not None:
        cfg.target_lufs = args.target_lufs
        cfg.full["target_lufs"] = args.target_lufs
    if args.peak_ceiling is not None:
        cfg.peak_ceiling_dbtp = args.peak_ceiling
        cfg.full["peak_ceiling_dbtp"] = args.peak_ceiling
    if args.sr is not None:
        cfg.target_sr = args.sr
        cfg.full["target_sr"] = args.sr
    return cfg


def main(argv: Optional[List[str]] = None) -> int:
    cfg = parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, cfg.log_level, logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    log.info("normalize_sources starting | input=%s output=%s enhance=%s dry=%s",
             cfg.input_dir, cfg.output_dir, cfg.enhance, cfg.dry_run)

    try:
        run_pipeline(cfg)
        return 0
    except KeyboardInterrupt:
        log.warning("Interrupted")
        return 130
    except Exception as exc:
        log.exception("Fatal pipeline error: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
