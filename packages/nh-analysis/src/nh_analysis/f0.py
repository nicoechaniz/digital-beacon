from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional, Tuple

import numpy as np


class F0Estimator(ABC):
    """Pluggable F0 estimator."""

    @abstractmethod
    def estimate(self, audio: np.ndarray, sr: int) -> Tuple[np.ndarray, np.ndarray]:
        """Return (f0_hz, voiced_flag) arrays aligned with hop_length."""
        ...


class LibrosaPyinEstimator(F0Estimator):
    def __init__(self, fmin: float = 75.0, fmax: float = 500.0,
                 frame_length: int = 2048, hop_length: int = 160):
        self.fmin = fmin
        self.fmax = fmax
        self.frame_length = frame_length
        self.hop_length = hop_length

    def estimate(self, audio: np.ndarray, sr: int) -> Tuple[np.ndarray, np.ndarray]:
        import librosa
        f0, voiced_flag, _ = librosa.pyin(
            audio,
            sr=sr,
            fmin=self.fmin,
            fmax=self.fmax,
            frame_length=self.frame_length,
            hop_length=self.hop_length,
            fill_na=0.0,
        )
        return f0, voiced_flag
