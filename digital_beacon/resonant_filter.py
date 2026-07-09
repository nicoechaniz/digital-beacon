"""Resonant filter: adaptive harmonic mask for sample analysis.

Reuses nh_analysis.mask.harmonic_mask and adapts the mask bandwidth based on
sample descriptors (flatness, inharmonicity, stability). The filter separates a
sample into harmonic (on the f1 lattice) and residual components, and provides
harmonicity descriptors that can drive the beacon/shaper mappings.
"""

from __future__ import annotations

import logging
from typing import Dict, Tuple

import numpy as np

from nh_analysis.mask import harmonic_mask

log = logging.getLogger(__name__)


class ResonantFilter:
    """Separate a sample into harmonic + residual components.

    The mask is adaptive: noisy/inharmonic samples get a wider mask so more
    energy is classified as harmonic; stable tonal samples get a narrower mask.
    """

    def __init__(
        self,
        sr: int = 48000,
        base_bw: float = 40.0,
        max_bw: float = 200.0,
        n_harmonics: int = 32,
    ):
        self.sr = sr
        self.base_bw = base_bw
        self.max_bw = max_bw
        self.n_harmonics = n_harmonics

    def bandwidth_hz(
        self,
        flatness: float = 0.0,
        inharmonicity: float = 0.0,
        stability: float = 1.0,
    ) -> float:
        """Compute adaptive mask bandwidth in Hz."""
        # Stable/tone-like -> narrower mask; noisy/inharmonic -> wider mask
        bw = self.base_bw
        bw += flatness * (self.max_bw - self.base_bw)
        bw += inharmonicity * (self.max_bw - self.base_bw) * 0.5
        # Stability narrows the mask when f0 is well defined
        bw *= (1.0 - stability * 0.5)
        return float(np.clip(bw, 10.0, self.max_bw))

    def separate(
        self,
        audio: np.ndarray,
        f1: float,
        flatness: float = 0.0,
        inharmonicity: float = 0.0,
        stability: float = 1.0,
    ) -> Dict[str, np.ndarray]:
        """Return harmonic_audio, residual_audio and mask."""
        bw = self.bandwidth_hz(flatness, inharmonicity, stability)
        result = harmonic_mask(
            audio,
            sr=self.sr,
            f1=f1,
            n_harmonics=self.n_harmonics,
            bandwidth_hz=bw,
            strict=False,
        )
        return result

    def descriptors(
        self,
        audio: np.ndarray,
        harmonic_audio: np.ndarray,
        residual_audio: np.ndarray,
    ) -> Dict[str, float]:
        """Compute harmonicity descriptors from separated components."""
        total_energy = float(np.sum(audio ** 2)) + 1e-12
        harmonic_energy = float(np.sum(harmonic_audio ** 2))
        residual_energy = float(np.sum(residual_audio ** 2))
        harmonicity = harmonic_energy / total_energy
        residual_ratio = residual_energy / total_energy
        return {
            "harmonicity": harmonicity,
            "residual_ratio": residual_ratio,
            "harmonic_rms": float(np.sqrt(harmonic_energy / len(audio))) if len(audio) else 0.0,
            "residual_rms": float(np.sqrt(residual_energy / len(audio))) if len(audio) else 0.0,
        }

    def separate_chunk(
        self,
        chunk: np.ndarray,
        f1: float,
        flatness: float = 0.0,
        inharmonicity: float = 0.0,
        stability: float = 1.0,
    ) -> Dict[str, float]:
        """Separate a single chunk and return descriptors."""
        result = self.separate(chunk, f1, flatness, inharmonicity, stability)
        desc = self.descriptors(chunk, result["harmonic_audio"], result["residual_audio"])
        return desc
