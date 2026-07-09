"""SamplePlayer — route the loaded sample through SuperCollider as a mix layer.

Instead of opening a separate sounddevice stream, we send the sample path to the
SuperCollider beacon engine via OSC. The sample is played by the `sample_player`
synth in beacon.scd and mixed with the beacon output on the R24.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from pythonosc.udp_client import SimpleUDPClient

log = logging.getLogger(__name__)


class SamplePlayer:
    """Control a sample playback layer in SuperCollider via OSC."""

    def __init__(self, sc_host: str = "127.0.0.1", sc_port: int = 57120):
        self.sc_osc = SimpleUDPClient(sc_host, sc_port)
        self._current_path: Optional[str] = None
        self._gain: float = 0.0

    def load(self, path: str) -> bool:
        resolved = str(Path(path).expanduser())
        self._current_path = resolved
        self.sc_osc.send_message("/beacon/sample/load", [resolved])
        log.info("SamplePlayer sent load to SC: %s", resolved)
        return True

    def set_gain(self, gain: float) -> None:
        self._gain = max(0.0, min(1.0, float(gain)))
        self.sc_osc.send_message("/beacon/sample/gain", [self._gain])
        log.debug("SamplePlayer gain sent: %.3f", self._gain)

    def get_gain(self) -> float:
        return self._gain

    def play(self) -> bool:
        # Playback is started by /beacon/sample/load once the buffer is ready.
        return True

    def stop(self) -> None:
        self.sc_osc.send_message("/beacon/sample/stop", [])
        self._current_path = None
        log.info("SamplePlayer stopped")

    def is_playing(self) -> bool:
        return self._current_path is not None
