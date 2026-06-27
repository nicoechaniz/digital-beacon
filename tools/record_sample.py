"""Record voice from R24 Ch.2, trim silence, normalize, save to voice dir.

Usage:
    python tools/record_sample.py --name mi_voz_03
    python tools/record_sample.py --name mi_voz_03 --duration 5

The recording starts on Enter (or --auto), captures the R24 Ch.2 mic
input via pw-record, then post-processes:

  1. Extract channel 2 from the 8-channel capture
  2. Trim leading/trailing silence (energy threshold)
  3. Normalize to peak ≈ 0.95 (matches your existing samples)
  4. Save stereo WAV at 44.1 kHz to ~/Music/voice-analysis/<name>.wav
  5. Optionally: re-run the comparison build so the new sample appears
     in the dropdown.
"""
from __future__ import annotations

import argparse
import logging
import shutil
import subprocess
import sys
import time
import wave
from pathlib import Path

import numpy as np
import soundfile as sf

VOICE_DIR = Path.home() / "Music" / "voice-analysis"
R24_SOURCE = "alsa_input.usb-ZOOM_Corporation_R24_0-00.analog-surround-71"
SAMPLE_RATE = 44100
TARGET_PEAK = 0.95
SILENCE_THRESHOLD_DB = -40.0  # energy threshold for trimming silence
TRIM_CHUNK_MS = 50           # chunk size for silence detection

log = logging.getLogger("record_sample")


def record(duration: float, out_path: Path) -> None:
    """Capture 8-channel WAV from R24, save raw to out_path."""
    cmd = [
        "pw-record",
        "--target", R24_SOURCE,
        "--format", "s16",
        "--rate", "48000",
        "--channels", "8",
        str(out_path),
    ]
    log.info("Starting pw-record for %.1fs (Ctrl-C to stop early)...", duration)
    log.info(" ".join(cmd))
    proc = subprocess.Popen(cmd)
    time.sleep(0.5)  # give pw-record a moment to actually start
    try:
        proc.wait(timeout=duration + 2)
    except subprocess.TimeoutExpired:
        proc.terminate()
        proc.wait()


def extract_channel_2(raw_path: Path) -> tuple[np.ndarray, int]:
    """Read 8-channel capture, return channel 2 mono and its sample rate."""
    y, sr = sf.read(str(raw_path))
    if y.ndim != 2 or y.shape[1] != 8:
        raise ValueError(f"Expected 8-channel capture, got shape {y.shape}")
    ch2 = y[:, 1].astype(np.float32)
    log.info("Channel 2 raw: peak=%.4f, RMS=%.4f, duration=%.2fs",
             float(np.abs(ch2).max()), float(np.sqrt(np.mean(ch2**2))),
             len(ch2) / sr)
    return ch2, sr


def trim_silence(y: np.ndarray, sr: int, threshold_db: float,
                 chunk_ms: int = 50) -> np.ndarray:
    """Trim leading and trailing silence below threshold (in dBFS RMS)."""
    chunk = int(sr * chunk_ms / 1000)
    n_chunks = len(y) // chunk
    if n_chunks < 3:
        return y  # too short to trim
    # Per-chunk RMS
    rms = np.array([
        float(np.sqrt(np.mean(y[i*chunk:(i+1)*chunk]**2)))
        for i in range(n_chunks)
    ])
    rms_db = 20.0 * np.log10(rms + 1e-12)
    above = rms_db > threshold_db
    if not above.any():
        log.warning("Whole signal below silence threshold; returning unchanged")
        return y
    first = int(np.argmax(above))
    last = n_chunks - 1 - int(np.argmax(above[::-1]))
    start_sample = first * chunk
    end_sample = (last + 1) * chunk
    log.info("Trim: keeping %.2fs of %.2fs (chunks %d..%d of %d)",
             (end_sample - start_sample) / sr,
             len(y) / sr, first, last, n_chunks)
    return y[start_sample:end_sample]


def normalize(y: np.ndarray, target_peak: float) -> np.ndarray:
    """Scale so peak = target_peak (preserves waveform shape)."""
    peak = float(np.abs(y).max())
    if peak < 1e-6:
        log.warning("Signal is silent; cannot normalize")
        return y
    g = target_peak / peak
    log.info("Normalize: peak %.4f → %.4f (gain %.1f dB)",
             peak, target_peak, 20 * np.log10(g))
    return y * g


def save_stereo(y: np.ndarray, sr: int, out_path: Path) -> None:
    """Save as stereo 16-bit WAV (mono duplicated to L=R)."""
    if sr != SAMPLE_RATE:
        log.info("Resampling %d → %d Hz", sr, SAMPLE_RATE)
        # Lightweight linear resample (mono → mono is fine for this case)
        n_dst = int(round(len(y) * SAMPLE_RATE / sr))
        x_src = np.linspace(0, len(y) - 1, n_dst)
        x0 = np.floor(x_src).astype(np.int64)
        x1 = np.clip(x0 + 1, 0, len(y) - 1)
        frac = (x_src - x0).astype(np.float32)
        y = ((1.0 - frac) * y[x0] + frac * y[x1]).astype(np.float32)
        sr = SAMPLE_RATE
    stereo = np.column_stack([y, y])
    pcm = (np.clip(stereo, -1.0, 1.0) * 32767).astype(np.int16)
    pcm_int = pcm.reshape(-1)
    with wave.open(str(out_path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm_int.tobytes())
    log.info("Wrote %s (%d KB)", out_path, out_path.stat().st_size // 1024)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", type=str, required=True,
                        help="Output filename (without .wav) in voice dir")
    parser.add_argument("--duration", type=float, default=10.0,
                        help="Max recording duration in seconds (default 10)")
    parser.add_argument("--threshold-db", type=float, default=SILENCE_THRESHOLD_DB,
                        help="Silence trim threshold in dBFS (default -40)")
    parser.add_argument("--target-peak", type=float, default=TARGET_PEAK,
                        help="Normalize to this peak (default 0.95)")
    parser.add_argument("--auto", action="store_true",
                        help="Skip Enter prompt, start recording immediately")
    parser.add_argument("--rebuild", action="store_true",
                        help="Re-run the comparison build after recording")
    parser.add_argument("--log", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=args.log,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%H:%M:%S",
    )

    out_path = VOICE_DIR / f"{args.name}.wav"
    if out_path.exists():
        log.error("File already exists: %s", out_path)
        sys.exit(1)

    if not args.auto:
        log.info("Will record to %s for up to %.1fs.", out_path, args.duration)
        log.info("Press Enter to start (or Ctrl-C to abort)...")
        try:
            input()
        except KeyboardInterrupt:
            log.info("Aborted.")
            sys.exit(1)

    raw_path = Path("/tmp/record_capture.wav")
    if raw_path.exists():
        raw_path.unlink()
    record(args.duration, raw_path)
    if not raw_path.exists():
        log.error("Recording failed; no output file produced")
        sys.exit(1)

    log.info("Post-processing...")
    y, sr = extract_channel_2(raw_path)
    y = trim_silence(y, sr, args.threshold_db)
    y = normalize(y, args.target_peak)
    save_stereo(y, sr, out_path)

    # Cleanup
    raw_path.unlink(missing_ok=True)

    log.info("Done. Sample saved to: %s", out_path)
    log.info("Run build_voice_compare_v3.py to add this to the dashboard.")

    if args.rebuild:
        log.info("Rebuilding dashboard...")
        subprocess.run(
            [sys.executable, str(Path(__file__).resolve().parent
                                 / "build_voice_compare_v3.py")],
            check=False,
        )


if __name__ == "__main__":
    sys.exit(main())