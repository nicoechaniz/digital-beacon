import numpy as np


def harmonicity_score(audio: np.ndarray, sr: int, f1: float, n_harmonics: int = 32,
                      hop_length: int = 512, n_fft: int = 2048) -> float:
    """Compute a global harmonicity score: ratio of energy on harmonic bins to total energy."""
    import librosa
    S = np.abs(librosa.stft(audio, n_fft=n_fft, hop_length=hop_length))
    freqs = librosa.fft_frequencies(sr=sr, n_fft=n_fft)
    total_energy = np.sum(S ** 2)
    if total_energy == 0:
        return 0.0
    harmonic_energy = 0.0
    for n in range(1, n_harmonics + 1):
        target = n * f1
        if target >= sr / 2:
            break
        idx = np.argmin(np.abs(freqs - target))
        # sum a small window around the bin
        lo = max(0, idx - 1)
        hi = min(S.shape[0], idx + 2)
        harmonic_energy += np.sum(S[lo:hi, :] ** 2)
    return harmonic_energy / total_energy


def spectral_metrics(audio: np.ndarray, sr: int, n_fft: int = 2048, hop_length: int = 512) -> dict:
    """Basic spectral metrics for candidate F0 search."""
    import librosa
    S = np.abs(librosa.stft(audio, n_fft=n_fft, hop_length=hop_length))
    power = np.mean(S ** 2, axis=1)
    freqs = librosa.fft_frequencies(sr=sr, n_fft=n_fft)
    peak_idx = int(np.argmax(power))
    return {
        "peak_freq": freqs[peak_idx],
        "peak_power": float(power[peak_idx]),
        "spectral_centroid": float(np.sum(freqs * power) / np.sum(power)) if np.sum(power) > 0 else 0.0,
    }


def harmonic_f1_search(audio: np.ndarray, sr: int,
                         fmin: float = 20.0, fmax: float = 200.0,
                         n_harmonics: int = 32,
                         hop_length: int = 512, n_fft: int = 2048) -> dict:
    """Search for the fundamental f1 that maximizes harmonic energy."""
    import librosa
    S = np.abs(librosa.stft(audio, n_fft=n_fft, hop_length=hop_length))
    freqs = librosa.fft_frequencies(sr=sr, n_fft=n_fft)
    power = np.mean(S ** 2, axis=1)

    candidates = np.linspace(fmin, fmax, 400)
    best_score = -1.0
    best_f1 = fmin
    for f1 in candidates:
        score = 0.0
        for n in range(1, n_harmonics + 1):
            target = n * f1
            if target >= sr / 2:
                break
            idx = int(np.round(target / (sr / n_fft)))
            if 0 <= idx < len(power):
                score += power[idx]
        if score > best_score:
            best_score = score
            best_f1 = f1

    return {
        "f1": float(best_f1),
        "score": float(best_score),
        "n_harmonics": n_harmonics,
    }
