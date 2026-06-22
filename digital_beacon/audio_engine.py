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
        # Per-voice state: {harmonic_n: {"phase": float, "env": float, "params": VoiceParams}}
        # env ramps 0→1 on attack, 1→0 on release. Voices with env≈0 and inactive are pruned.
        self._voice_state: dict[int, dict] = {}
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
        dt = frames / self._sample_rate
        voices = self._store.get_snapshot()  # active voices only
        active_ns = set(voices.keys())
        tracked_ns = set(self._voice_state.keys())

        # ── Add new voices (just became active) ──────────────────────
        for n in active_ns - tracked_ns:
            self._voice_state[n] = {"phase": 0.0, "env": 0.0, "params": voices[n]}

        # ── Mark released voices (was tracked, no longer active) ────
        for n in tracked_ns - active_ns:
            self._voice_state[n]["params"].active = False

        # ── Update active voices' params ─────────────────────────────
        for n in active_ns & tracked_ns:
            self._voice_state[n]["params"] = voices[n]

        # Per-voice normalization
        n_active = len(active_ns)
        norm = 1.0 / (n_active ** 0.5) if n_active > 0 else 1.0

        mix = np.zeros((frames, 2), dtype=np.float32)

        to_prune = []
        for n, state in self._voice_state.items():
            params = state["params"]
            if params.freq <= 0:
                to_prune.append(n)
                continue

            target_env = 1.0 if params.active else 0.0
            current_env = state["env"]
            attack_s = params.attack_s
            release_s = params.release_s

            # Compute envelope ramp
            if target_env > current_env:
                # Attack phase
                rate = 1.0 / max(attack_s, 0.0001)  # avoid div by zero
                new_env = min(target_env, current_env + rate * dt)
            elif target_env < current_env:
                # Release phase
                rate = 1.0 / max(release_s, 0.0001)
                new_env = max(target_env, current_env - rate * dt)
            else:
                new_env = current_env

            state["env"] = new_env

            # Prune fully released voices
            if not params.active and new_env <= 0.0:
                to_prune.append(n)
                continue

            if new_env <= 0.0:
                continue

            # Generate sine
            t = np.arange(frames, dtype=np.float64) / self._sample_rate
            start_phase = state["phase"]
            carrier_phases = 2.0 * np.pi * params.freq * t + start_phase
            sine = np.sin(carrier_phases + params.phase).astype(np.float32)
            sine *= float(params.gain) * norm * new_env
            state["phase"] = (
                carrier_phases[-1] + 2.0 * np.pi * params.freq / self._sample_rate
            ) % (2.0 * np.pi)

            angle = (float(params.pan) + 1.0) * (np.pi / 4.0)
            mix[:, 0] += sine * float(np.cos(angle))
            mix[:, 1] += sine * float(np.sin(angle))

        for n in to_prune:
            del self._voice_state[n]

        # Master gain + soft limiter
        mix *= self._store.get_master_gain()
        mix = np.tanh(mix * 1.05) * 0.95
        outdata[:] = mix

    def _on_stream_finished(self) -> None:
        log.warning("Audio stream finished unexpectedly.")
        self._running = False
