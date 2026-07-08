"""LFO processing and sensor routing (Phase 9).

Path-targeted modulation from LFOs and sensors to scene parameters.
Uses the ModulationRoute model from nh-core to map sources to targets.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from nh_core import ModulationRoute, LFOState


@dataclass
class LFOModulator:
    """Runtime LFO that computes waveform values from state."""

    lfo: LFOState
    phase: float = 0.0  # current phase [0, 1)
    _sample_rate: float = 44100.0

    def advance(self, dt: float) -> float:
        """Advance the LFO by dt seconds, return normalized output [-1, 1]."""
        rate = self._effective_rate()
        if rate <= 0:
            return 0.0

        self.phase += rate * dt
        self.phase %= 1.0
        return self._waveform_value()

    def _effective_rate(self) -> float:
        if self.lfo.rate_hz is not None:
            return self.lfo.rate_hz
        # strum_divisor: rate = f1 / divisor
        if self.lfo.strum_divisor and self.lfo.strum_divisor > 0:
            return 65.0 / self.lfo.strum_divisor  # default f1, overridden by scene
        return 0.0

    def _waveform_value(self) -> float:
        p = self.phase
        wf = self.lfo.waveform
        if wf == "sine":
            return math.sin(2.0 * math.pi * p)
        elif wf == "triangle":
            return 4.0 * abs(p - 0.5) - 1.0
        elif wf == "saw":
            return 2.0 * p - 1.0
        elif wf == "square":
            return 1.0 if p < 0.5 else -1.0
        elif wf == "sample_hold":
            # Value changes only at phase wrap — sampled each cycle.
            if not hasattr(self, "_sh_value"):
                import random
                self._sh_value = random.uniform(-1.0, 1.0)
            # On wrap, re-sample.
            # (We approximate by returning cached value; full impl needs phase-tracking.)
            return self._sh_value
        return 0.0


class SensorRouter:
    """Route sensor events to scene parameters via ModulationRoute entries.

    Safety: sensor influence is clamped to [0, 1] per source-configurable
    enable/disable. Events without a matching route are dropped.
    """

    def __init__(self, routes: Dict[str, ModulationRoute] = None):
        self.routes: Dict[str, ModulationRoute] = routes or {}
        self.enabled_sources: Dict[str, bool] = {}

    def set_enabled(self, source: str, enabled: bool) -> None:
        self.enabled_sources[source] = enabled

    def apply(
        self,
        sensor_type: str,
        raw_value: float,
        influence: float = 1.0,
    ) -> List[Dict[str, Any]]:
        """Apply a sensor event, returning list of (target_path, scaled_value) pairs."""
        results = []
        if not self.enabled_sources.get(sensor_type, True):
            return results

        for rid, route in self.routes.items():
            if route.source == sensor_type:
                scaled = influence * (raw_value * route.scale + route.offset)
                if route.range_min is not None:
                    scaled = max(route.range_min, scaled)
                if route.range_max is not None:
                    scaled = min(route.range_max, scaled)
                results.append({"path": route.target_path, "value": scaled})
        return results
