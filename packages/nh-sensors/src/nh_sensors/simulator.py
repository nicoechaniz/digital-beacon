import numpy as np
import time
from typing import Callable, Dict, Optional


class EEGSimulator:
    """Generate synthetic EEG-like sensor events for testing."""

    def __init__(self, callback: Optional[Callable[[Dict[str, object]], None]] = None,
                 rate_hz: float = 10.0):
        self.callback = callback
        self.rate_hz = rate_hz
        self._running = False

    def _generate_focus(self) -> Dict[str, object]:
        # Brownian-ish focus value 0-1
        value = float(np.clip(np.random.normal(0.5, 0.2), 0.0, 1.0))
        return {
            "timestamp": time.time(),
            "type": "eeg.focus",
            "value": value,
            "confidence": 0.85,
            "rate": self.rate_hz,
            "units": "normalized_0_1",
        }

    def emit(self) -> Dict[str, object]:
        ev = self._generate_focus()
        if self.callback:
            self.callback(ev)
        return ev

    def run(self, duration_s: float = 10.0) -> None:
        self._running = True
        start = time.time()
        while self._running and time.time() - start < duration_s:
            self.emit()
            time.sleep(1.0 / self.rate_hz)

    def stop(self) -> None:
        self._running = False
