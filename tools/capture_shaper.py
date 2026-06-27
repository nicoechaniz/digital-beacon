"""Capture Shaper output directly to a WAV, bypassing PipeWire entirely.

Spawns a complete minimal Shaper stack (store + AudioEngine + OSCReceiver)
in-process, attaches a recorder to the audio callback, runs the voice → OSC
analysis, then writes the captured blocks as a stereo WAV.

No SC, no MIDI, no dashboard, no external Shaper — fully standalone.

Usage:
    python tools/capture_shaper.py path/to/voice.wav --out /tmp/shaper.wav
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
import wave
from pathlib import Path
from typing import List

import librosa
import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from digital_beacon.state import VoiceParameterStore
from digital_beacon.audio_engine import AudioEngine
from digital_beacon.osc_receiver import ShaperOSCReceiver
from digital_beacon import config as cfg

log = logging.getLogger("capture_shaper")

F1_MIN = cfg.F1_MIN
F1_MAX = cfg.F1_MAX
N_HARMONICS = 32


def analyze(y, sr, f0_min, f0_max):
    hop = int(0.0464 * sr)
    n_fft = 4096
    log.info("Running pYIN (f0 ∈ [%.0f, %.0f] Hz, hop=%d, n_fft=%d)",
             f0_min, f0_max, hop, n_fft)
    f0, voiced, _ = librosa.pyin(
        y, fmin=f0_min, fmax=f0_max, sr=sr,
        hop_length=hop, frame_length=n_fft, fill_na=0.0,
    )
    stft = np.abs(librosa.stft(y, n_fft=n_fft, hop_length=hop))
    freqs = librosa.fft_frequencies(sr=sr, n_fft=n_fft)
    times = librosa.frames_to_time(np.arange(len(f0)), sr=sr, hop_length=hop)

    T = len(f0)
    gains_db = np.full((T, N_HARMONICS), -120.0, dtype=np.float32)
    for t in range(T):
        ft = f0[t]
        if not voiced[t] or ft <= 0:
            continue
        target_freqs = np.array([ft * (n + 1) for n in range(N_HARMONICS)])
        for n in range(N_HARMONICS):
            tgt = target_freqs[n]
            if tgt > sr / 2 - 50:
                break
            if tgt <= freqs[0] or tgt >= freqs[-1]:
                continue
            mag = float(np.interp(tgt, freqs, stft[:, t]))
            gains_db[t, n] = 20.0 * np.log10(mag + 1e-12)

    log.info("Analysis: %d frames, %.2f s, %.1f%% voiced",
             T, times[-1] if len(times) else 0, 100.0 * voiced.mean())
    if voiced.any():
        log.info("F0 (voiced): mean=%.1f min=%.1f max=%.1f",
                 float(f0[voiced].mean()),
                 float(f0[voiced].min()),
                 float(f0[voiced].max()))
    return times, f0, voiced, gains_db


def capture(wav_path, out_path, thresh_db, f0_min, f0_max,
            rate_hz, voice_gain):
    log.info("Loading %s", wav_path)
    y, sr = sf.read(str(wav_path), always_2d=False)
    if y.ndim > 1:
        log.info("Stereo input — taking channel 0")
        y = y[:, 0]
    y = y.astype(np.float32)
    duration = len(y) / sr
    log.info("Audio: sr=%d duration=%.2f s", sr, duration)

    times, f0, voiced, gains_db = analyze(y, sr, f0_min, f0_max)
    f0_safe = np.clip(f0, F1_MIN, F1_MAX)

    # ─── Spawn minimal Shaper stack in-process ────────────────────────
    store = VoiceParameterStore()
    store.set_master_gain(1.0)
    audio = AudioEngine(store)
    osc = ShaperOSCReceiver(store)
    sink: List[np.ndarray] = []

    audio.attach_recorder(sink)
    osc.start()       # binds UDP :9001 (NH broadcast) and :9002 (direct)
    audio.start()     # opens sounddevice OutputStream
    log.info("Shaper running locally on :%d and :%d",
             cfg.BEACON_BROADCAST_PORT, cfg.SHAPER_OSC_PORT)

    # ─── Pre-roll: give the OSC server + audio callback a beat ────────
    time.sleep(0.2)
    store.panic()  # make sure we're starting from silence

    prev_active: set[int] = set()
    dt_frame = 1.0 / rate_hz
    n_frames = int(np.ceil(duration * rate_hz))
    log.info("Streaming %d frames at %.0f Hz", n_frames, rate_hz)

    t_start = time.monotonic()
    try:
        for i in range(n_frames):
            t_audio = i * dt_frame
            idx = int(np.searchsorted(times, t_audio))
            idx = min(max(idx, 0), len(times) - 1)

            if idx + 1 < len(times):
                t0, t1 = times[idx], times[idx + 1]
                w = 0.0 if t1 == t0 else (t_audio - t0) / (t1 - t0)
                ft = (1 - w) * f0_safe[idx] + w * f0_safe[idx + 1]
                is_voiced = bool(voiced[idx] or voiced[idx + 1])
                gains_t = (1 - w) * gains_db[idx] + w * gains_db[idx + 1]
            else:
                ft = float(f0_safe[idx])
                is_voiced = bool(voiced[idx])
                gains_t = gains_db[idx]

            store.update_f1(float(ft))

            if is_voiced and ft > 0:
                ref = gains_t[0]
                active_mask = gains_t > (ref + thresh_db)
                active = {n + 1 for n in range(N_HARMONICS) if active_mask[n]}
            else:
                active = set()

            for n in range(1, N_HARMONICS + 1):
                g_db = gains_t[n - 1]
                g_norm = max(0.0, min(1.0, (g_db + 50.0) / 50.0))
                store.set_gain(n, float(g_norm))

            new_active = active - prev_active
            new_inactive = prev_active - active
            for n in new_active:
                freq = ft * n
                store.voice_on(n, n, float(freq), gain=float(voice_gain))
            for n in new_inactive:
                store.voice_off(n)
            prev_active = active

            elapsed = time.monotonic() - t_start
            target = (i + 1) * dt_frame
            sleep_for = target - elapsed
            if sleep_for > 0:
                time.sleep(sleep_for)

        time.sleep(0.3)  # let last blocks drain

    finally:
        store.panic()
        audio.detach_recorder()
        osc.stop()
        audio.stop()
        log.info("Stopped. %d blocks captured.", len(sink))

    # ─── Concatenate captured blocks → WAV ───────────────────────────
    if not sink:
        log.error("No audio captured!")
        sys.exit(1)

    valid = [b for b in sink
             if isinstance(b, np.ndarray) and b.ndim == 2 and b.shape[1] == 2]
    if not valid:
        log.error("No valid 2-channel blocks!")
        sys.exit(1)

    audio_data = np.concatenate(valid, axis=0).astype(np.float32)
    sr_out = cfg.AUDIO_SAMPLE_RATE
    log.info("Captured: %d samples @ %d Hz (%.2f s), peak=%.4f, RMS=%.4f",
             audio_data.shape[0], sr_out,
             audio_data.shape[0] / sr_out,
             float(np.abs(audio_data).max()),
             float(np.sqrt(np.mean(audio_data ** 2))))

    # Soft-clip + normalize to peak 0.95
    peak = float(np.abs(audio_data).max())
    if peak > 0.95:
        log.warning("Peak %.3f > 0.95 — applying soft-clip", peak)
        audio_data = np.tanh(audio_data) * 0.95
    elif peak > 0:
        audio_data = audio_data * (0.95 / peak)
        log.info("Normalized: peak 0.95 (gain was %.1f dB)",
                 20 * np.log10(0.95 / peak))

    pcm = (np.clip(audio_data, -1.0, 1.0) * 32767).astype(np.int16)
    pcm_interleaved = pcm.reshape(-1)
    with wave.open(str(out_path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(sr_out)
        w.writeframes(pcm_interleaved.tobytes())
    log.info("Wrote %s (%.1f KB)", out_path, out_path.stat().st_size / 1024)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wav", type=Path)
    parser.add_argument("--out", type=Path, default=Path("/tmp/shaper_tap.wav"))
    parser.add_argument("--thresh-db", type=float, default=-30.0)
    parser.add_argument("--f0-min", type=float, default=70.0)
    parser.add_argument("--f0-max", type=float, default=400.0)
    parser.add_argument("--rate-hz", type=float, default=50.0)
    parser.add_argument("--voice-gain", type=float, default=1.0)
    parser.add_argument("--log", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=args.log,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    capture(args.wav, args.out, args.thresh_db, args.f0_min, args.f0_max,
            args.rate_hz, args.voice_gain)
    return 0


if __name__ == "__main__":
    sys.exit(main())