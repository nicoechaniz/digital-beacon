"""Real-time additive synthesis engine for the Shaper.

Adapted from NaturalHarmony/harmonic_shaper/audio_engine.py:
- Same architecture: numpy + sounddevice PortAudio callback.
- Bumped MAX_VOICES to config.MAX_VOICES (32).
- Equal-power stereo pan preserved.
- Phase accumulator continuity preserved across callbacks.
- The audio callback runs in a C thread, so the snapshot read must be
  brief and lock-free from its perspective — we rely on dict copy being
  fast for ≤32 entries.
"""

import logging
from typing import Optional

import numpy as np

try:
    import sounddevice as sd
    HAS_SOUNDDEVICE = True
except ImportError:
    HAS_SOUNDDEVICE = False
    sd = None  # type: ignore

from .state import VoiceParameterStore
from . import config

log = logging.getLogger(__name__)


class AudioEngine:
    """Stereo additive synthesis — one pure sine per active voice."""

    def __init__(
        self,
        store: VoiceParameterStore,
        sample_rate: int = config.AUDIO_SAMPLE_RATE,
        block_size: int = config.AUDIO_BLOCK_SIZE,
        device: Optional[int | str] = config.AUDIO_DEVICE,
    ):
        if not HAS_SOUNDDEVICE:
            raise ImportError("sounddevice is required. pip install sounddevice")
        self._store = store
        self._sample_rate = sample_rate
        self._block_size = block_size
        self._device = device
        self._stream: Optional["sd.OutputStream"] = None
        self._phase_acc: dict[int, float] = {}
        self._running = False

    def start(self) -> None:
        if self._running:
            return
        self._stream = sd.OutputStream(
            samplerate=self._sample_rate,
            blocksize=self._block_size,
            channels=2,
            dtype="float32",
            device=self._device,
            callback=self._audio_callback,
            finished_callback=self._on_stream_finished,
        )
        self._stream.start()
        self._running = True
        log.info("Shaper audio: sr=%d block=%d device=%s",
                 self._sample_rate, self._block_size, self._device)

    def stop(self) -> None:
        self._running = False
        if self._stream:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception as exc:
                log.warning("Error closing stream: %s", exc)
            self._stream = None
        log.info("Shaper audio stopped.")

    @property
    def is_running(self) -> bool:
        return bool(self._running and self._stream and self._stream.active)

    @staticmethod
    def list_devices() -> str:
        if HAS_SOUNDDEVICE:
            return str(sd.query_devices())
        return "(sounddevice not installed)"

    def _audio_callback(self, outdata: np.ndarray, frames: int, time_info, status) -> None:
        if status:
            log.debug("Audio status: %s", status)
        voices = self._store.get_snapshot()
        mix = np.zeros((frames, 2), dtype=np.float32)

        for n, params in voices.items():
            if params.freq <= 0:
                continue
            t = np.arange(frames, dtype=np.float64) / self._sample_rate
            start_phase = self._phase_acc.get(n, 0.0)
            carrier_phases = 2.0 * np.pi * params.freq * t + start_phase
            sine = np.sin(carrier_phases + params.phase).astype(np.float32)
            sine *= float(params.gain)
            self._phase_acc[n] = (
                carrier_phases[-1] + 2.0 * np.pi * params.freq / self._sample_rate
            ) % (2.0 * np.pi)
            angle = (float(params.pan) + 1.0) * (np.pi / 4.0)
            mix[:, 0] += sine * float(np.cos(angle))
            mix[:, 1] += sine * float(np.sin(angle))

        # Prune accumulators for voices that went inactive
        for n in [k for k in list(self._phase_acc) if k not in voices]:
            del self._phase_acc[n]

        mix *= self._store.get_master_gain()
        np.clip(mix, -1.0, 1.0, out=mix)
        outdata[:] = mix

    def _on_stream_finished(self) -> None:
        log.warning("Audio stream finished unexpectedly.")
        self._running = False
