"""Sample layer: load, loop, and analyze audio samples for ratio-based modulation.

This module is intentionally simple and self-contained. It does not depend on
the nh-toolkit refactor; it uses librosa + numpy + python-osc so it can be
wired into the existing digital_beacon.main process.

A sample is read into memory, played in a loop, and analyzed in chunks. The
resulting descriptors (energy, f0 ratio, spectral centroid, etc.) are published
as OSC messages that can drive the beacon and the shaper.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Deque, Dict, List, Optional, Tuple
from collections import deque

import numpy as np

log = logging.getLogger(__name__)


try:
    import librosa
    HAS_LIBROSA = True
except ImportError:
    HAS_LIBROSA = False
    librosa = None

from .resonant_filter import ResonantFilter

try:
    import sounddevice as sd
    HAS_SOUNDDEVICE = True
except ImportError:
    HAS_SOUNDDEVICE = False
    sd = None


@dataclass
class SampleDescriptor:
    """A single frame of descriptors extracted from a sample chunk."""

    rms: float = 0.0
    f0_hz: float = 0.0
    f0_ratio: float = 1.0
    centroid: float = 0.0
    bandwidth: float = 0.0
    flatness: float = 0.0
    band_energy: Optional[Dict[int, float]] = None  # energy per octave band
    # Derived descriptors
    rms_delta: float = 0.0
    rms_smooth: float = 0.0
    f0_stability: float = 0.0
    centroid_delta: float = 0.0
    inharmonicity: float = 0.0
    harmonicity: float = 0.0
    residual_ratio: float = 0.0
    harmonic_rms: float = 0.0
    residual_rms: float = 0.0
    timestamp: float = 0.0

    def __post_init__(self):
        if self.band_energy is None:
            self.band_energy = {}

    def to_dict(self) -> Dict[str, float]:
        d = {
            "rms": float(self.rms),
            "f0_hz": float(self.f0_hz),
            "f0_ratio": float(self.f0_ratio),
            "centroid": float(self.centroid),
            "bandwidth": float(self.bandwidth),
            "flatness": float(self.flatness),
            "rms_delta": float(self.rms_delta),
            "rms_smooth": float(self.rms_smooth),
            "f0_stability": float(self.f0_stability),
            "centroid_delta": float(self.centroid_delta),
            "inharmonicity": float(self.inharmonicity),
            "harmonicity": float(self.harmonicity),
            "residual_ratio": float(self.residual_ratio),
            "harmonic_rms": float(self.harmonic_rms),
            "residual_rms": float(self.residual_rms),
            "timestamp": float(self.timestamp),
        }
        d.update({f"band_{k}": float(v) for k, v in self.band_energy.items()})
        return d


class SampleLayer:
    """Load a sample, loop it, analyze chunks, and broadcast descriptors."""

    # 32 octave-scaled bands from 20 Hz to sr/2 (approx)
    N_BANDS = 32

    def __init__(
        self,
        path: str,
        sr: int = 48000,
        chunk_s: float = 0.05,
        f0_beacon_hz: float = 40.4,
        output_device: Optional[int | str] = None,
        on_descriptor: Optional[Callable[[SampleDescriptor], None]] = None,
        history_size: int = 10,
    ):
        if not HAS_LIBROSA:
            raise ImportError("librosa is required for SampleLayer")

        self.path = Path(path)
        self.sr = sr
        self.chunk_s = chunk_s
        self.chunk_size = int(round(sr * chunk_s))
        self.f0_beacon_hz = f0_beacon_hz
        self.output_device = output_device
        self.on_descriptor = on_descriptor
        self.history_size = history_size

        self._y: np.ndarray = np.zeros(0, dtype=np.float32)
        self._y_harmonic: np.ndarray = np.zeros(0, dtype=np.float32)
        self._y_residual: np.ndarray = np.zeros(0, dtype=np.float32)
        self._position: int = 0
        self._running: bool = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._last_descriptor: Optional[SampleDescriptor] = None
        self._history: Deque[SampleDescriptor] = deque(maxlen=history_size)
        self._band_edges: Optional[np.ndarray] = None
        self._resonant_filter = ResonantFilter(sr=sr)

        self._load()
        self._build_band_edges()

    def _load(self) -> None:
        if not self.path.exists():
            raise FileNotFoundError(f"sample not found: {self.path}")
        y, sr_loaded = librosa.load(str(self.path), sr=self.sr, mono=True)
        self._y = y.astype(np.float32)
        log.info("SampleLayer loaded %s: sr=%d length=%.2fs frames=%d",
                 self.path.name, sr_loaded, len(self._y) / sr_loaded, len(self._y))

        # Separate harmonic / residual once at load time.
        # Initial bandwidth is based on global flatness of the whole sample.
        try:
            flat = float(librosa.feature.spectral_flatness(y=self._y)[0, 0])
        except Exception:
            flat = 0.0
        try:
            sep = self._resonant_filter.separate(self._y, self.f0_beacon_hz, flatness=flat, inharmonicity=0.0, stability=1.0)
            self._y_harmonic = sep["harmonic_audio"].astype(np.float32)
            self._y_residual = sep["residual_audio"].astype(np.float32)
        except Exception as e:
            log.warning("ResonantFilter separation failed: %s", e)
            self._y_harmonic = self._y.copy()
            self._y_residual = np.zeros_like(self._y)

    def _build_band_edges(self) -> None:
        # Octave-scaled bands from 20 Hz to Nyquist
        f_min = 20.0
        f_max = self.sr / 2.0
        ratio = (f_max / f_min) ** (1.0 / self.N_BANDS)
        self._band_edges = f_min * (ratio ** np.arange(self.N_BANDS + 1))

    # ------------------------------------------------------------------
    # Analysis
    # ------------------------------------------------------------------
    def _compute_band_energies(self, chunk: np.ndarray) -> Dict[int, float]:
        D = np.abs(librosa.stft(chunk, n_fft=512, hop_length=128))
        freqs = librosa.fft_frequencies(sr=self.sr, n_fft=512)
        energies = {}
        for i in range(self.N_BANDS):
            lo = self._band_edges[i]
            hi = self._band_edges[i + 1]
            mask = (freqs >= lo) & (freqs < hi)
            energies[i] = float(np.mean(D[mask])) if np.any(mask) else 0.0
        return energies

    def _inharmonicity(self, chunk: np.ndarray, f0: float) -> float:
        """Rough inharmonicity: energy NOT on integer multiples of f0."""
        if f0 <= 0:
            return 0.0
        S = np.abs(librosa.stft(chunk, n_fft=2048, hop_length=512))
        freqs = librosa.fft_frequencies(sr=self.sr, n_fft=2048)
        power = np.mean(S ** 2, axis=1)
        total = np.sum(power)
        if total == 0:
            return 0.0
        harmonic_mask = np.zeros_like(freqs, dtype=bool)
        for n in range(1, 64):
            h = n * f0
            if h >= self.sr / 2:
                break
            harmonic_mask |= (np.abs(freqs - h) < 5.0)
        harmonic_power = np.sum(power[harmonic_mask])
        return float(1.0 - harmonic_power / total)

    def _analyze(self, chunk: np.ndarray, h_chunk: np.ndarray = np.zeros(0), r_chunk: np.ndarray = np.zeros(0)) -> SampleDescriptor:
        desc = SampleDescriptor(timestamp=time.time())
        if len(chunk) == 0:
            return desc

        chunk = chunk.astype(np.float64)
        rms = float(np.sqrt(np.mean(chunk ** 2)))
        desc.rms = rms

        # f0 via yin
        f0s = librosa.yin(
            chunk,
            fmin=librosa.note_to_hz("C2"),
            fmax=librosa.note_to_hz("C7"),
            sr=self.sr,
        )
        f0 = float(np.median(f0s)) if len(f0s) > 0 else 0.0
        if np.isnan(f0) or f0 <= 0:
            f0 = 0.0
        desc.f0_hz = f0
        if f0 > 0 and self.f0_beacon_hz > 0:
            desc.f0_ratio = f0 / self.f0_beacon_hz

        # Spectral features
        S = np.abs(librosa.stft(chunk, n_fft=2048, hop_length=512))
        if S.shape[1] > 0:
            desc.centroid = float(librosa.feature.spectral_centroid(S=S, sr=self.sr)[0, 0])
            desc.bandwidth = float(librosa.feature.spectral_bandwidth(S=S, sr=self.sr)[0, 0])
            desc.flatness = float(librosa.feature.spectral_flatness(S=S)[0, 0])

        # Band energies
        desc.band_energy = self._compute_band_energies(chunk)

        # Inharmonicity
        desc.inharmonicity = self._inharmonicity(chunk, f0)

        # Harmonic / residual descriptors from pre-separated components
        if len(h_chunk) > 0 and len(r_chunk) > 0:
            h_energy = float(np.sum(h_chunk ** 2))
            r_energy = float(np.sum(r_chunk ** 2))
            total_energy = h_energy + r_energy + 1e-12
            desc.harmonicity = h_energy / total_energy
            desc.residual_ratio = r_energy / total_energy
            desc.harmonic_rms = float(np.sqrt(h_energy / len(h_chunk)))
            desc.residual_rms = float(np.sqrt(r_energy / len(r_chunk)))

        # Derived descriptors from history
        if self._history:
            prev = self._history[-1]
            desc.rms_delta = rms - prev.rms
            desc.centroid_delta = desc.centroid - prev.centroid

        # Moving average of RMS
        if self._history:
            window = [d.rms for d in self._history] + [rms]
            desc.rms_smooth = float(np.mean(window))
        else:
            desc.rms_smooth = rms

        # F0 stability: low variance of recent f0 estimates
        if len(self._history) >= 2:
            recent_f0 = [d.f0_hz for d in list(self._history)[-5:] + [desc]]
            non_zero = [f for f in recent_f0 if f > 0]
            if len(non_zero) > 1:
                desc.f0_stability = 1.0 / (1.0 + np.std(non_zero) / (np.mean(non_zero) + 1e-6))
            else:
                desc.f0_stability = 0.0
        else:
            desc.f0_stability = 0.0

        # Update history
        self._history.append(desc)
        while len(self._history) > self.history_size:
            self._history.popleft()

        return desc

    # ------------------------------------------------------------------
    # Loop playback (optional) + analysis loop
    # ------------------------------------------------------------------
    def _next_chunk(self) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        with self._lock:
            if len(self._y) == 0:
                z = np.zeros(self.chunk_size, dtype=np.float32)
                return z, z, z
            end = self._position + self.chunk_size
            if end <= len(self._y):
                chunk = self._y[self._position:end]
                h_chunk = self._y_harmonic[self._position:end]
                r_chunk = self._y_residual[self._position:end]
                self._position = end
            else:
                # wrap around
                tail = self._y[self._position:]
                h_tail = self._y_harmonic[self._position:]
                r_tail = self._y_residual[self._position:]
                need = self.chunk_size - len(tail)
                chunk = np.concatenate([tail, self._y[:need]])
                h_chunk = np.concatenate([h_tail, self._y_harmonic[:need]])
                r_chunk = np.concatenate([r_tail, self._y_residual[:need]])
                self._position = need
            return chunk, h_chunk, r_chunk

    def _loop(self) -> None:
        next_time = time.time() + self.chunk_s
        while self._running:
            chunk, h_chunk, r_chunk = self._next_chunk()
            desc = self._analyze(chunk, h_chunk, r_chunk)
            self._last_descriptor = desc
            if self.on_descriptor is not None:
                try:
                    self.on_descriptor(desc)
                except Exception:
                    log.exception("descriptor callback failed")

            # Optional: play the chunk through the default audio output.
            if self.output_device is not None and HAS_SOUNDDEVICE:
                try:
                    sd.play(chunk, self.sr, device=self.output_device, blocking=False)
                except Exception:
                    pass

            # Sleep until next chunk time
            sleep_s = next_time - time.time()
            if sleep_s > 0:
                time.sleep(sleep_s)
            next_time += self.chunk_s

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, name="sample-layer", daemon=True)
        self._thread.start()
        log.info("SampleLayer started: chunk=%d samples @ %.0f Hz, bands=%d",
                 self.chunk_size, 1.0 / self.chunk_s, self.N_BANDS)

    def stop(self) -> None:
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        log.info("SampleLayer stopped")

    def last_descriptor(self) -> Optional[SampleDescriptor]:
        return self._last_descriptor

    def current_position(self) -> int:
        with self._lock:
            return self._position

    def __enter__(self) -> SampleLayer:
        self.start()
        return self

    def __exit__(self, *args) -> None:
        self.stop()
