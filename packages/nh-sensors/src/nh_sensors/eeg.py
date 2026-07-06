import numpy as np
from scipy.signal import welch
from typing import Dict, List


BANDS = {
    "delta": (0.5, 4),
    "theta": (4, 8),
    "alpha": (8, 13),
    "smr": (12, 15),
    "beta": (13, 30),
    "gamma": (30, 44),
}


class EEGProcessor:
    """Band-power extractor and concentration estimator for Muse EEG."""

    def __init__(self, fs: int = 256, window_size: int = 1024,
                 smoothing_factor: float = 0.7, frontal_channels: List[str] = None):
        self.fs = fs
        self.window_size = window_size
        self.smoothing_factor = smoothing_factor
        self.frontal_channels = frontal_channels or ["AF7", "AF8"]
        self.baseline: Dict[str, float] = {}
        self._last_concentration = 0.0

    def compute_band_power(self, signal: np.ndarray, low_freq: float, high_freq: float) -> float:
        if len(signal) < self.fs:
            return 0.0
        freqs, psd = welch(signal, fs=self.fs, nperseg=min(len(signal), self.fs * 2),
                           noverlap=self.fs // 2, scaling="density")
        band_idx = (freqs >= low_freq) & (freqs <= high_freq)
        if not np.any(band_idx):
            return 0.0
        return float(np.mean(psd[band_idx]))

    def analyze(self, channels: Dict[str, np.ndarray]) -> Dict[str, float]:
        """Analyze latest window for frontal channels and return metrics."""
        frontal = [np.array(channels[ch][-self.window_size:]) for ch in self.frontal_channels if ch in channels]
        if not frontal or not all(len(s) >= self.fs for s in frontal):
            return {}

        band_powers = {}
        for band, (low, high) in BANDS.items():
            powers = [self.compute_band_power(s, low, high) for s in frontal]
            band_powers[band] = float(np.mean(powers))

        beta_alpha = band_powers["beta"] / (band_powers["alpha"] + 1e-10)
        smr = band_powers["smr"]
        theta_beta = band_powers["theta"] / (band_powers["beta"] + 1e-10)
        inv_theta_beta = 1.0 / (theta_beta + 1e-10)

        if self.baseline:
            beta_alpha /= self.baseline.get("beta_alpha", 1.0)
            smr /= self.baseline.get("smr", 1.0)
            inv_theta_beta /= self.baseline.get("inv_theta_beta", 1.0)

        raw = 0.5 * beta_alpha + 0.3 * smr + 0.2 * inv_theta_beta
        score = np.clip(raw * 50, 0, 100)
        score = self.smoothing_factor * self._last_concentration + (1 - self.smoothing_factor) * score
        self._last_concentration = score

        return {
            "band_powers": band_powers,
            "concentration_score": float(score),
            "beta_alpha_ratio": float(beta_alpha),
            "smr_power": float(smr),
            "inv_theta_beta": float(inv_theta_beta),
        }

    def calibrate(self, measurements: List[Dict[str, float]]) -> None:
        """Set baseline from a list of metric dicts."""
        if not measurements:
            return
        self.baseline = {
            "beta_alpha": float(np.mean([m["beta_alpha_ratio"] for m in measurements])),
            "smr": float(np.mean([m["smr_power"] for m in measurements])),
            "inv_theta_beta": float(np.mean([m["inv_theta_beta"] for m in measurements])),
        }

    def signal_quality(self, channels: Dict[str, np.ndarray]) -> str:
        all_data = np.concatenate([np.array(channels[ch]) for ch in channels if ch in channels])
        if len(all_data) == 0:
            return "unknown"
        std = np.std(all_data)
        amplitude = np.max(np.abs(all_data))
        if 50 < amplitude < 1500 and 10 < std < 500:
            return "good"
        elif 20 < amplitude < 2000 and 5 < std < 800:
            return "fair"
        return "poor"
