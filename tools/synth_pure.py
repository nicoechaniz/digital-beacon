"""Pure-Python additive synthesis of voice → WAV.

Bypasses the digital-beacon Shaper entirely (which had normalization bugs that
caused clipping under rapid voice_on/off transitions). Implements the same
algorithm in numpy:

    For each analysis frame (every 20ms):
      1. Get F0 + per-harmonic magnitudes
      2. Active set = harmonics within 30 dB of H1
      3. Each active voice plays a pure sine at f0·n
      4. Per-voice gain from STFT magnitude (mapped dB → 0..1)
      5. Master sum normalized by 1/sqrt(N_active)
      6. Soft-clip with tanh at output

Output: 16-bit stereo WAV at 44.1 kHz.

Usage:
    python tools/synth_pure.py path/to/voice.wav --out /tmp/synth.wav
"""
from __future__ import annotations

import argparse
import logging
import sys
import wave
from pathlib import Path

import librosa
import numpy as np
import soundfile as sf

log = logging.getLogger("synth_pure")

N_HARMONICS = 32
SAMPLE_RATE = 44100


def analyze(y, sr, f0_min, f0_max):
    hop = int(0.0464 * sr)
    n_fft = 4096
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


def synthesize(y, sr, thresh_db, f0_min, f0_max, max_voices=8,
                noise_floor_db=-50.0, gain_curve="sqrt"):
    """Render the additive synthesis sample-by-sample.

    Parameters
    ----------
    thresh_db : float
        Threshold (dB) below H1 (the fundamental) for considering a
        harmonic active. Default -30. Use a more negative value (-50) to
        allow more harmonics in, less negative (-20) to be stricter.
    max_voices : int
        Cap simultaneous active harmonics.
    noise_floor_db : float
        Absolute noise floor — any harmonic with magnitude below this
        is treated as inactive even if it's above the relative threshold.
        Prevents spectral leakage from formants being treated as harmonics.
    gain_curve : {"linear", "sqrt", "square"}
        How to map dB magnitude → 0..1 gain:
          - "linear": (g_db + |floor|) / |floor|  (default in earlier version)
          - "sqrt":   sqrt of linear — compresses range, harmonics feel
                      more balanced, less dynamic
          - "square": square of linear — expands range, emphasizes
                      strongest harmonics, suppresses weak ones
        Most speech has a steep spectral tilt (-12 dB/oct rolloff above
        F0). To preserve the perceptual hierarchy, "square" usually
        matches best — strong harmonics stay strong, weak ones drop out.
    """
    times, f0, voiced, gains_db = analyze(y, sr, f0_min, f0_max)

    # Smooth the F0 contour with a median filter (kills octave jumps and
    # single-frame spikes) then a small Gaussian blur. Without this, every
    # ~46ms analysis frame can jump 30+ Hz and the harmonic series gets
    # re-tuned rapidly, producing audible beating/whistling artifacts.
    from scipy.signal import medfilt
    f0_smooth = f0.copy()
    voiced_idx = np.where(voiced)[0]
    if len(voiced_idx) >= 5:
        f0_voiced = f0[voiced]
        # Median filter of 5 frames removes outlier spikes (e.g. 247 Hz
        # when the true F0 is ~130). Then light 3-frame Gaussian to
        # smooth pitch micro-jitter without smearing real intonation.
        f0_med = medfilt(f0_voiced, kernel_size=5)
        # Gaussian smooth
        from scipy.ndimage import gaussian_filter1d
        f0_voiced_smooth = gaussian_filter1d(f0_med, sigma=1.5)
        f0_smooth[voiced_idx] = f0_voiced_smooth
    # Hold last F0 forward through unvoiced regions (instead of 0) so the
    # synth continues smoothly through gaps
    last_f0 = 0.0
    for i in range(len(f0_smooth)):
        if voiced[i] and f0_smooth[i] > 0:
            last_f0 = f0_smooth[i]
        elif voiced[i] == False and last_f0 > 0:
            f0_smooth[i] = last_f0  # bridge brief unvoiced gaps
            voiced[i] = True  # mark as voiced for synthesis purposes
    log.info("F0 smoothing applied (median + Gaussian)")
    f0 = f0_smooth
    duration = len(y) / sr
    total_samples = int(np.ceil(duration * SAMPLE_RATE))
    log.info("Rendering %d samples @ %d Hz (%.2f s), curve=%s, thresh=%ddB, "
             "floor=%ddB, max_voices=%d",
             total_samples, SAMPLE_RATE, duration, gain_curve,
             thresh_db, noise_floor_db, max_voices)

    # Per-voice state
    phases = np.zeros(N_HARMONICS + 1)  # index 0 unused, 1..32
    envs = np.zeros(N_HARMONICS + 1)
    last_active = np.zeros(N_HARMONICS + 1, dtype=bool)

    out = np.zeros(total_samples, dtype=np.float64)
    block_size = 256
    floor_abs = abs(noise_floor_db)

    # Pre-compute per-block interpolated F0 and gains to avoid per-sample
    # index lookup. We also interpolate ft WITHIN a block so the harmonic
    # series glides continuously instead of stepping at block boundaries.
    n_blocks = (total_samples + block_size - 1) // block_size
    block_ft = np.zeros(n_blocks + 1)  # +1 for interpolation endpoint
    block_voiced = np.zeros(n_blocks + 1, dtype=bool)
    block_gains = np.zeros((n_blocks + 1, N_HARMONICS), dtype=np.float32)
    for b in range(n_blocks + 1):
        t_b = min(b * block_size / SAMPLE_RATE, duration)
        idx = int(np.searchsorted(times, t_b))
        idx = min(max(idx, 0), len(times) - 1)
        block_voiced[b] = bool(voiced[idx])
        block_ft[b] = float(f0[idx])
        block_gains[b] = gains_db[idx]

    for block_idx in range(n_blocks):
        block_start = block_idx * block_size
        block_end = min(block_start + block_size, total_samples)
        n_samples = block_end - block_start

        # Linear interpolation of F0 across the block (smooths pitch steps
        # between analysis frames — no audible pitch quantization).
        ft_start = block_ft[block_idx]
        ft_end = block_ft[block_idx + 1]
        voiced_start = block_voiced[block_idx]
        voiced_end = block_voiced[block_idx + 1]

        # Active set decision at start of block
        frame_gains = block_gains[block_idx]
        if voiced_start and ft_start > 0:
            ref = frame_gains[0]
            above_relative = frame_gains > (ref + thresh_db)
            above_absolute = frame_gains > noise_floor_db
            active_mask = above_relative & above_absolute
            if active_mask.sum() > max_voices:
                strengths = frame_gains.copy()
                strengths[~active_mask] = -200
                top_idx = np.argpartition(-strengths, max_voices)[:max_voices]
                new_mask = np.zeros(N_HARMONICS, dtype=bool)
                new_mask[top_idx] = True
                active_mask = new_mask
        else:
            active_mask = np.zeros(N_HARMONICS, dtype=bool)

        for s in range(n_samples):
            sample_t = block_start + s
            # Interpolate F0 smoothly within the block
            frac = (s + 0.5) / block_size if block_size > 0 else 0
            ft = ft_start + (ft_end - ft_start) * frac
            mix = 0.0
            for n in range(1, N_HARMONICS + 1):
                target_env = 1.0 if active_mask[n - 1] else 0.0
                if target_env > envs[n]:
                    envs[n] = min(target_env, envs[n] + 1.0 / (0.010 * SAMPLE_RATE))
                else:
                    envs[n] = max(target_env, envs[n] - 1.0 / (0.030 * SAMPLE_RATE))
                if envs[n] <= 0:
                    continue
                g_db = frame_gains[n - 1]
                g_lin = max(0.0, min(1.0, (g_db + floor_abs) / floor_abs))
                if gain_curve == "sqrt":
                    g_norm = np.sqrt(g_lin)
                elif gain_curve == "square":
                    g_norm = g_lin * g_lin
                else:
                    g_norm = g_lin
                phases[n] += 2.0 * np.pi * ft * n / SAMPLE_RATE
                mix += g_norm * envs[n] * np.sin(phases[n])

            n_active = int(np.sum(envs > 0.001))
            norm = 1.0 / np.sqrt(max(n_active, 1))
            out[sample_t] = mix * norm

        if block_idx % 50 == 0:
            active_count = int(active_mask.sum())
            t_b = block_start / SAMPLE_RATE
            log.info("  t=%.2fs voiced=%d f0=%.1f active=%d",
                     t_b, voiced_start, ft_start, active_count)

    # Apply a low-pass filter at max_active_harmonic * max_expected_F0 Hz to
    # remove intermodulation products and aliasing above the natural range
    # of our harmonics. Without this, the sum of N sines produces audible
    # sum/difference components above the highest harmonic (the "whistling"
    # artifact). The cutoff is conservative: max_harmonic * max_F0 + margin.
    max_harmonic = max_voices
    max_f0 = float(np.max(f0_smooth)) if len(f0_smooth) > 0 else 200.0
    cutoff_hz = min(max_harmonic * max_f0 * 1.1, SAMPLE_RATE / 2 - 1000)
    log.info("Applying low-pass filter at %.0f Hz to remove intermodulation",
             cutoff_hz)
    from scipy.signal import butter, filtfilt
    nyq = SAMPLE_RATE / 2
    b, a = butter(4, cutoff_hz / nyq, btype='low')
    out = filtfilt(b, a, out).astype(np.float64)

    log.info("Final mix: peak=%.4f RMS=%.4f",
             float(np.abs(out).max()), float(np.sqrt(np.mean(out**2))))
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wav", type=Path)
    parser.add_argument("--out", type=Path,
                        default=Path.home() / "Music" / "voice-analysis" / "synth_pure.wav")
    parser.add_argument("--thresh-db", type=float, default=-30.0)
    parser.add_argument("--f0-min", type=float, default=70.0)
    parser.add_argument("--f0-max", type=float, default=400.0)
    parser.add_argument("--log", default="INFO")
    parser.add_argument("--max-voices", type=int, default=8,
                        help="Cap simultaneous active harmonics (default 8)")
    parser.add_argument("--noise-floor-db", type=float, default=-50.0,
                        help="Absolute noise floor in dB (default -50)")
    parser.add_argument("--gain-curve", choices=["linear", "sqrt", "square"],
                        default="sqrt",
                        help="dB → 0..1 mapping: linear, sqrt (default), square")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=args.log,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%H:%M:%S",
    )

    y, sr = sf.read(str(args.wav), always_2d=False)
    if y.ndim > 1:
        y = y[:, 0]
    y = y.astype(np.float32)
    log.info("Input: %d samples @ %d Hz (%.2f s)",
             len(y), sr, len(y) / sr)

    out = synthesize(y, sr, args.thresh_db, args.f0_min, args.f0_max,
                     max_voices=args.max_voices,
                     noise_floor_db=args.noise_floor_db,
                     gain_curve=args.gain_curve)

    # Soft-clip + normalize to peak 0.95
    peak = float(np.abs(out).max())
    if peak > 0.95:
        log.warning("Peak %.3f > 0.95 — applying tanh soft-clip", peak)
        out = np.tanh(out) * 0.95
    elif peak > 0:
        out = out * (0.95 / peak)
        log.info("Normalized: peak 0.95 (gain was %.1f dB)",
                 20 * np.log10(0.95 / peak))
    else:
        log.error("Output is silent!")
        sys.exit(1)

    # Write 16-bit stereo WAV (mono → stereo duplicate)
    pcm = (np.clip(out, -1.0, 1.0) * 32767).astype(np.int16)
    pcm_stereo = np.column_stack([pcm, pcm]).reshape(-1)
    with wave.open(str(args.out), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm_stereo.tobytes())
    log.info("Wrote %s (%.1f KB)", args.out, args.out.stat().st_size / 1024)


if __name__ == "__main__":
    sys.exit(main())