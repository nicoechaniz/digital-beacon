import numpy as np
from scipy.ndimage import uniform_filter1d
from typing import Dict, Optional, Tuple


def _interp1d_frames(desc: np.ndarray, target_length: int) -> np.ndarray:
    """Linearly interpolate [..., N] to [..., target_length] along time axis.

    desc is expected as [B, N, D] where N is the time dimension.
    """
    if desc.shape[-1] == target_length:
        return desc
    if desc.ndim < 3:
        desc = desc.reshape(1, desc.shape[0], desc.shape[1] if desc.ndim == 2 else 1)
    B, N, D = desc.shape
    if N == target_length:
        return desc
    old_x = np.linspace(0, 1, N)
    new_x = np.linspace(0, 1, target_length)
    result = np.zeros((B, target_length, D), dtype=desc.dtype)
    for b in range(B):
        for d in range(D):
            result[b, :, d] = np.interp(new_x, old_x, desc[b, :, d])
    return result


def compute_v4_linear(
    f0: np.ndarray,
    voiced: np.ndarray,
    target_length: int,
) -> np.ndarray:
    """V4-lin: 4D temporal F0 dynamics (linear ratios)."""
    f0 = np.asarray(f0, dtype=np.float64)
    voiced = np.asarray(voiced, dtype=bool)
    if f0.ndim == 1:
        f0 = f0[np.newaxis, :]
        voiced = voiced[np.newaxis, :]
    B, N = f0.shape

    v = voiced.astype(np.float64)
    v_smooth = uniform_filter1d(v, size=3, mode='nearest', axis=-1)

    f0_safe = f0.copy()
    f0_safe[~voiced] = 0.0

    f0_prev = np.concatenate([f0_safe[:, :1], f0_safe[:, :-1]], axis=1)
    v_prev = np.concatenate([voiced[:, :1], voiced[:, :-1]], axis=1)
    both_prev = voiced & v_prev

    ratio_prev = np.ones((B, N), dtype=np.float64)
    denom_prev = np.maximum(f0_prev, 1.0)
    ratio_prev[both_prev] = f0_safe[both_prev] / denom_prev[both_prev]

    f0_next = np.concatenate([f0_safe[:, 1:], f0_safe[:, -1:]], axis=1)
    v_next = np.concatenate([voiced[:, 1:], voiced[:, -1:]], axis=1)
    both_next = voiced & v_next

    ratio_next = np.ones((B, N), dtype=np.float64)
    denom_curr = np.maximum(f0_safe, 1.0)
    ratio_next[both_next] = f0_next[both_next] / denom_curr[both_next]

    ratio_prev = np.clip(ratio_prev, 0.5, 2.0) - 1.0
    ratio_next = np.clip(ratio_next, 0.5, 2.0) - 1.0

    ratio_mag = (np.abs(ratio_prev) + np.abs(ratio_next)) / 2.0
    local_mean = uniform_filter1d(ratio_mag, size=5, mode='nearest', axis=-1)
    local_sq = uniform_filter1d(ratio_mag ** 2, size=5, mode='nearest', axis=-1)
    local_std = np.sqrt(np.maximum(local_sq - local_mean ** 2, 0.0))
    regularity = np.clip(1.0 - local_std, 0.0, 1.0)

    desc = np.stack([ratio_prev, ratio_next, v_smooth, regularity], axis=-1)
    return _interp1d_frames(desc, target_length)


def compute_v4_log(
    f0: np.ndarray,
    voiced: np.ndarray,
    target_length: int,
) -> np.ndarray:
    """V4-log: 4D temporal F0 dynamics (log2 ratios)."""
    f0 = np.asarray(f0, dtype=np.float64)
    voiced = np.asarray(voiced, dtype=bool)
    if f0.ndim == 1:
        f0 = f0[np.newaxis, :]
        voiced = voiced[np.newaxis, :]
    B, N = f0.shape

    v = voiced.astype(np.float64)
    v_smooth = uniform_filter1d(v, size=3, mode='nearest', axis=-1)

    f0_safe = f0.copy()
    f0_safe[~voiced] = 0.0

    f0_prev = np.concatenate([f0_safe[:, :1], f0_safe[:, :-1]], axis=1)
    v_prev = np.concatenate([voiced[:, :1], voiced[:, :-1]], axis=1)
    both_prev = voiced & v_prev

    log_ratio_prev = np.zeros((B, N), dtype=np.float64)
    denom_prev = np.maximum(f0_prev, 1.0)
    raw_ratio_prev = f0_safe / denom_prev
    log_ratio_prev[both_prev] = np.log2(np.clip(raw_ratio_prev[both_prev], 0.01, None))
    log_ratio_prev = np.clip(log_ratio_prev, -1.0, 1.0)

    f0_next = np.concatenate([f0_safe[:, 1:], f0_safe[:, -1:]], axis=1)
    v_next = np.concatenate([voiced[:, 1:], voiced[:, -1:]], axis=1)
    both_next = voiced & v_next

    log_ratio_next = np.zeros((B, N), dtype=np.float64)
    denom_curr = np.maximum(f0_safe, 1.0)
    raw_ratio_next = f0_next / denom_curr
    log_ratio_next[both_next] = np.log2(np.clip(raw_ratio_next[both_next], 0.01, None))
    log_ratio_next = np.clip(log_ratio_next, -1.0, 1.0)

    ratio_mag = (np.abs(log_ratio_prev) + np.abs(log_ratio_next)) / 2.0
    local_mean = uniform_filter1d(ratio_mag, size=5, mode='nearest', axis=-1)
    local_sq = uniform_filter1d(ratio_mag ** 2, size=5, mode='nearest', axis=-1)
    local_std = np.sqrt(np.maximum(local_sq - local_mean ** 2, 0.0))
    regularity = np.clip(1.0 - local_std, 0.0, 1.0)

    desc = np.stack([log_ratio_prev, log_ratio_next, v_smooth, regularity], axis=-1)
    return _interp1d_frames(desc, target_length)


def compute_h_series(
    audio: np.ndarray,
    f0: np.ndarray,
    voiced: np.ndarray,
    target_length: int,
    sr: int = 16000,
    n_fft: int = 2048,
    hop_length: int = 160,
    peak_search: int = 2,
    norm_stats: Optional[Dict[str, np.ndarray]] = None,
) -> np.ndarray:
    """H-series: 8D harmonic amplitude structure.

    Dimensions 0-4: log(H_{n+1}/H_1 + 1e-3)
    Dimension 5: harmonic_concentration
    Dimension 6: harmonic_deviation
    Dimension 7: voicing_strength
    """
    import librosa
    audio = np.asarray(audio, dtype=np.float64)
    f0 = np.asarray(f0, dtype=np.float64)
    voiced = np.asarray(voiced, dtype=bool)
    if audio.ndim == 1:
        audio = audio[np.newaxis, :]
    if f0.ndim == 1:
        f0 = f0[np.newaxis, :]
        voiced = voiced[np.newaxis, :]
    B = audio.shape[0]
    freq_res = sr / n_fft

    S = np.abs(librosa.stft(audio, n_fft=n_fft, hop_length=hop_length, center=True))
    T_stft = S.shape[2]
    n_bins = S.shape[1]
    N = min(T_stft, f0.shape[1])

    f0_aligned = f0[:, :N]
    v_aligned = voiced[:, :N]
    S_aligned = S[:, :, :N]
    total_energy = S_aligned.sum(axis=1)

    n_harmonics = 6
    harmonics = np.zeros((B, n_harmonics, N), dtype=np.float64)
    for h in range(1, n_harmonics + 1):
        expected_freq = f0_aligned * h
        expected_bin = np.round(expected_freq / freq_res).astype(int)
        for offset in range(-peak_search, peak_search + 1):
            bin_idx = np.clip(expected_bin + offset, 0, n_bins - 1)
            gathered = np.take_along_axis(S_aligned, bin_idx[:, np.newaxis, :], axis=1).squeeze(1)
            harmonics[:, h - 1, :] = np.maximum(harmonics[:, h - 1, :], gathered)

    v_mask = v_aligned.astype(np.float64)
    harmonics = harmonics * v_mask[:, np.newaxis, :]

    H1 = harmonics[:, 0, :]
    h_ratios = []
    for n in range(1, 6):
        ratio = np.log(harmonics[:, n, :] / (H1 + 1e-6) + 1e-3)
        ratio = ratio * v_mask
        h_ratios.append(ratio)

    h_sum = harmonics.sum(axis=1)
    h_conc = h_sum / (total_energy + 1e-8)
    h_conc = np.clip(h_conc, 0.0, 1.0) * v_mask

    ratio_stack = np.stack(h_ratios, axis=-1)
    h_dev = ratio_stack.std(axis=-1) * v_mask

    if N >= 3:
        v_smooth = uniform_filter1d(v_mask, size=3, mode='nearest', axis=-1)
    else:
        v_smooth = v_mask

    desc = np.stack(h_ratios + [h_conc, h_dev, v_smooth], axis=-1)
    if norm_stats is not None:
        mean = np.asarray(norm_stats['mean'], dtype=np.float64)
        std = np.asarray(norm_stats['std'], dtype=np.float64)
        normalized = (desc - mean) / (std + 1e-8)
        desc = normalized * v_mask[:, :, np.newaxis]

    return _interp1d_frames(desc, target_length)


A4_16K_BAND_EDGES = [
    (3, 6), (6, 12), (12, 24), (24, 48),
    (48, 96), (96, 192), (192, 384), (384, 513),
]


def compute_a4_16k(
    audio: np.ndarray,
    target_length: int,
    sr: int = 16000,
    n_fft: int = 1024,
    hop_length: int = 160,
) -> np.ndarray:
    """A4-16k: 8D spectral band energy deltas (non-ratio control)."""
    import librosa
    audio = np.asarray(audio, dtype=np.float64)
    if audio.ndim == 1:
        audio = audio[np.newaxis, :]
    B = audio.shape[0]

    S = librosa.stft(audio, n_fft=n_fft, hop_length=hop_length, center=True)
    log_mag = np.log1p(np.abs(S))
    T_stft = log_mag.shape[2]

    bands = []
    for lo, hi in A4_16K_BAND_EDGES:
        hi_clamped = min(hi, log_mag.shape[1])
        band_mean = log_mag[:, lo:hi_clamped, :].mean(axis=1)
        bands.append(band_mean)
    banded = np.stack(bands, axis=1)  # [B, 8, T]

    delta = banded[:, :, 1:] - banded[:, :, :-1]
    zero_pad = np.zeros((B, 8, 1), dtype=np.float64)
    delta = np.concatenate([zero_pad, delta], axis=2)

    mean = delta.mean(axis=2, keepdims=True)
    std = delta.std(axis=2, keepdims=True).clip(min=1e-8)
    delta = (delta - mean) / std

    return _interp1d_frames(delta.transpose(0, 2, 1), target_length)


def load_h_series_norm_stats(path: str) -> Dict[str, np.ndarray]:
    import json
    with open(path, "r") as f:
        data = json.load(f)
    return {
        "mean": np.array(data["mean"], dtype=np.float64),
        "std": np.array(data["std"], dtype=np.float64),
    }
