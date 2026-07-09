"""SampleManager — convenience wrapper around SampleLayer + SampleModulator + SamplePlayer.

Exposes a small API for load/stop/state/mapping/player that can be wired into the
web dashboard and CLI.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from pythonosc.udp_client import SimpleUDPClient

from .sample_layer import SampleDescriptor, SampleLayer
from .sample_modulator import ModulationTarget, SampleModulator, VALID_DESCRIPTORS
from .sample_player import SamplePlayer
from .state import VoiceParameterStore

log = logging.getLogger(__name__)


class SampleManager:
    """Manage a single loopable sample as a control/ratio source."""

    def __init__(self, store: VoiceParameterStore, sc_host: str = "127.0.0.1", sc_port: int = 57120):
        self.store = store
        self.sc_host = sc_host
        self.sc_port = sc_port
        self.sc_osc = SimpleUDPClient(sc_host, sc_port)
        self.player = SamplePlayer(sc_host=sc_host, sc_port=sc_port)
        self.layer: Optional[SampleLayer] = None
        self.modulator: Optional[SampleModulator] = None
        self.current_path: Optional[str] = None

        self._presets_dir = Path.home() / "Music" / "digital-beacon-mapping-presets"
        self._presets_dir.mkdir(parents=True, exist_ok=True)

    def reset_audio(self) -> None:
        """Reset beacon and shaper audio parameters to defaults before switching presets."""
        self.sc_osc.send_message("/beacon/reset", [])
        # Reset vsrate before f1 so f1 ends up at the default value
        self.store.set_vsrate(1.0)
        self.store.update_f1(40.4)
        self.store.set_master_gain(0.0)
        for i in range(1, 33):
            self.sc_osc.send_message(f"/beacon/gain/{i}", [0.8])
            self.sc_osc.send_message(f"/beacon/az/{i}", [0.0])
            self.sc_osc.send_message(f"/beacon/on/{i}", [1.0])
            self.sc_osc.send_message(f"/beacon/q/{i}", [0.5])
            self.sc_osc.send_message(f"/beacon/dist/{i}", [1.0])
        log.info("SampleManager audio reset")

    def load(self, path: str, sr: int = 48000, chunk_s: float = 0.05,
             f0_beacon_hz: float = 40.4, default_mapping: bool = False) -> None:
        """Load a sample and start analysis loop. Modulation is off by default."""
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
        self.player.load(str(resolved))
        self.player.play()
        # Reset audio after the layer starts so we begin from a clean default state
        self.reset_audio()
        log.info("SampleManager loaded: %s (modulation=%s)", self.current_path, default_mapping)

    def _ensure_modulator(self) -> None:
        """Create a modulator if missing. Called before applying a preset/mapping."""
        if self.modulator is None:
            self.modulator = SampleModulator(self.store, self.sc_host, self.sc_port)
            if self.layer is not None:
                self.layer.on_descriptor = self.modulator.on_descriptor

    def _set_empty_mapping(self) -> None:
        """Ensure the modulator exists and has no targets."""
        self._ensure_modulator()
        assert self.modulator is not None
        self.modulator.set_targets([])

    def stop(self) -> None:
        if self.layer is not None:
            self.layer.stop()
            self.layer = None
        self.player.stop()
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
        self._ensure_modulator()
        assert self.modulator is not None
        targets = [t for t in targets if t.get("descriptor") in VALID_DESCRIPTORS]
        self.modulator.mapping_from_dict(targets)
        log.info("SampleManager mapping updated: %d targets", len(targets))

    def apply_preset(self, name: str) -> None:
        """Apply a named mapping preset (built-in or user-saved), replacing any existing mapping."""
        self._ensure_modulator()
        assert self.modulator is not None
        # Always clear current mapping first so no in-flight descriptor uses old targets
        self.modulator.set_targets([])
        # Reset audio state before loading the new preset
        self.reset_audio()
        # Try user-saved first
        preset_path = self._presets_dir / f"{name}.json"
        if preset_path.exists():
            data = json.loads(preset_path.read_text())
            self.set_mapping(data)
            log.info("SampleManager loaded user preset: %s", name)
            return
        # Fall back to built-in preset
        self.modulator.preset_mapping(name)
        log.info("SampleManager loaded built-in preset: %s", name)

    def clear_mapping(self) -> None:
        """Remove all active modulation targets."""
        self._ensure_modulator()
        assert self.modulator is not None
        self.modulator.set_targets([])
        log.info("SampleManager mapping cleared")

    def set_player_gain(self, gain: float) -> None:
        """Set gain (0..1) of the sample player loop."""
        self.player.set_gain(gain)

    def get_player_gain(self) -> float:
        return self.player.get_gain()

    def player_state(self) -> Dict[str, Any]:
        return {"playing": self.player.is_playing(), "gain": self.player.get_gain()}

    def save_preset(self, name: str) -> None:
        """Save current mapping as a user preset."""
        self._ensure_modulator()
        assert self.modulator is not None
        preset_path = self._presets_dir / f"{name}.json"
        preset_path.write_text(json.dumps(self.modulator.mapping_to_dict(), indent=2))
        log.info("SampleManager saved preset: %s", name)

    def list_presets(self) -> List[str]:
        """List built-in + user mapping presets."""
        built_ins = [
            "default", "tune-to-sample", "spectrum-projection", "timbre-filter",
            "rhythmic-pump", "phase-manifold-tune", "consonance-gate", "harmonic-projection",
        ]
        user_presets = [p.stem for p in self._presets_dir.glob("*.json")]
        return sorted(set(built_ins + user_presets))

    def list_targets(self) -> List[Dict]:
        self._ensure_modulator()
        assert self.modulator is not None
        return self.modulator.mapping_to_dict()

    def default_mapping(self) -> None:
        self._ensure_modulator()
        assert self.modulator is not None
        self.modulator.default_mapping()
