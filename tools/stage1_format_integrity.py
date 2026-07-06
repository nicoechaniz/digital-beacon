"""Stage 1: Format & Integrity pipeline for digital-beacon sources.

Handles:
  1. Read-only access to originals with SHA256 computation per file
  2. Canonical float32 conversion (int16→float32 is lossless, no dither)
  3. DC offset subtraction (mean removal), with conditional zero-phase ~25Hz
     2-pole HP via scipy.signal.filtfilt only when DC is time-varying
  4. Stereo→mono handling: cross-correlate channels for lag detection, average
     if r≥0.98 and lag≈0, select cleaner single channel if lagged-but-correlated,
     quarantine flag if anti-phase or low-correlation
  5. Corruption detection: decode errors, header/sample count mismatch, dropouts,
     flat-top clipping runs, truncation
  6. Immutable manifest generation as JSON with fields: sha256, duration,
     sample_rate, channels, and all flags

Uses ffprobe for metadata extraction, ffmpeg for decode-repair attempts.
Output: float32 canonical copies to canonical/, manifest JSON.
"""
from __future__ import annotations

import hashlib
import json
import logging
import subprocess
import tempfile
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import soundfile as sf

try:
    from scipy import signal as sp_signal
    from scipy.ndimage import label as nd_label
except ImportError:
    sp_signal = None
    nd_label = None

log = logging.getLogger("stage1_format_integrity")


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Stage1ManifestEntry:
    """Immutable entry in the Stage 1 manifest."""
    sha256: str
    duration: float
    sample_rate: int
    channels: int
    filename: str
    flags: List[str] = field(default_factory=list)
    stereo_decision: str = "mono"
    dc_offset: float = 0.0
    corruption_flags: List[str] = field(default_factory=list)
    repair_actions: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Stage1Result:
    """Result of processing a single source file through Stage 1."""
    canonical_path: Optional[Path]
    manifest_entry: Stage1ManifestEntry
    y_mono: np.ndarray
    sr: int
    flags: List[str]
    repair_actions: List[str]
    stereo_decision: str
    corruptions: List[str]
    dc_offset: float
    metadata: Dict[str, Any]
    ffprobe_info: Dict[str, Any]


# ---------------------------------------------------------------------------
# SHA256 (read-only)
# ---------------------------------------------------------------------------

def sha256_file(path: Path) -> str:
    """Chunked SHA256 of a file (read-only)."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# ffprobe / ffmpeg helpers
# ---------------------------------------------------------------------------

def ffprobe_metadata(path: Path, timeout: int = 30) -> Dict[str, Any]:
    """Extract metadata via ffprobe -show_format -show_streams -of json."""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_format", "-show_streams",
        "-of", "json",
        str(path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if result.returncode != 0:
            return {"error": result.stderr.strip(), "command": " ".join(cmd)}
        return json.loads(result.stdout)
    except subprocess.TimeoutExpired:
        return {"error": "ffprobe timed out", "command": " ".join(cmd)}
    except Exception as exc:
        return {"error": str(exc), "command": " ".join(cmd)}


def ffmpeg_decode_repair(
    path: Path,
    out_path: Path,
    target_sr: int = 44100,
    timeout: int = 120,
) -> bool:
    """Attempt decode-repair via ffmpeg to a float32 LE WAV.

    Returns True if ffmpeg exits with 0 and the output file exists.
    """
    cmd = [
        "ffmpeg", "-y", "-v", "error",
        "-i", str(path),
        "-c:a", "pcm_f32le",
        "-ar", str(target_sr),
        str(out_path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if result.returncode != 0:
            log.warning("ffmpeg decode-repair failed for %s: %s", path.name, result.stderr.strip())
            return False
        return out_path.exists() and out_path.stat().st_size > 44
    except subprocess.TimeoutExpired:
        log.warning("ffmpeg decode-repair timed out for %s", path.name)
        return False
    except Exception as exc:
        log.warning("ffmpeg decode-repair exception for %s: %s", path.name, exc)
        return False


# ---------------------------------------------------------------------------
# Canonical float32 read
# ---------------------------------------------------------------------------

def read_canonical_float32(
    path: Path,
    target_sr: Optional[int] = None,
) -> Tuple[np.ndarray, int, sf.SoundFileInfo, List[str]]:
    """Read audio into float32, optionally resampling, returning (y, sr, info, flags).

    soundfile is tried first; if it fails, ffmpeg decode-repair is attempted.
    """
    flags: List[str] = []
    info: sf.SoundFileInfo

    # Try soundfile first
    try:
        info = sf.info(str(path))
        y, sr = sf.read(str(path), dtype="float32", always_2d=False)
        actual_frames = len(y) if y.ndim == 1 else y.shape[0]
    except Exception as exc:
        log.warning("soundfile read failed for %s: %s", path.name, exc)
        flags.append("soundfile_decode_error")
        # Try ffmpeg repair
        with tempfile.TemporaryDirectory() as td:
            repair_path = Path(td) / "repaired.wav"
            if ffmpeg_decode_repair(path, repair_path, target_sr=target_sr or 44100):
                try:
                    info = sf.info(str(repair_path))
                    y, sr = sf.read(str(repair_path), dtype="float32", always_2d=False)
                    actual_frames = len(y) if y.ndim == 1 else y.shape[0]
                    flags.append("ffmpeg_repair_used")
                except Exception as exc2:
                    log.error("ffmpeg repair also failed for %s: %s", path.name, exc2)
                    raise RuntimeError(f"Cannot decode {path.name}: {exc2}") from exc2
            else:
                raise RuntimeError(f"Cannot decode {path.name}: {exc}") from exc

    # Subtype check: int16→float32 is handled by sf.read automatically and is lossless
    if info.subtype == "PCM_16":
        pass  # already normalized by soundfile
    elif info.subtype not in ("FLOAT", "PCM_32", "PCM_24"):
        flags.append(f"uncommon_subtype_{info.subtype}")

    # Ensure float32
    if y.dtype != np.float32:
        y = y.astype(np.float32, copy=False)

    # Resample if needed
    if target_sr is not None and sr != target_sr:
        if sp_signal is not None:
            y = sp_signal.resample(y, int(len(y) * target_sr / sr))
            sr = target_sr
            flags.append("resampled")
        else:
            flags.append("resample_needed_but_scipy_missing")

    return y, sr, info, flags


# ---------------------------------------------------------------------------
# Corruption detection
# ---------------------------------------------------------------------------

def _fallback_label(boolean_mask: np.ndarray) -> Tuple[np.ndarray, int]:
    """Scipy-free connected-component label."""
    labeled = np.cumsum(
        np.diff(np.concatenate([[0], boolean_mask.astype(int)])) != 0
    ) + 1
    labeled[~boolean_mask] = 0
    numf = int(np.max(labeled))
    return labeled, numf


def detect_corruptions(
    y: np.ndarray,
    info: sf.SoundFileInfo,
    actual_frames: int,
) -> List[str]:
    """Detect corruption issues in decoded audio.

    Checks:
      - header/sample count mismatch (declared vs actual frames)
      - flat-top clipping runs (≥5 samples at |y| ≥ 0.9995)
      - dropouts (zero runs ≥100 samples)
      - silent/decode failure (max |y| < 1e-9)
      - truncation (actual_frames < declared frames by >0.1%)
    """
    flags: List[str] = []
    nd_label_local = _fallback_label if nd_label is None else nd_label

    declared = getattr(info, "frames", 0)
    if declared and abs(declared - actual_frames) > max(1, int(0.001 * declared)):
        flags.append("header_sample_count_mismatch")
    if declared and actual_frames < declared - max(1, int(0.001 * declared)):
        flags.append("truncation")

    # flat-top clipping runs
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

    # dropouts (zero runs ≥100 samples)
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

    # silent / decode fail
    if float(np.max(np.abs(y))) < 1e-9:
        flags.append("silent_or_decode_fail")

    return flags


# ---------------------------------------------------------------------------
# Stereo→mono
# ---------------------------------------------------------------------------

def stereo_to_mono(
    y: np.ndarray,
    sr: int,
    high_corr: float = 0.98,
    low_corr: float = 0.70,
    max_lag: int = 64,
) -> Tuple[str, np.ndarray, List[str]]:
    """Cross-correlate stereo decision.

    Returns (decision, mono_signal, flags).

    Decision logic:
      - If anti-phase (r < -0.05): select cleaner channel, flag "stereo_anti_phase"
      - If r ≥ high_corr and |lag| ≤ max(1, max_lag): average channels
      - If r ≥ low_corr: select cleaner channel (lower peak-to-RMS)
      - Otherwise: flag "stereo_low_correlation", select channel 0
    """
    if y.ndim == 1:
        return "mono", y.astype(np.float32), []
    if y.shape[1] < 2:
        return "mono", y[:, 0].astype(np.float32), []

    ch0 = y[:, 0].astype(np.float64)
    ch1 = y[:, 1].astype(np.float64)
    flags: List[str] = []

    # Cross-correlation (zero-mean)
    c0 = ch0 - ch0.mean()
    c1 = ch1 - ch1.mean()
    if sp_signal is not None:
        corr = sp_signal.correlate(c0, c1, mode="full")
    else:
        corr = np.correlate(c0, c1, mode="full")
    norm = np.sqrt(np.sum(c0 * c0) * np.sum(c1 * c1)) + 1e-12
    corr = corr / norm
    peak_idx = int(np.argmax(np.abs(corr)))
    max_r = float(corr[peak_idx])
    lag_samples = peak_idx - (len(c0) - 1)

    def _peak_to_rms(ch: np.ndarray) -> float:
        return float(np.max(np.abs(ch))) / (np.sqrt(np.mean(ch * ch)) + 1e-9)

    if max_r < -0.05:  # anti-phase
        flags.append("stereo_anti_phase")
        p2r0 = _peak_to_rms(ch0)
        p2r1 = _peak_to_rms(ch1)
        mono = (ch0 if p2r0 <= p2r1 else ch1).astype(np.float32)
        return "select_cleaner_anti_phase", mono, flags

    abs_r = abs(max_r)
    if abs_r >= high_corr and abs(lag_samples) <= max(1, max_lag):
        mono = ((ch0 + ch1) / 2.0).astype(np.float32)
        return "average_channels", mono, flags

    if abs_r >= low_corr:
        p2r0 = _peak_to_rms(ch0)
        p2r1 = _peak_to_rms(ch1)
        mono = (ch0 if p2r0 <= p2r1 else ch1).astype(np.float32)
        return f"select_cleaner_lag={lag_samples}", mono, flags

    # low correlation
    flags.append("stereo_low_correlation")
    mono = ch0.astype(np.float32)
    return "select_ch0_low_corr", mono, flags


# ---------------------------------------------------------------------------
# DC offset + conditional HP
# ---------------------------------------------------------------------------

def detect_time_varying_dc(
    y: np.ndarray,
    sr: int,
    win_s: float = 0.5,
    var_thresh: float = 5e-6,
) -> bool:
    """Return True if DC offset varies significantly across windows."""
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


def apply_zero_phase_hp(
    y: np.ndarray,
    sr: int,
    cutoff: float = 25.0,
    order: int = 2,
) -> np.ndarray:
    """Apply zero-phase high-pass filter via filtfilt (2×order poles)."""
    if sp_signal is None:
        return y.astype(np.float32)
    b, a = sp_signal.butter(order, cutoff, btype="high", fs=sr)
    yf = sp_signal.filtfilt(b, a, y)
    return yf.astype(np.float32)


def dc_subtract_and_conditional_hp(
    y: np.ndarray,
    sr: int,
    full_cfg: Dict[str, Any],
) -> Tuple[np.ndarray, float, List[str]]:
    """Subtract DC mean; if time-varying, apply zero-phase ~25Hz 2-pole HP.

    Returns (y_repaired, dc_value, repair_actions).
    """
    repair_actions: List[str] = []
    dc = float(np.mean(y))
    y_repaired = (y - dc).astype(np.float32)
    repair_actions.append("dc_subtract_mean")

    if detect_time_varying_dc(
        y_repaired.astype(np.float64),
        sr,
        full_cfg.get("dc_var_window_s", 0.5),
        full_cfg.get("dc_var_thresh", 5e-6),
    ):
        y_repaired = apply_zero_phase_hp(
            y_repaired,
            sr,
            full_cfg.get("hp_cutoff_hz", 25.0),
        )
        repair_actions.append("timevar_dc_hp25")

    return y_repaired, dc, repair_actions


# ---------------------------------------------------------------------------
# Main Stage 1 processor
# ---------------------------------------------------------------------------

def process_stage1(
    src_path: Path,
    canonical_dir: Path,
    cfg: Dict[str, Any],
    dry_run: bool = False,
) -> Stage1Result:
    """Process a single source file through Stage 1.

    Steps:
      1. SHA256 (read-only)
      2. ffprobe metadata
      3. Canonical float32 read (with ffmpeg fallback)
      4. Corruption detection
      5. Stereo→mono handling
      6. DC offset subtraction + conditional HP
      7. Write canonical float32 WAV
      8. Return result + manifest entry
    """
    flags: List[str] = []
    repair_actions: List[str] = []
    corruptions: List[str] = []

    # 1. SHA256
    input_sha = sha256_file(src_path)

    # 2. ffprobe metadata
    ffprobe_info = ffprobe_metadata(src_path)
    if "error" in ffprobe_info:
        flags.append("ffprobe_error")

    # 3. Read canonical float32
    target_sr = cfg.get("target_sr", 44100)
    y, sr, info, read_flags = read_canonical_float32(src_path, target_sr=target_sr)
    flags.extend(read_flags)

    actual_frames = len(y) if y.ndim == 1 else y.shape[0]

    # 4. Corruption detection (on first channel if stereo, or mono)
    y_for_corruption = y[:, 0] if y.ndim > 1 else y
    corruptions = detect_corruptions(y_for_corruption, info, actual_frames)
    flags.extend(corruptions)

    # 5. Stereo→mono
    stereo_dec, y_mono, stereo_flags = stereo_to_mono(
        y,
        sr,
        cfg.get("stereo_high_corr", 0.98),
        cfg.get("stereo_low_corr", 0.70),
        cfg.get("stereo_max_lag_samples", 64),
    )
    flags.extend(stereo_flags)

    if y_mono.ndim > 1:
        y_mono = y_mono[:, 0]

    # 6. DC subtraction + conditional HP
    y_repaired, dc, dc_actions = dc_subtract_and_conditional_hp(y_mono, sr, cfg)
    repair_actions.extend(dc_actions)

    # 7. Write canonical WAV (float32)
    base = src_path.stem
    can_path = canonical_dir / f"{base}.wav"
    if not dry_run:
        canonical_dir.mkdir(parents=True, exist_ok=True)
        sf.write(str(can_path), y_repaired, sr, subtype="FLOAT")
    else:
        log.info("[dry-run] would write canonical %s", can_path)

    # Build manifest entry
    manifest_entry = Stage1ManifestEntry(
        sha256=input_sha,
        duration=round(actual_frames / float(sr), 3),
        sample_rate=sr,
        channels=1,  # canonical is always mono
        filename=src_path.name,
        flags=flags[:],
        stereo_decision=stereo_dec,
        dc_offset=round(dc, 8),
        corruption_flags=corruptions[:],
        repair_actions=repair_actions[:],
    )

    metadata = {
        "original_subtype": info.subtype,
        "original_format": info.format,
        "original_samplerate": info.samplerate,
        "original_channels": info.channels,
        "actual_frames": actual_frames,
    }

    return Stage1Result(
        canonical_path=can_path if not dry_run else None,
        manifest_entry=manifest_entry,
        y_mono=y_repaired,
        sr=sr,
        flags=flags[:],
        repair_actions=repair_actions[:],
        stereo_decision=stereo_dec,
        corruptions=corruptions[:],
        dc_offset=dc,
        metadata=metadata,
        ffprobe_info=ffprobe_info,
    )


# ---------------------------------------------------------------------------
# Manifest I/O
# ---------------------------------------------------------------------------

def write_manifest(
    manifest_path: Path,
    entries: List[Stage1ManifestEntry],
    pipeline_version: str = "1.0.0",
    dry_run: bool = False,
) -> None:
    """Write an immutable manifest JSON."""
    if dry_run:
        return
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    man = {
        "version": pipeline_version,
        "stage": "format_and_integrity",
        "entries": [e.to_dict() for e in entries],
    }
    manifest_path.write_text(json.dumps(man, indent=2, default=str), encoding="utf-8")


def read_manifest(manifest_path: Path) -> Dict[str, Any]:
    """Read a manifest JSON."""
    return json.loads(manifest_path.read_text(encoding="utf-8"))
