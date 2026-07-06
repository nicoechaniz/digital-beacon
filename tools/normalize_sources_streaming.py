#!/usr/bin/env python3
"""Streaming version of normalize_sources — processes long files in 30s blocks.

Key differences from normalize_sources.py:
- Two-pass: Pass 1 accumulates metrics, Pass 2 applies gain and writes.
- Memory usage is O(blocksize) not O(filesize).
- All metrics are accumulated via streaming algorithms.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import re
import subprocess
import sys
import time
import wave
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import soundfile as sf

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
log = logging.getLogger("normalize_sources_streaming")

# ---------------------------------------------------------------------------
# Optional deps
# ---------------------------------------------------------------------------
try:
    import pyloudnorm as pyln
except ImportError:
    pyln = None

try:
    import webrtcvad
except ImportError:
    webrtcvad = None

try:
    import scipy.signal as sp_signal
except ImportError:
    sp_signal = None

try:
    import librosa
except ImportError:
    librosa = None

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BLOCK_SIZE_S = 30  # seconds per block
PIPELINE_VERSION = "2.0-streaming"

@dataclass
class FileMetrics:
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

# ---------------------------------------------------------------------------
# FFmpeg loudness (zero Python memory)
# ---------------------------------------------------------------------------

def run_ffmpeg_loudness(path: Path) -> Tuple[float, float, float]:
    """Return (integrated_lufs, lra, true_peak) via ffmpeg loudnorm.

    Uses two-pass analysis: first pass measures, second pass reports.
    Memory usage: O(1) — ffmpeg streams the file.
    """
    cmd = [
        "ffmpeg", "-hide_banner", "-nostats", "-y",
        "-i", str(path),
        "-af", "loudnorm=print_format=json",
        "-f", "null", "-"
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        stderr = proc.stderr
        # Parse JSON from loudnorm output
        m = re.search(r"\{[^}]*\"input_i\"[^}]*\}", stderr, re.DOTALL)
        if not m:
            return -70.0, 0.0, -120.0
        data = json.loads(m.group())
        il = float(data.get("input_i", -70.0))
        lra = float(data.get("input_lra", 0.0))
        tp = float(data.get("input_tp", -120.0))
        return il, lra, tp
    except Exception as exc:
        log.warning("ffmpeg loudness failed for %s: %s", path.name, exc)
        return -70.0, 0.0, -120.0

# ---------------------------------------------------------------------------
# VAD (streaming)
# ---------------------------------------------------------------------------

def vad_block(y: np.ndarray, sr: int, thresh_db: float = -45.0) -> Tuple[bool, int]:
    """Return (is_voiced, voiced_samples) using energy-based VAD.

    Splits block into 20ms frames, marks voiced if RMS > threshold.
    voiced_samples is approximate (voiced frames * frame_len).
    """
    frame_ms = 20
    frame_len = max(1, int(sr * frame_ms / 1000))
    n_frames = len(y) // frame_len
    if n_frames == 0:
        return True, len(y)

    thresh_lin = 10.0 ** (thresh_db / 20.0)
    voiced_count = 0

    for i in range(n_frames):
        start = i * frame_len
        end = start + frame_len
        frame = y[start:end]
        rms = float(np.sqrt(np.mean(frame.astype(np.float64) ** 2)))
        if rms > thresh_lin:
            voiced_count += 1

    is_voiced = voiced_count > n_frames / 2
    voiced_samples = min(voiced_count * frame_len, len(y))
    return is_voiced, voiced_samples

# ---------------------------------------------------------------------------
# Pass 1: accumulate metrics
# ---------------------------------------------------------------------------

def pass1_accumulate(path: Path, sr: int) -> Tuple[FileMetrics, float, np.ndarray, List[str]]:
    """Two-pass Pass 1: read blocks, accumulate metrics.

    Returns: (metrics, duration_s, frame_mask, flags)
    """
    blocksize = int(sr * BLOCK_SIZE_S)

    # Streaming accumulators
    total_samples = 0
    voiced_samples = 0
    voiced_energy = 0.0
    noise_energy = 0.0
    dc_sum = 0.0
    clip_count = 0
    voiced_clip_count = 0
    peak_max = 0.0
    tp_max = 0.0
    voiced_dur = 0.0
    noise_dur = 0.0
    flags: List[str] = []

    # Frame exclusion mask (frame-level, not sample-level)
    frame_masks = []

    # Read file info
    info = sf.info(str(path))
    duration_s = info.duration
    total_frames = info.frames

    # Pass 1a: ffmpeg loudness (zero memory)
    full_lufs, lra, tp_ffmpeg = run_ffmpeg_loudness(path)

    # Pass 1b: streaming blocks for VAD, energies, clips, etc.
    for block in sf.blocks(str(path), blocksize=blocksize, dtype="float32", always_2d=False):
        if block.ndim > 1:
            block = np.mean(block, axis=1).astype(np.float32, copy=False)
        n = len(block)
        if n == 0:
            continue

        total_samples += n
        dc_sum += float(np.sum(block))

        # Peak & true peak
        block_peak = float(np.max(np.abs(block)))
        if block_peak > peak_max:
            peak_max = block_peak
        # True peak via 4x oversampling (per block)
        if sp_signal is not None:
            try:
                up = sp_signal.resample_poly(block.astype(np.float64), up=4, down=1)
                tp_block = float(np.max(np.abs(up)))
                if tp_block > tp_max:
                    tp_max = tp_block
            except Exception:
                pass

        # VAD
        is_voiced, block_voiced_samples = vad_block(block, sr)
        block_dur = n / sr

        if is_voiced:
            voiced_dur += block_dur * (block_voiced_samples / n)
            voiced_energy += float(np.sum(block.astype(np.float64) ** 2)) * (block_voiced_samples / n)
            voiced_clip_count += int(np.sum(np.abs(block) >= 0.999))
            voiced_samples += block_voiced_samples
            noise_dur += block_dur * ((n - block_voiced_samples) / n)
            noise_energy += float(np.sum(block.astype(np.float64) ** 2)) * ((n - block_voiced_samples) / n)
        else:
            noise_dur += block_dur
            noise_energy += float(np.sum(block.astype(np.float64) ** 2))
            voiced_dur += 0.0

        clip_count += int(np.sum(np.abs(block) >= 0.999))

        # Frame exclusion mask (sibilant detection per block)
        if librosa is not None and n >= 512:
            try:
                cent = librosa.feature.spectral_centroid(
                    y=block.astype(np.float32), sr=sr, n_fft=512, hop_length=256, center=False
                )[0]
                zcr = librosa.feature.zero_crossing_rate(
                    block, frame_length=512, hop_length=256, center=False
                )[0]
                S = np.abs(librosa.stft(block.astype(np.float32), n_fft=512, hop_length=256, center=False))
                freqs = librosa.fft_frequencies(sr=sr, n_fft=512)
                hf_idx = freqs > 4000
                tot = np.sum(S * S, axis=0) + 1e-12
                hf = np.sum(S[hf_idx, :] * S[hf_idx, :], axis=0) if np.any(hf_idx) else np.zeros_like(tot)
                hf_ratio = hf / tot
                sibilant = (cent > 3200) & (zcr > 0.09) & (hf_ratio > 0.18)
                frame_masks.append(sibilant)
            except Exception:
                pass

    dc_offset = dc_sum / total_samples if total_samples > 0 else 0.0

    # Speech LUFS: use ffmpeg result or estimate from voiced energy
    speech_lufs = full_lufs  # ffmpeg already gives speech-active if we used VAD, but it gives full
    # Better: estimate speech LUFS from voiced energy ratio
    if voiced_energy > 0 and noise_energy > 0:
        # Simplistic: speech LUFS = full_lufs + 10*log10(voiced_ratio)
        # But more accurate: use the energy ratio
        voiced_ratio = voiced_samples / total_samples if total_samples > 0 else 0
        if voiced_ratio > 0:
            speech_lufs = full_lufs + 10 * math.log10(voiced_ratio)

    # SNR
    if noise_energy > 0 and voiced_energy > 0:
        snr = 10 * math.log10(voiced_energy / noise_energy)
        noise_floor = 10 * math.log10(noise_energy / total_samples) if total_samples > 0 else -120
    else:
        snr = 0.0
        noise_floor = -120.0

    # Clipping
    if voiced_samples > 0:
        clip_pct = 100.0 * voiced_clip_count / voiced_samples
    else:
        clip_pct = 100.0 * clip_count / total_samples if total_samples > 0 else 0.0

    # True peak from ffmpeg if available, else from block max
    true_peak = tp_ffmpeg if tp_ffmpeg > -100 else (20 * math.log10(max(tp_max, 1e-12)))

    # Speech ratio
    speech_ratio = voiced_dur / duration_s if duration_s > 0 else 0.0

    # Frame mask (concatenate all block masks)
    if frame_masks:
        frame_mask = np.concatenate(frame_masks)
    else:
        frame_mask = np.array([], dtype=bool)

    # Bandwidth & reverb: sample first 60s of voiced audio
    bw = 0.0
    reverb = 0.0
    voiced_sample_limit = int(60 * sr)
    if voiced_samples > 0:
        # Read first 60s of voiced audio (approximate: first voiced blocks)
        y_voiced_sample = np.array([], dtype=np.float32)
        for block in sf.blocks(str(path), blocksize=blocksize, dtype="float32", always_2d=False):
            if len(y_voiced_sample) >= voiced_sample_limit:
                break
            if block.ndim > 1:
                block = np.mean(block, axis=1).astype(np.float32, copy=False)
            is_voiced, block_voiced_samples = vad_block(block, sr)
            if is_voiced and block_voiced_samples > 0:
                y_voiced_sample = np.concatenate([y_voiced_sample, block[:min(block_voiced_samples, voiced_sample_limit - len(y_voiced_sample))]])
        
        if len(y_voiced_sample) >= 256:
            # Bandwidth
            try:
                n_fft = min(4096, 2 ** int(np.floor(np.log2(len(y_voiced_sample)))))
                if n_fft >= 256:
                    spec = np.fft.rfft(y_voiced_sample[:n_fft])
                    power = (np.abs(spec) ** 2).astype(np.float64)
                    freqs = np.fft.rfftfreq(n_fft, 1.0 / sr)
                    peak = np.max(power)
                    thresh = peak * (10.0 ** (-40.0 / 10.0))
                    above = power > thresh
                    if np.any(above):
                        bw = float(freqs[np.where(above)[0][-1]])
            except Exception:
                pass
            
            # Reverb proxy C50
            try:
                hop = int(sr * 0.01)
                if hop > 0:
                    n = len(y_voiced_sample)
                    num_env = (n + hop - 1) // hop
                    pad = np.zeros(num_env * hop, dtype=np.float32)
                    pad[:n] = y_voiced_sample ** 2
                    env = np.mean(pad.reshape(num_env, hop), axis=1)
                    if len(env) > 10:
                        early_n = max(1, int(0.05 / 0.01))
                        ee = float(np.sum(env[:early_n]))
                        le = float(np.sum(env[early_n:]))
                        if le > 0:
                            reverb = 10.0 * np.log10(ee / le)
            except Exception:
                pass

    metrics = FileMetrics(
        full_lufs=round(full_lufs, 3),
        speech_lufs=round(speech_lufs, 3),
        lra=round(lra, 3),
        true_peak_dbtp=round(true_peak, 3),
        dc_offset=round(dc_offset, 8),
        clipping_pct_voiced=round(clip_pct, 4),
        speech_ratio=round(speech_ratio, 5),
        snr_db=round(snr, 2),
        noise_floor_db=round(noise_floor, 2),
        bandwidth_hz=round(bw, 1),
        reverb_proxy=round(reverb, 4),
        voiced_duration_s=round(voiced_dur, 3),
        duration_s=round(duration_s, 3),
    )

    return metrics, duration_s, frame_mask, flags

# ---------------------------------------------------------------------------
# Pass 2: apply gain and write
# ---------------------------------------------------------------------------

def pass2_write(path: Path, sr: int, gain_db: float, out_path: Path) -> None:
    """Read blocks, apply gain, clip, write to output."""
    blocksize = int(sr * BLOCK_SIZE_S)
    gain_lin = 10.0 ** (gain_db / 20.0)

    with sf.SoundFile(str(out_path), "w", samplerate=sr, channels=1, subtype="FLOAT") as out_f:
        for block in sf.blocks(str(path), blocksize=blocksize, dtype="float32", always_2d=False):
            if block.ndim > 1:
                block = np.mean(block, axis=1).astype(np.float32, copy=False)
            # Apply gain
            block_gained = block * gain_lin
            # Clip
            block_gained = np.clip(block_gained, -1.0, 1.0)
            out_f.write(block_gained)

# ---------------------------------------------------------------------------
# Main process
# ---------------------------------------------------------------------------

def process_one_file_streaming(src_path: Path, out_dir: Path, target_lufs: float = -23.0, peak_ceiling: float = -3.0) -> dict:
    """Process one file with streaming two-pass."""
    t0 = time.time()
    info = sf.info(str(src_path))
    sr = info.samplerate

    # Pass 1
    t1 = time.time()
    metrics, duration_s, frame_mask, flags = pass1_accumulate(src_path, sr)
    log.info("Pass 1 done in %.1fs: %s", time.time() - t1, src_path.name)

    # Compute gain
    gain_db = min(target_lufs - metrics.speech_lufs, peak_ceiling - metrics.true_peak_dbtp)
    if gain_db > 18.0:
        flags.extend(["very_quiet", "SNR-suspect"])

    # Pass 2
    out_path = out_dir / src_path.name
    out_dir.mkdir(parents=True, exist_ok=True)
    t2 = time.time()
    pass2_write(src_path, sr, gain_db, out_path)
    log.info("Pass 2 done in %.1fs: %s", time.time() - t2, src_path.name)

    return {
        "filename": src_path.name,
        "gain_db": round(gain_db, 4),
        "metrics": asdict(metrics),
        "flags": flags,
        "duration_s": duration_s,
        "elapsed_s": round(time.time() - t0, 3),
    }

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Streaming normalization pipeline")
    ap.add_argument("-i", "--input-dir", required=True, type=Path)
    ap.add_argument("-o", "--output-dir", required=True, type=Path)
    ap.add_argument("--target-lufs", type=float, default=-23.0)
    ap.add_argument("--peak-ceiling", type=float, default=-3.0)
    ap.add_argument("--log", choices=["DEBUG", "INFO", "WARNING"], default="INFO")
    args = ap.parse_args()

    logging.basicConfig(level=getattr(logging, args.log), format="%(levelname)s %(name)s %(message)s")

    sources = sorted(args.input_dir.rglob("*.wav"))
    log.info("Found %d files", len(sources))

    results = []
    for src in sources:
        try:
            res = process_one_file_streaming(src, args.output_dir, args.target_lufs, args.peak_ceiling)
            results.append(res)
            log.info("OK: %s gain=%.2f flags=%s", res["filename"], res["gain_db"], res["flags"])
        except Exception as exc:
            log.error("FAILED %s: %s", src.name, exc)
            results.append({"filename": src.name, "error": str(exc)})

    # Summary
    summary_path = args.output_dir / "summary.json"
    summary_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    log.info("Summary written to %s", summary_path)

if __name__ == "__main__":
    main()
