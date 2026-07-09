"""SamplePlayer: loop the loaded sample through sounddevice for monitoring.

This is a separate audio path from the SuperCollider beacon engine. It lets the
user hear the original sample on top of the beacon/shaper mix, controlled by a
gain slider.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Optional

import numpy as np

log = logging.getLogger(__name__)

try:
    import sounddevice as sd
    HAS_SOUNDDEVICE = True
except ImportError:
    HAS_SOUNDDEVICE = False
    sd = None


class SamplePlayer:
    """Loop-play a WAV file with adjustable gain.

    Runs on the default sounddevice output. Gain can be changed live.
    """

    def __init__(self, sr: int = 48000):
        self.sr = sr
        self._y: np.ndarray = np.zeros(0, dtype=np.float32)
        self._position: int = 0
        self._gain: float = 0.0
        self._stream: Optional[sd.OutputStream] = None
        self._lock = threading.Lock()

    def load(self, path: str) -> bool:
        if not HAS_SOUNDDEVICE:
            log.warning("sounddevice not available; sample player disabled")
            return False
        try:
            import librosa
            y, sr_loaded = librosa.load(str(Path(path).expanduser()), sr=self.sr, mono=True)
            with self._lock:
                self._y = y.astype(np.float32)
                self._position = 0
            log.info("SamplePlayer loaded: %s sr=%d length=%.2fs", path, sr_loaded, len(self._y) / sr_loaded)
            return True
        except Exception as e:
            log.error("SamplePlayer failed to load %s: %s", path, e)
            return False

    def set_gain(self, gain: float) -> None:
        with self._lock:
            self._gain = max(0.0, min(1.0, float(gain)))
        log.debug("SamplePlayer gain: %.3f", self._gain)

    def get_gain(self) -> float:
        with self._lock:
            return self._gain

    def play(self) -> bool:
        if not HAS_SOUNDDEVICE or self._stream is not None:
            return False
        try:
            self._stream = sd.OutputStream(
                samplerate=self.sr,
                channels=1,
                dtype=np.float32,
                callback=self._callback,
            )
            self._stream.start()
            log.info("SamplePlayer started")
            return True
        except Exception as e:
            log.error("SamplePlayer failed to start: %s", e)
            self._stream = None
            return False

    def stop(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception as e:
                log.warning("SamplePlayer stop error: %s", e)
            self._stream = None
            log.info("SamplePlayer stopped")

    def is_playing(self) -> bool:
        return self._stream is not None

    def _callback(self, outdata: np.ndarray, frames: int, time_info, status) -> None:
        with self._lock:
            y = self._y
            pos = self._position
            gain = self._gain
            if len(y) == 0:
                outdata[:] = 0.0
                return
            end = pos + frames
            if end <= len(y):
                chunk = y[pos:end]
                self._position = end
            else:
                tail = y[pos:]
                need = frames - len(tail)
                chunk = np.concatenate([tail, y[:need]])
                self._position = need
        outdata[:, 0] = chunk * gain
