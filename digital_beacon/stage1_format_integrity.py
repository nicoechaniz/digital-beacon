"""Stage 1: Format & Integrity pipeline for digital-beacon.

Handles:
1. Read-only access to originals with SHA256 computation per file
2. Metadata extraction via ffprobe
3. Decode-repair fallback via ffmpeg
4. Canonical float32 conversion (int16->float32 lossless, no dither)
5. DC offset subtraction + conditional zero-phase ~25Hz 2-pole HP
6. Stereo->mono handling with cross-correlation, lag detection
7. Corruption detection (decode errors, header mismatch, dropouts, clipping, truncation)
8. Immutable manifest entry generation

All outputs are float32. Originals are never modified.
"""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
import tempfile
from dataclasses import dataclass, field
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

@dataclass
class Stage1Result:
    """Output of Stage 1 processing for a single source file."""

    source_path: Path
    sha256: str
    canonical_path: Optional[Path] = None
    sidecar_path: Optional[Path] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    flags: List[str] = field(default_factory=list)
    corruptions: List[str] = field(default_factory=list)
    stereo_decision: str = "mono"
    dc_offset: float = 0.0
    repair_actions: List[str] = field(default_factory=list)
    manifest_entry: Dict[str, Any] = field(default_factory=dict)
    y_canonical: Optional[np.ndarray] = None
    sr: int = 0
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# SHA256
# ---------------------------------------------------------------------------

def compute_sha256(path: Path) -> str:
    """Chunked SHA256 of a file (read-only)."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# ffprobe / ffmpeg
# ---------------------------------------------------------------------------

def _ffprobe_available() -> bool:
    try:
        subprocess.run(["ffprobe", "-version"], capture_output=True, check=True)
        return True
    except Exception:
        return False


def run_ffprobe(path: Path) -> Dict[str, Any]:
    """Extract metadata via ffprobe. Returns JSON dict with format and streams."""
    if not _ffprobe_available():
        return {}
    cmd = [
        "ffprobe", "-v", "error",
        "-show_format", "-show_streams",
        "-of", "json",
        str(path),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if proc.returncode != 0:
            log.warning("ffprobe failed for %s: %s", path, proc.stderr[:200])
            return {}
        data = json.loads(proc.stdout)
        return data
    except Exception as exc:
        log.warning("ffprobe exception for %s: %s", path, exc)
        return {}


def _ffmpeg_available() -> bool:
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
        return True
    except Exception:
        return False


def attempt_ffmpeg_repair(src: Path, dst: Path, target_sr: Optional[int] = None) -> bool:
    """Attempt decode-repair via ffmpeg. Writes float32 LE WAV to dst.

    Returns True if ffmpeg succeeded and dst was written.
    """
    if not _ffmpeg_available():
        return False
    sr_arg = []
    if target_sr is not None:
        sr_arg = ["-ar", str(target_sr)]
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-err_detect", "ignore_err",
        "-i", str(src),
        "-c:a", "pcm_f32le",
        "-f", "wav",
    ] + sr_arg + [str(dst)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if proc.returncode != 0:
            log.warning("ffmpeg repair failed for %s: %s", src, proc.stderr[:200])
            return False
        return dst.exists() and dst.stat().st_size > 44  # minimal WAV header
    except Exception as exc:
        log.warning("ffmpeg repair exception for %s: %s", src, exc)
        return False


# ---------------------------------------------------------------------------
# Audio loading with fallback
# ---------------------------------------------------------------------------

def load_audio_with_fallback(
    path: Path,
    target_sr: Optional[int] = None,
) -> Tuple[np.ndarray, int, Dict[str, Any]]:
    """Load audio read-only. Tries soundfile first, falls back to ffmpeg repair.

    Returns (y, sr, metadata_dict).  y is float32.
    """
    metadata: Dict[str, Any] = {}
    ffprobe_data = run_ffprobe(path)
    if ffprobe_data:
        metadata["ffprobe"] = ffprobe_data
        # Extract common fields
        fmt = ffprobe_data.get("format", {})
        streams = ffprobe_data.get("streams", [])
        if streams:
            audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), streams[0])
            metadata["duration_from_container"] = _parse_float(fmt.get("duration"))
            metadata["sample_rate_from_container"] = _parse_int(audio_stream.get("sample_rate"))
            metadata["channels_from_container"] = _parse_int(audio_stream.get("channels"))
            metadata["codec_name"] = audio_stream.get("codec_name", "unknown")
            metadata["bit_rate"] = _parse_int(fmt.get("bit_rate"))

    # Primary: soundfile
    try:
        info = sf.info(str(path))
        y, sr = sf.read(str(path), dtype="float32", always_2d=False)
        metadata["soundfile_info"] = {
            "samplerate": info.samplerate,
            "channels": info.channels,
            "frames": info.frames,
            "duration": info.duration,
            "subtype": info.subtype,
            "format": info.format,
        }
        if target_sr is not None and sr != target_sr:
            if sp_signal is not None:
                y = sp_signal.resample_poly(y.astype(np.float64), up=target_sr, down=sr).astype(np.float32)
                sr = target_sr
        return y, sr, metadata
    except Exception as exc:
        log.warning("soundfile read failed for %s: %s", path, exc)

    # Fallback: ffmpeg repair decode
    if _ffmpeg_available():
        with tempfile.TemporaryDirectory() as td:
            tmp_wav = Path(td) / "repaired.wav"
            if attempt_ffmpeg_repair(path, tmp_wav, target_sr=target_sr):
                try:
                    y, sr = sf.read(str(tmp_wav), dtype="float32", always_2d=False)
                    metadata["ffmpeg_repair"] = True
                    metadata["soundfile_info"] = {
                        "samplerate": sr,
                        "channels": 1 if y.ndim == 1 else y.shape[1],
                        "frames": len(y) if y.ndim == 1 else y.shape[0],
                        "duration": len(y) / float(sr) if y.ndim == 1 else y.shape[0] / float(sr),
                        "subtype": "FLOAT",
                        "format": "WAV",
                    }
                    return y, sr, metadata
                except Exception as exc2:
                    log.warning("soundfile read of ffmpeg repair failed for %s: %s", path, exc2)

    raise RuntimeError(f"Unable to decode audio: {path}")


def _parse_float(val: Any) -> Optional[float]:
    if val is None:
        return None
    try:
        return float(val)
    except Exception:
        return None


def _parse_int(val: Any) -> Optional[int]:
    if val is None:
        return None
    try:
        return int(val)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Canonical float32 conversion
# ---------------------------------------------------------------------------

def to_canonical_float32(y: np.ndarray, original_subtype: Optional[str] = None) -> np.ndarray:
    """Ensure float32 without dither. Int16->float32 is exact / lossless."""
    if y.dtype == np.float32:
        return y
    if y.dtype == np.float64:
        return y.astype(np.float32)
    # Integer formats: cast to float32 is exact for 16-bit, very close for 24-bit
    return y.astype(np.float32)


# ---------------------------------------------------------------------------
# DC offset and conditional HP
# ---------------------------------------------------------------------------

def detect_time_varying_dc(y: np.ndarray, sr: int, win_s: float, var_thresh: float) -> bool:
    """True if DC offset varies across windows (e.g. varying DC bias)."""
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
    """Zero-phase high-pass via filtfilt (effective 4-pole)."""
    if sp_signal is None:
        return y.astype(np.float32)
    b, a = sp_signal.butter(order, cutoff, btype="high", fs=sr)
    yf = sp_signal.filtfilt(b, a, y)
    return yf.astype(np.float32)


def subtract_dc_and_condition(
    y: np.ndarray, sr: int, cfg: Dict[str, Any]
) -> Tuple[np.ndarray, float, List[str]]:
    """Subtract mean DC. If DC is time-varying, apply zero-phase 25Hz HP.

    Returns (y_repaired, dc_value, repair_actions).
    """
    dc = float(np.mean(y))
    y_repaired = (y - dc).astype(np.float32)
    actions: List[str] = ["dc_subtract_mean"]

    if detect_time_varying_dc(
        y_repaired.astype(np.float64),
        sr,
        cfg.get("dc_var_window_s", 0.5),
        cfg.get("dc_var_thresh", 5e-6),
    ):
        y_repaired = apply_zero_phase_hp(
            y_repaired, sr, cfg.get("hp_cutoff_hz", 25.0)
        )
        actions.append("timevar_dc_hp25")

    return y_repaired, dc, actions


# ---------------------------------------------------------------------------
# Stereo -> mono
# ---------------------------------------------------------------------------

def stereo_to_mono(
    y: np.ndarray, sr: int, cfg: Dict[str, Any]
) -> Tuple[str, np.ndarray, List[str]]:
    """Cross-correlate stereo channels and decide how to collapse to mono.

    Rules:
    - r >= high_corr (0.98) and |lag| <= max_lag -> average channels
    - r >= low_corr (0.70) but lag or lower r -> select cleaner single channel
    - max_r < -0.05 (anti-phase) -> flag + select cleaner channel
    - r < low_corr -> flag + select ch0

    Returns (decision_string, mono_signal, flags).
    """
    if y.ndim == 1:
        return "mono", y.astype(np.float32), []
    if y.shape[1] < 2:
        return "mono", y[:, 0].astype(np.float32), []

    ch0 = y[:, 0].astype(np.float64)
    ch1 = y[:, 1].astype(np.float64)
    flags: List[str] = []

    high_corr = cfg.get("stereo_high_corr", 0.98)
    low_corr = cfg.get("stereo_low_corr", 0.70)
    max_lag = cfg.get("stereo_max_lag_samples", 64)

    # Cross correlation (zero-mean)
    c0 = ch0 - ch0.mean()
    c1 = ch1 - ch1.mean()
    corr = sp_signal.correlate(c0, c1, mode="full") if sp_signal else np.correlate(c0, c1, mode="full")
    norm = np.sqrt(np.sum(c0 * c0) * np.sum(c1 * c1)) + 1e-12
    corr = corr / norm
    peak_idx = int(np.argmax(np.abs(corr)))
    max_r = float(corr[peak_idx])
    lag_samples = peak_idx - (len(c0) - 1)

    # Anti-phase detection
    if max_r < -0.05:
        flags.append("stereo_anti_phase")
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

    # Low correlation
    flags.append("stereo_low_correlation")
    mono = ch0.astype(np.float32)
    return "select_ch0_low_corr", mono, flags


# ---------------------------------------------------------------------------
# Corruption detection
# ---------------------------------------------------------------------------

def _fallback_label(boolean_mask: np.ndarray) -> Tuple[np.ndarray, int]:
    """Scipy-free connected-component label."""
    labeled = np.cumsum(
        np.diff(np.concatenate([[0], boolean_mask.astype(int)])) != 0
    ) + 1
    labeled[~boolean_mask] = 0
    numf = int(np.max(labeled)) if boolean_mask.any() else 0
    return labeled, numf


def detect_corruptions(
    y: np.ndarray,
    declared_frames: Optional[int],
    actual_frames: int,
    original_subtype: str = "",
) -> List[str]:
    """Detect corruption signatures.

    Checks:
    - Header / declared frame count mismatch vs actual frames
    - Flat-top clipping runs (>=5 samples at |y| >= 0.9995)
    - Dropouts (zero runs >=100 samples)
    - Silent / decode-fail (max |y| < 1e-9)
    """
    flags: List[str] = []
    nd_label_local = _fallback_label if nd_label is None else nd_label

    if declared_frames is not None and declared_frames > 0:
        if abs(declared_frames - actual_frames) > max(1, int(0.001 * declared_frames)):
            flags.append("truncation_or_header_mismatch")

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

    # dropouts (zero runs >= 100 samples)
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


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------

def build_manifest_entry(
    filename: str,
    sha256: str,
    duration: float,
    sample_rate: int,
    channels: int,
    flags: List[str],
    corruptions: List[str],
    stereo_decision: str,
    dc_offset: float,
) -> Dict[str, Any]:
    """Immutable manifest entry for a single source file."""
    return {
        "file": filename,
        "sha256": sha256,
        "duration": round(duration, 3),
        "sample_rate": sample_rate,
        "channels": channels,
        "dc_offset": round(dc_offset, 8),
        "stereo_decision": stereo_decision,
        "flags": flags[:],
        "corruption_flags": corruptions[:],
    }


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def run_stage1(
    src_path: Path,
    canonical_dir: Path,
    cfg: Dict[str, Any],
    dry_run: bool = False,
) -> Optional[Stage1Result]:
    """Run the full Stage 1 pipeline on a single source file.

    Returns Stage1Result on success, or None on idempotent skip (existing
    canonical sidecar with matching SHA256).
    """
    result = Stage1Result(source_path=src_path)
    base = src_path.stem
    can_path = canonical_dir / f"{base}.wav"
    can_sidecar = canonical_dir / f"{base}.json"

    # 1. SHA256
    try:
        result.sha256 = compute_sha256(src_path)
    except Exception as exc:
        result.error = f"sha256 failed: {exc}"
        return result

    # Idempotency check
    if can_sidecar.exists():
        try:
            side = json.loads(can_sidecar.read_text(encoding="utf-8"))
            if side.get("sha256") == result.sha256:
                log.info("Idempotent skip for %s", src_path.name)
                return None
        except Exception:
            pass

    # 2. Load audio (with ffprobe metadata + ffmpeg fallback)
    try:
        y, sr, metadata = load_audio_with_fallback(src_path, target_sr=cfg.get("target_sr", None))
        result.metadata = metadata
        result.sr = sr
    except Exception as exc:
        result.error = f"load failed: {exc}"
        return result

    sf_info = metadata.get("soundfile_info", {})
    original_subtype = sf_info.get("subtype", "")
    declared_frames = sf_info.get("frames")
    actual_frames = len(y) if y.ndim == 1 else y.shape[0]

    # 3. Canonical float32 (already float32 from loader, but ensure)
    y = to_canonical_float32(y, original_subtype)

    # 4. Corruption detection (on first channel if stereo)
    y_for_corrupt = y[:, 0] if y.ndim > 1 else y
    result.corruptions = detect_corruptions(y_for_corrupt, declared_frames, actual_frames, original_subtype)

    # 5. Stereo -> mono
    stereo_dec, y_mono, stereo_flags = stereo_to_mono(y, sr, cfg)
    result.stereo_decision = stereo_dec
    result.flags.extend(stereo_flags)
    if y_mono.ndim > 1:
        y_mono = y_mono[:, 0]

    # 6. DC subtraction + conditional HP
    y_repaired, dc, repair_actions = subtract_dc_and_condition(y_mono, sr, cfg)
    result.dc_offset = dc
    result.repair_actions = repair_actions

    # 7. Additional corruption from stereo handling
    if "stereo_low_correlation" in result.flags or "stereo_anti_phase" in result.flags:
        result.corruptions.append("stereo_problem")

    # 8. Write canonical
    if not dry_run:
        canonical_dir.mkdir(parents=True, exist_ok=True)
        sf.write(str(can_path), y_repaired, sr, subtype="FLOAT")
        # Sidecar
        side = {
            "sha256": result.sha256,
            "pipeline_version": cfg.get("pipeline_version", "1.0.0"),
            "dc_offset": dc,
            "stereo_decision": stereo_dec,
            "corruption_flags": result.corruptions,
            "flags": result.flags,
            "metadata": {
                "sample_rate": sr,
                "channels": 1,
                "duration": round(actual_frames / float(sr), 3),
                "original_subtype": original_subtype,
            },
        }
        can_sidecar.write_text(json.dumps(side, indent=2), encoding="utf-8")
    else:
        log.info("[dry-run] would write canonical %s", can_path)

    result.canonical_path = can_path
    result.sidecar_path = can_sidecar
    result.y_canonical = y_repaired

    # 9. Manifest entry
    result.manifest_entry = build_manifest_entry(
        filename=src_path.name,
        sha256=result.sha256,
        duration=actual_frames / float(sr),
        sample_rate=sr,
        channels=1,
        flags=result.flags,
        corruptions=result.corruptions,
        stereo_decision=stereo_dec,
        dc_offset=dc,
    )

    return result
