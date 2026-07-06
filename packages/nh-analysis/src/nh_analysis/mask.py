import numpy as np


def harmonic_mask(audio: np.ndarray, sr: int, f1: float, n_harmonics: int = 32,
                  bandwidth_hz: float = 40.0, strict: bool = True,
                  hop_length: int = 512, n_fft: int = 2048) -> dict:
    """Separate audio into harmonic and residual components via comb mask.

    Returns dict with harmonic_audio, residual_audio, mask.
    """
    import librosa
    S = librosa.stft(audio, n_fft=n_fft, hop_length=hop_length)
    S_mag = np.abs(S)
    freqs = librosa.fft_frequencies(sr=sr, n_fft=n_fft)

    mask = np.zeros_like(S_mag, dtype=np.float32)
    for n in range(1, n_harmonics + 1):
        target = n * f1
        if target >= sr / 2:
            break
        if strict:
            # Hard mask: 1 at the single closest bin, 0 elsewhere
            idx = np.argmin(np.abs(freqs - target))
            mask[idx, :] = 1.0
        else:
            # Soft mask around target with bandwidth
            half_bw = bandwidth_hz / 2.0
            lo = target - half_bw
            hi = target + half_bw
            window = (freqs >= lo) & (freqs <= hi)
            mask[window, :] = 1.0

    # Expand mask to complex STFT shape
    mask_complex = mask.astype(np.complex64)
    S_harmonic = S * mask_complex
    S_residual = S * (1.0 - mask_complex)

    harmonic_audio = librosa.istft(S_harmonic, hop_length=hop_length, length=len(audio))
    residual_audio = librosa.istft(S_residual, hop_length=hop_length, length=len(audio))

    return {
        "harmonic_audio": harmonic_audio,
        "residual_audio": residual_audio,
        "mask": mask,
    }
