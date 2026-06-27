"""Voice → Shaper.

Read a monoaural WAV of spoken voice, extract F0 + per-harmonic magnitudes
with librosa.pyin + STFT, then stream the result to the Shaper via OSC
(/beacon/f1 + /digital/harmonic/<N>/gain + /beacon/voice/on|off) in
real-time playback synchronized to the original audio.

Usage:
    python tools/voice_to_shaper.py path/to/voice.wav [--thresh-db -30]
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

import librosa
import numpy as np
import soundfile as sf
from pythonosc.udp_client import SimpleUDPClient

# Shaper defaults (mirror digital_beacon/config.py)
BEACON_PORT = 9001
SHAPER_PORT = 9002
F1_MIN = 60.0
F1_MAX = 400.0
N_HARMONICS = 32
DEFAULT_VOICE_GAIN = 0.6

log = logging.getLogger("voice_to_shaper")


def analyze(
    y: np.ndarray, sr: int, f0_min: float, f0_max: float
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Return (times, f0, voiced_flag, gains_db) where:
        - times: frame centers in seconds
        - f0: F0 in Hz (0 if unvoiced)
        - voiced_flag: bool per frame
        - gains_db: shape (T, N_HARMONICS), magnitudes at f1·N in dB
    """
    # Frame settings — 46.4 ms hop (gives ~21 fps). n_fft chosen for ~30 Hz
    # freq resolution at low frequencies, which is needed to resolve harmonics
    # at ~70 Hz fundamental (the spacing matters).
    hop_length = int(0.0464 * sr)
    n_fft = 4096

    log.info("Running pYIN (f0 ∈ [%.0f, %.0f] Hz, hop=%d, n_fft=%d)",
             f0_min, f0_max, hop_length, n_fft)

    f0, voiced_flag, voiced_prob = librosa.pyin(
        y,
        fmin=f0_min,
        fmax=f0_max,
        sr=sr,
        hop_length=hop_length,
        frame_length=n_fft,
        fill_na=0.0,
    )

    # Build STFT magnitude. Use the same n_fft / hop so frames line up.
    stft = np.abs(librosa.stft(y, n_fft=n_fft, hop_length=hop_length))
    freqs = librosa.fft_frequencies(sr=sr, n_fft=n_fft)

    # Frame centers in seconds (librosa.frames_to_time respects hop_length)
    times = librosa.frames_to_time(
        np.arange(len(f0)), sr=sr, hop_length=hop_length
    )

    # For each frame: magnitudes at f1·N for N=1..32. If unvoiced, skip.
    T = len(f0)
    gains_db = np.full((T, N_HARMONICS), -120.0, dtype=np.float32)

    for t in range(T):
        ft = f0[t]
        if not voiced_flag[t] or ft <= 0:
            continue
        # Frequencies we want to probe
        target_freqs = np.array([ft * (n + 1) for n in range(N_HARMONICS)])
        # Snap each target to nearest STFT bin, gather magnitude
        # Linear interpolation is more accurate than nearest-bin; do that.
        for n in range(N_HARMONICS):
            tgt = target_freqs[n]
            if tgt > sr / 2 - 50:  # Nyquist guard
                break
            # Interpolate magnitude at tgt from the STFT bins
            if tgt <= freqs[0] or tgt >= freqs[-1]:
                continue
            mag = float(np.interp(tgt, freqs, stft[:, t]))
            gains_db[t, n] = 20.0 * np.log10(mag + 1e-12)

    log.info(
        "Analysis done: %d frames, %.2f s, %.1f%% voiced",
        T, times[-1] if len(times) else 0, 100.0 * voiced_flag.mean(),
    )
    if voiced_flag.any():
        log.info(
            "F0 stats (voiced only): mean=%.1f Hz min=%.1f max=%.1f",
            float(f0[voiced_flag].mean()),
            float(f0[voiced_flag].min()),
            float(f0[voiced_flag].max()),
        )

    return times, f0, voiced_flag, gains_db


def stream_to_shaper(
    wav_path: Path,
    thresh_db: float,
    f0_min: float,
    f0_max: float,
    osc_host: str = "127.0.0.1",
    rate_hz: float = 50.0,
    dry_run: bool = False,
):
    """Load WAV, analyze, then stream OSC messages to the Shaper in
    real-time playback synchronized to the audio."""
    log.info("Loading WAV: %s", wav_path)
    y, sr = sf.read(str(wav_path), always_2d=False)
    if y.ndim > 1:
        log.warning("Stereo input — taking channel 0 only")
        y = y[:, 0]
    duration = len(y) / sr
    log.info("Audio: sr=%d samples=%d duration=%.2f s", sr, len(y), duration)

    times, f0, voiced, gains_db = analyze(y, sr, f0_min, f0_max)

    if dry_run:
        log.info("Dry run — not sending OSC.")
        return

    # Connect to Shaper. /digital/* on 9002, /beacon/* on 9001.
    beacon = SimpleUDPClient(osc_host, BEACON_PORT)
    shaper = SimpleUDPClient(osc_host, SHAPER_PORT)

    # Reset state
    shaper.send_message("/digital/panic", [])
    beacon.send_message("/beacon/panic", [])
    time.sleep(0.05)

    # Sanity: clamp F0 into Shaper's range
    f0_safe = np.clip(f0, F1_MIN, F1_MAX)

    # Frame schedule. We update at `rate_hz`; interpolate everything.
    dt_frame = 1.0 / rate_hz
    n_frames = int(np.ceil(duration * rate_hz))
    log.info("Streaming: %d OSC updates at %.0f Hz (frame dt=%.1f ms)",
             n_frames, rate_hz, 1000.0 * dt_frame)

    # Track which harmonics are currently active so we can send
    # voice_on/voice_off transitions (avoids OSC floods).
    prev_active = set()

    t_start = time.monotonic()
    for i in range(n_frames):
        t_audio = i * dt_frame

        # Find the analysis frame closest to this time
        idx = int(np.searchsorted(times, t_audio))
        idx = min(max(idx, 0), len(times) - 1)

        # Find F0 via simple interp between the two surrounding frames
        if idx + 1 < len(times):
            t0, t1 = times[idx], times[idx + 1]
            w = 0.0 if t1 == t0 else (t_audio - t0) / (t1 - t0)
            ft = (1 - w) * f0_safe[idx] + w * f0_safe[idx + 1]
            is_voiced = voiced[idx] or voiced[idx + 1]
            gains_t = (1 - w) * gains_db[idx] + w * gains_db[idx + 1]
        else:
            ft = float(f0_safe[idx])
            is_voiced = voiced[idx]
            gains_t = gains_db[idx]

        # Update /beacon/f1 (even if unvoiced — Shaper just ignores)
        beacon.send_message("/beacon/f1", float(ft))

        if is_voiced and ft > 0:
            # Active set: harmonics above threshold relative to harmonic 1
            ref = gains_t[0]
            active_mask = gains_t > (ref + thresh_db)
            active = {n + 1 for n in range(N_HARMONICS) if active_mask[n]}
        else:
            active = set()

        # Update per-harmonic gains (cheap to send all 32)
        for n in range(1, N_HARMONICS + 1):
            g_db = gains_t[n - 1]
            # Map dB to 0..1 gain. -60 dB → 0, 0 dB → 1, soft floor at -50 dB.
            g_norm = max(0.0, min(1.0, (g_db + 50.0) / 50.0))
            shaper.send_message(f"/digital/harmonic/{n}/gain", float(g_norm))

        # Transitions: voice_on for newly active, voice_off for newly inactive.
        # Important: only fire voice_on the FIRST time a harmonic becomes active.
        # Re-firing it on every frame resets the attack envelope (click!).
        # We track the set of currently-active harmonics and only emit on
        # transitions.
        new_active = active - prev_active
        new_inactive = prev_active - active
        for n in new_active:
            # voice_id == harmonic_n (Shaper accepts any int)
            freq = ft * n
            # Boost above DEFAULT_VOICE_GAIN to compensate for the Shaper's
            # 1/sqrt(N) per-voice normalization (which makes the synth ~10 dB
            # quieter than the original). 1.0 is "full strength" for one voice;
            # with N active voices the Shaper scales by 1/sqrt(N).
            beacon.send_message(
                "/beacon/voice/on",
                [n, float(freq), 1.0, n, n],
            )
        for n in new_inactive:
            beacon.send_message("/beacon/voice/off", [n])
        prev_active = active

        # Sleep until next frame, but never drift ahead of the audio
        elapsed = time.monotonic() - t_start
        target = (i + 1) * dt_frame
        sleep_for = target - elapsed
        if sleep_for > 0:
            time.sleep(sleep_for)

    # Cleanup: panic after playback
    time.sleep(0.1)
    shaper.send_message("/digital/panic", [])
    beacon.send_message("/beacon/panic", [])
    log.info("Stream complete. Panned.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wav", type=Path, help="Input WAV (mono preferred)")
    parser.add_argument(
        "--thresh-db", type=float, default=-30.0,
        help="dB below harmonic 1 to count a harmonic as active (default -30)",
    )
    parser.add_argument(
        "--f0-min", type=float, default=70.0,
        help="Lower F0 bound for pYIN (Hz). Default 70 (male speech).",
    )
    parser.add_argument(
        "--f0-max", type=float, default=400.0,
        help="Upper F0 bound for pYIN (Hz). Default 400.",
    )
    parser.add_argument(
        "--rate-hz", type=float, default=50.0,
        help="OSC update rate (Hz). Default 50.",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Analyze only, no OSC sent.",
    )
    parser.add_argument(
        "--log", default="INFO", help="Log level (DEBUG/INFO/WARNING).",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=args.log,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%H:%M:%S",
    )

    stream_to_shaper(
        wav_path=args.wav,
        thresh_db=args.thresh_db,
        f0_min=args.f0_min,
        f0_max=args.f0_max,
        rate_hz=args.rate_hz,
        dry_run=args.dry_run,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())