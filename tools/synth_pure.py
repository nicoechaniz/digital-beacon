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

The pipeline is split into two phases:

  prepare_analysis(y, sr, ...) -> dict
    Cache F0, voiced mask, per-harmonic gain estimates (already smoothed and
    bridged), STFT raw magnitudes, and frequency grid. All the expensive
    librosa work happens here, ONCE.

  synthesize_prepared(prepared, ...) -> np.ndarray
    Re-render audio from the cached dict. Cheap — no librosa calls.
    Supports per-harmonic gain overrides (per_harmonic_gains) and per-harmonic
    waveform overrides (wave_shapes).

  synthesize(y, sr, ...) -> np.ndarray
    Thin wrapper: prepare_analysis() then synthesize_prepared() with default
    parameters. Same signature as before — all existing callers continue to
    work unchanged.

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
    """Legacy analyze() — returns (times, f0, voiced, gains_db).

    Kept for callers that import it directly (e.g. build_voice_compare_v3.py).
    For new code prefer prepare_analysis() which returns the full dict the
    synthesizer consumes (including STFT + frequency grid).
    """
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


def prepare_analysis(y, sr, f0_min=70.0, f0_max=400.0) -> dict:
    """Phase 1: run the expensive analysis once, return a cached dict.

    Returns a dict with keys:
        times       : (T,) array of frame centers in seconds
        f0          : (T,) array of (smoothed, bridged) fundamental in Hz
        voiced      : (T,) bool array — True where the frame is treated as voiced
        gains_db    : (T, N_HARMONICS) float32 — dB magnitude per harmonic,
                      recomputed for bridged frames so no silent gaps
        sr          : int — sample rate the analysis ran at
        duration    : float — length of input in seconds
        stft_raw    : (n_freqs, T) float — |STFT| for any later re-analysis
        freqs_stft  : (n_freqs,) float — frequency axis matching stft_raw

    The dict is self-contained — synthesize_prepared() takes only this dict
    plus optional rendering parameters. No further librosa calls needed.
    """
    hop = int(0.0464 * sr)
    n_fft = 4096

    # Raw librosa analysis
    f0, voiced, _ = librosa.pyin(
        y, fmin=f0_min, fmax=f0_max, sr=sr,
        hop_length=hop, frame_length=n_fft, fill_na=0.0,
    )
    stft_raw = np.abs(librosa.stft(y, n_fft=n_fft, hop_length=hop))
    freqs_stft = librosa.fft_frequencies(sr=sr, n_fft=n_fft)
    times = librosa.frames_to_time(np.arange(len(f0)), sr=sr, hop_length=hop)

    # Per-harmonic gain table (in dB), -120 for unvoiced frames
    T = len(f0)
    gains_db = np.full((T, N_HARMONICS), -120.0, dtype=np.float32)
    for t in range(T):
        ft = f0[t]
        if not voiced[t] or ft <= 0:
            continue
        for n in range(N_HARMONICS):
            tgt = ft * (n + 1)
            if tgt > sr / 2 - 50:
                break
            if tgt <= freqs_stft[0] or tgt >= freqs_stft[-1]:
                continue
            mag = float(np.interp(tgt, freqs_stft, stft_raw[:, t]))
            gains_db[t, n] = 20.0 * np.log10(mag + 1e-12)

    log.info("Analysis: %d frames, %.2f s, %.1f%% voiced",
             T, times[-1] if len(times) else 0, 100.0 * voiced.mean())
    if voiced.any():
        log.info("F0 (raw): mean=%.1f min=%.1f max=%.1f",
                 float(f0[voiced].mean()),
                 float(f0[voiced].min()),
                 float(f0[voiced].max()))

    # --- F0 smoothing: median + Gaussian + unvoiced bridging ---
    # Median filter of 5 frames removes outlier spikes (e.g. 247 Hz when the
    # true F0 is ~130). Then light 3-frame Gaussian to smooth pitch micro-
    # jitter without smearing real intonation. Without this, every ~46 ms
    # analysis frame can jump 30+ Hz and the harmonic series gets re-tuned
    # rapidly, producing audible beating/whistling artifacts.
    from scipy.signal import medfilt
    from scipy.ndimage import gaussian_filter1d
    f0_smooth = f0.copy()
    voiced_idx = np.where(voiced)[0]
    if len(voiced_idx) >= 5:
        f0_voiced = f0[voiced]
        f0_med = medfilt(f0_voiced, kernel_size=5)
        f0_voiced_smooth = gaussian_filter1d(f0_med, sigma=1.5)
        f0_smooth[voiced_idx] = f0_voiced_smooth

    # Hold last F0 forward through unvoiced regions (instead of 0) so the
    # synth continues smoothly through gaps.
    bridged = set()  # frames that changed from unvoiced→voiced
    last_f0 = 0.0
    for i in range(len(f0_smooth)):
        if voiced[i] and f0_smooth[i] > 0:
            last_f0 = f0_smooth[i]
        elif voiced[i] == False and last_f0 > 0:
            f0_smooth[i] = last_f0  # bridge brief unvoiced gaps
            voiced[i] = True  # mark as voiced for synthesis purposes
            bridged.add(i)

    # Recompute harmonic gains for bridged frames — their original gains_db
    # is all -120 dB (analyze() skips unvoiced frames), which would produce
    # silence despite the bridged F0.
    if bridged:
        log.info("Recomputing harmonic gains for %d bridged frames", len(bridged))
        for t in bridged:
            ft = f0_smooth[t]
            if ft <= 0:
                continue
            for n in range(N_HARMONICS):
                tgt = ft * (n + 1)
                if tgt > sr / 2 - 50:
                    break
                if tgt <= freqs_stft[0] or tgt >= freqs_stft[-1]:
                    continue
                mag = float(np.interp(tgt, freqs_stft, stft_raw[:, t]))
                gains_db[t, n] = 20.0 * np.log10(mag + 1e-12)
    log.info("F0 smoothing applied (median + Gaussian + bridging)")

    duration = float(len(y) / sr)

    return {
        "times": times,
        "f0": f0_smooth,           # post-smoothing, post-bridging
        "voiced": voiced,          # True after bridging for bridged frames
        "gains_db": gains_db,
        "sr": int(sr),
        "duration": duration,
        "stft_raw": stft_raw,
        "freqs_stft": freqs_stft,
    }


def _waveform_value(shape: str, phase: float) -> float:
    """Evaluate one sample of a waveform at the given phase (radians).

    sine (default): np.sin(phase)
    square: sign(sin(phase))
    saw:    2*(phase/(2π) mod 1) - 1
    triangle: 2*abs(2*(phase/(2π) mod 1) - 1) - 1

    The non-sine shapes are generated via phase accumulation (no lookup table),
    which keeps phases continuous across samples — the synth already maintains
    one phase per harmonic from sample to sample.
    """
    if shape == "sine" or shape is None:
        return float(np.sin(phase))
    if shape == "square":
        return float(np.sign(np.sin(phase)))
    if shape == "saw":
        frac = (phase / (2.0 * np.pi)) % 1.0
        return float(2.0 * frac - 1.0)
    if shape == "triangle":
        frac = (phase / (2.0 * np.pi)) % 1.0
        return float(2.0 * abs(2.0 * frac - 1.0) - 1.0)
    raise ValueError(f"unknown wave shape {shape!r}; expected sine/square/saw/triangle")


def synthesize_prepared(prepared: dict,
                        thresh_db: float = -30.0,
                        noise_floor_db: float = -40.0,
                        max_voices: int = 6,
                        gain_curve: str = "sqrt",
                        spectral_tilt_db: float = -12.0,
                        per_harmonic_gains: dict | None = None,
                        wave_shapes: dict | None = None) -> np.ndarray:
    """Phase 2: render audio from a cached analysis dict.

    Parameters
    ----------
    prepared : dict
        Output of prepare_analysis(). MUST contain at minimum the keys
        returned by that function; the renderer does NOT call analyze() or
        prepare_analysis() again.
    thresh_db : float
        Threshold (dB) below H1 for considering a harmonic active.
        Default -30.
    noise_floor_db : float
        Absolute noise floor — any harmonic with magnitude below this is
        treated as inactive even if it's above the relative threshold.
        Prevents spectral leakage from formants being treated as harmonics.
        NOTE: the default in this function is -40.0 (matching the existing
        build_voice_compare_v3.py workflow), while the legacy synthesize()
        default was -50.0; that mismatch is preserved via synthesize()'s
        explicit noise_floor_db=-50.0 pass-through.
    max_voices : int
        Cap simultaneous active harmonics. Default 6 (was 8 in legacy
        synthesize(); build_voice_compare_v3 uses 6).
    gain_curve : {"linear", "sqrt", "square"}
        How to map dB magnitude → 0..1 gain:
          - "linear": (g_db + |floor|) / |floor|
          - "sqrt":   sqrt of linear — compresses range
          - "square": square of linear — expands range
    spectral_tilt_db : float
        Spectral tilt in dB per octave applied to harmonic gains.
        Default -12.0 matches the natural glottal source roll-off
        (Titze 2015: -10 to -15 dB/oct for normal voice).
        0 disables tilt (flat).
    per_harmonic_gains : dict[int, float], optional
        Multiplicative gain applied to each harmonic AFTER STFT-derived
        dB→linear conversion and BEFORE spectral tilt. Keys are 1-based
        harmonic numbers: {1: 1.0, 2: 0.8, 3: 0.6, ...}.
        Unspecified harmonics default to 1.0.
    wave_shapes : dict[int, str], optional
        Per-harmonic waveform override. Keys are 1-based harmonic numbers,
        values are one of: "sine", "square", "saw", "triangle".
        Unspecified harmonics default to "sine".

    Returns
    -------
    np.ndarray, shape (total_samples,), dtype float64, range typically
    within [-1, 1] but NOT normalized — caller's responsibility to peak-
    normalize / soft-clip before writing 16-bit WAV.
    """
    # Sanity check — make sure we got a real prepared dict and not raw audio.
    if not isinstance(prepared, dict):
        raise ValueError(
            f"prepared must be a dict from prepare_analysis(); got {type(prepared).__name__}"
        )
    required = {"times", "f0", "voiced", "gains_db", "sr", "duration"}
    missing = required - set(prepared.keys())
    if missing:
        raise ValueError(
            f"prepared dict missing keys {missing}; "
            "did you pass raw audio instead of prepare_analysis() output?"
        )

    times = prepared["times"]
    f0 = prepared["f0"]
    voiced = prepared["voiced"]
    gains_db = prepared["gains_db"]
    sr = prepared["sr"]
    duration = prepared["duration"]

    # Pre-compute per-harmonic spectral tilt gains.
    # Natural voice has -10 to -15 dB/oct roll-off (Titze 2015).
    # We apply this as a gentle multiplicative gain per harmonic so higher
    # harmonics decay naturally rather than being cut off abruptly by a LPF.
    if spectral_tilt_db != 0.0:
        tilt_gains = np.ones(N_HARMONICS, dtype=np.float64)
        for n in range(1, N_HARMONICS + 1):
            octaves = np.log2(max(n, 1))
            tilt_gains[n - 1] = 10.0 ** (spectral_tilt_db * octaves / 20.0)
        log.info("Spectral tilt: %.1f dB/oct (H1=%.3f, H2=%.3f, H4=%.3f)",
                 spectral_tilt_db,
                 tilt_gains[0], tilt_gains[1], tilt_gains[3])
    else:
        tilt_gains = None
        log.info("Spectral tilt: flat (0 dB/oct)")

    # Per-harmonic gain overrides: 1-based index → multiplier.
    # Default 1.0 means "no change". Applied AFTER dB→linear and BEFORE tilt.
    harm_gains = np.ones(N_HARMONICS, dtype=np.float64)
    if per_harmonic_gains:
        for k, g in per_harmonic_gains.items():
            if not (1 <= k <= N_HARMONICS):
                raise ValueError(
                    f"per_harmonic_gains key {k} out of range 1..{N_HARMONICS}"
                )
            harm_gains[k - 1] = float(g)
        active_overrides = {k: v for k, v in per_harmonic_gains.items() if v != 1.0}
        if active_overrides:
            log.info("per_harmonic_gains overrides: %s", active_overrides)

    # Per-harmonic waveform overrides: 1-based index → shape name.
    # Default 'sine'. Each call to _waveform_value evaluates one sample at
    # the harmonic's current phase — phases are kept continuous across
    # samples, so non-sine shapes stay band-limited (no aliasing from a
    # piecewise construction at a fixed sample rate).
    shape_map: dict[int, str] = {}
    if wave_shapes:
        valid = {"sine", "square", "saw", "triangle"}
        for k, s in wave_shapes.items():
            if not (1 <= k <= N_HARMONICS):
                raise ValueError(
                    f"wave_shapes key {k} out of range 1..{N_HARMONICS}"
                )
            if s not in valid:
                raise ValueError(
                    f"wave_shapes[{k}] = {s!r} not in {valid}"
                )
            shape_map[k] = s
        if any(v != "sine" for v in shape_map.values()):
            log.info("wave_shapes overrides: %s", shape_map)

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
                # Per-harmonic gain override (between dB→linear and tilt).
                g_norm *= harm_gains[n - 1]
                # Apply spectral tilt (natural harmonic decay).
                if tilt_gains is not None:
                    g_norm *= tilt_gains[n - 1]
                phases[n] += 2.0 * np.pi * ft * n / SAMPLE_RATE
                # Per-harmonic waveform selection (default sine).
                shape = shape_map.get(n, "sine")
                wave_val = _waveform_value(shape, phases[n])
                mix += g_norm * envs[n] * wave_val

            n_active = int(np.sum(envs > 0.001))
            norm = 1.0 / np.sqrt(max(n_active, 1))
            out[sample_t] = mix * norm

        if block_idx % 50 == 0:
            active_count = int(active_mask.sum())
            t_b = block_start / SAMPLE_RATE
            log.info("  t=%.2fs voiced=%d f0=%.1f active=%d",
                     t_b, voiced_start, ft_start, active_count)

    log.info("Final mix: peak=%.4f RMS=%.4f",
             float(np.abs(out).max()), float(np.sqrt(np.mean(out**2))))
    return out


def synthesize(y, sr, thresh_db, f0_min, f0_max, max_voices=8,
                noise_floor_db=-50.0, gain_curve="sqrt",
                spectral_tilt_db=-12.0):
    """Render the additive synthesis sample-by-sample.

    Backwards-compatible wrapper: runs prepare_analysis() then
    synthesize_prepared() with default parameters. Every existing caller
    (build_voice_compare_v3.py, the CLI entry point) continues to work
    unchanged.

    For re-rendering the SAME audio with different synth parameters
    (waveform, per-harmonic gain tweaks, etc.), call prepare_analysis() once
    then synthesize_prepared() multiple times — saves ~95% of the cost on
    the second+ passes.
    """
    prepared = prepare_analysis(y, sr, f0_min=f0_min, f0_max=f0_max)
    return synthesize_prepared(
        prepared,
        thresh_db=thresh_db,
        noise_floor_db=noise_floor_db,
        max_voices=max_voices,
        gain_curve=gain_curve,
        spectral_tilt_db=spectral_tilt_db,
    )


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
    parser.add_argument("--spectral-tilt-db", type=float, default=-12.0,
                        help="Spectral tilt in dB/oct (default -12, 0=flat)")
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
                     gain_curve=args.gain_curve,
                     spectral_tilt_db=args.spectral_tilt_db)

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