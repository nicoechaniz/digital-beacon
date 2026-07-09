"""SampleManager — convenience wrapper around SampleLayer + SampleModulator.

Exposes a small API for load/stop/state/mapping that can be wired into the
web dashboard and CLI.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, List, Optional

from .sample_layer import SampleDescriptor, SampleLayer
from .sample_modulator import ModulationTarget, SampleModulator
from .state import VoiceParameterStore

log = logging.getLogger(__name__)


class SampleManager:
    """Manage a single loopable sample as a control/ratio source."""

    def __init__(self, store: VoiceParameterStore, sc_host: str = "127.0.0.1", sc_port: int = 57120):
        self.store = store
        self.sc_host = sc_host
        self.sc_port = sc_port
        self.layer: Optional[SampleLayer] = None
        self.modulator: Optional[SampleModulator] = None
        self.current_path: Optional[str] = None

    def load(self, path: str, sr: int = 48000, chunk_s: float = 0.05,
             f0_beacon_hz: float = 40.4, default_mapping: bool = True) -> None:
        """Load a sample, start analysis loop, and optionally install default mapping."""
        self.stop()
        resolved = Path(path).expanduser()
        if not resolved.exists():
            raise FileNotFoundError(f"sample not found: {resolved}")
        self.layer = SampleLayer(
            str(resolved),
            sr=sr,
            chunk_s=chunk_s,
            f0_beacon_hz=f0_beacon_hz,
        )
        self.modulator = SampleModulator(self.store, self.sc_host, self.sc_port)
        self.layer.on_descriptor = self.modulator.on_descriptor
        if default_mapping:
            self.modulator.default_mapping()
        self.layer.start()
        self.current_path = str(resolved)
        log.info("SampleManager loaded: %s", self.current_path)

    def stop(self) -> None:
        if self.layer is not None:
            self.layer.stop()
            self.layer = None
        self.modulator = None
        self.current_path = None
        log.info("SampleManager stopped")

    def is_running(self) -> bool:
        return self.layer is not None and self.layer._running

    def last_descriptor(self) -> Optional[Dict]:
        d = self.layer.last_descriptor() if self.layer else None
        return d.to_dict() if d else None

    def set_mapping(self, targets: List[Dict]) -> None:
        """Replace the current modulation mapping with a list of target dicts."""
        if self.modulator is None:
            raise RuntimeError("no sample loaded")
        self.modulator.remove_targets()
        for t in targets:
            self.modulator.add_target(ModulationTarget(
                descriptor=t["descriptor"],
                target_type=t["target_type"],
                param=t["param"],
                voice=t.get("voice"),
                band=t.get("band"),
                scale=float(t.get("scale", 1.0)),
                offset=float(t.get("offset", 0.0)),
                min_value=float(t.get("min_value", 0.0)),
                max_value=float(t.get("max_value", 1.0)),
                active=bool(t.get("active", True)),
            ))
        log.info("SampleManager mapping updated: %d targets", len(targets))

    def list_targets(self) -> List[Dict]:
        if self.modulator is None:
            return []
        return [
            {
                "descriptor": t.descriptor,
                "target_type": t.target_type,
                "param": t.param,
                "voice": t.voice,
                "band": t.band,
                "scale": t.scale,
                "offset": t.offset,
                "min_value": t.min_value,
                "max_value": t.max_value,
                "active": t.active,
            }
            for t in self.modulator.list_targets()
        ]

    def default_mapping(self) -> None:
        if self.modulator is None:
            raise RuntimeError("no sample loaded")
        self.modulator.default_mapping()
