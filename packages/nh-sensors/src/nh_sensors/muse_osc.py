from __future__ import annotations

from typing import Callable, Dict, Optional
from pythonosc.dispatcher import Dispatcher
from pythonosc.osc_server import BlockingOSCUDPServer
import threading


class MuseOSCAdapter:
    """Receives Muse / Mind Monitor OSC and emits SensorEvents."""

    def __init__(self, ip: str = "0.0.0.0", port: int = 5000,
                 callback: Optional[Callable[[Dict[str, object]], None]] = None):
        self.ip = ip
        self.port = port
        self.callback = callback
        self._channels: Dict[str, list] = {"TP9": [], "AF7": [], "AF8": [], "TP10": []}
        self._server: Optional[BlockingOSCUDPServer] = None
        self._thread: Optional[threading.Thread] = None

    def _on_eeg(self, address, *args):
        if len(args) < 4:
            return
        for ch, val in zip(["TP9", "AF7", "AF8", "TP10"], args[:4]):
            self._channels[ch].append(float(val))
            # keep bounded window
            if len(self._channels[ch]) > 4096:
                self._channels[ch] = self._channels[ch][-2048:]

    def _on_elements(self, address, *args):
        # /muse/elements/alpha_absolute etc.
        parts = address.split("/")
        if len(parts) < 4:
            return
        band = parts[-1].split("_")[0]
        ev = {
            "timestamp": None,
            "type": f"eeg.band_power.{band}",
            "value": {ch: float(v) for ch, v in zip(["TP9", "AF7", "AF8", "TP10"], args[:4])},
            "confidence": 1.0,
            "rate": 10.0,
            "units": "absolute_power_db",
        }
        if self.callback:
            self.callback(ev)

    def start(self):
        dispatcher = Dispatcher()
        dispatcher.map("/muse/eeg", self._on_eeg)
        dispatcher.map("/muse/elements/alpha_absolute", self._on_elements)
        dispatcher.map("/muse/elements/beta_absolute", self._on_elements)
        dispatcher.map("/muse/elements/theta_absolute", self._on_elements)
        dispatcher.map("/muse/elements/gamma_absolute", self._on_elements)
        dispatcher.map("/muse/elements/delta_absolute", self._on_elements)
        self._server = BlockingOSCUDPServer((self.ip, self.port), dispatcher)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self):
        if self._server:
            self._server.shutdown()
        if self._thread:
            self._thread.join(timeout=1.0)

    def get_channels(self) -> Dict[str, list]:
        return {k: list(v) for k, v in self._channels.items()}
