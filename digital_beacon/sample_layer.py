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
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np

log = logging.getLogger(__name__)


try:
    import librosa
    HAS_LIBROSA = True
except ImportError:
    HAS_LIBROSA = False
    librosa = None

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
    band_energy: Optional[Dict[int, float]] = None  # energy in coarse bands (octave-ish)
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
            "timestamp": float(self.timestamp),
        }
        d.update({f"band_{k}": float(v) for k, v in self.band_energy.items()})
        return d


class SampleLayer:
    """Load a sample, loop it, analyze chunks, and broadcast descriptors."""

    def __init__(
        self,
        path: str,
        sr: int = 48000,
        chunk_s: float = 0.05,
        f0_beacon_hz: float = 40.4,
        output_device: Optional[int | str] = None,
        on_descriptor: Optional[Callable[[SampleDescriptor], None]] = None,
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

        self._y: np.ndarray = np.zeros(0, dtype=np.float32)
        self._position: int = 0
        self._running: bool = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._last_descriptor: Optional[SampleDescriptor] = None

        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            raise FileNotFoundError(f"sample not found: {self.path}")
        y, sr_loaded = librosa.load(str(self.path), sr=self.sr, mono=True)
        self._y = y.astype(np.float32)
        log.info("SampleLayer loaded %s: sr=%d length=%.2fs frames=%d",
                 self.path.name, sr_loaded, len(self._y) / sr_loaded, len(self._y))

    # ------------------------------------------------------------------
    # Analysis
    # ------------------------------------------------------------------
    def _analyze(self, chunk: np.ndarray) -> SampleDescriptor:
        desc = SampleDescriptor(timestamp=time.time())
        if len(chunk) == 0:
            return desc

        chunk = chunk.astype(np.float64)
        rms = float(np.sqrt(np.mean(chunk ** 2)))
        desc.rms = rms

        # f0 via piptrack (lightweight, no crepe model needed)
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

        # Coarse band energies (low, mid, high)
        D = np.abs(librosa.stft(chunk, n_fft=512, hop_length=128))
        freqs = librosa.fft_frequencies(sr=self.sr, n_fft=512)
        bands = [(0, 200), (200, 1000), (1000, 5000)]
        for idx, (lo, hi) in enumerate(bands):
            mask = (freqs >= lo) & (freqs < hi)
            desc.band_energy[idx] = float(np.mean(D[mask])) if np.any(mask) else 0.0

        return desc

    # ------------------------------------------------------------------
    # Loop playback (optional) + analysis loop
    # ------------------------------------------------------------------
    def _next_chunk(self) -> np.ndarray:
        with self._lock:
            if len(self._y) == 0:
                return np.zeros(self.chunk_size, dtype=np.float32)
            end = self._position + self.chunk_size
            if end <= len(self._y):
                chunk = self._y[self._position:end]
                self._position = end
            else:
                # wrap around
                tail = self._y[self._position:]
                need = self.chunk_size - len(tail)
                chunk = np.concatenate([tail, self._y[:need]])
                self._position = need
            return chunk

    def _loop(self) -> None:
        next_time = time.time() + self.chunk_s
        while self._running:
            chunk = self._next_chunk()
            desc = self._analyze(chunk)
            self._last_descriptor = desc
            if self.on_descriptor is not None:
                try:
                    self.on_descriptor(desc)
                except Exception:
                    log.exception("descriptor callback failed")

            # Optional: play the chunk through the default audio output.
            # This is disabled by default because the R24 audio path is handled
            # by SuperCollider; the sample layer is a control source.
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
        log.info("SampleLayer started: chunk=%d samples @ %.0f Hz", self.chunk_size, 1.0 / self.chunk_s)

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
