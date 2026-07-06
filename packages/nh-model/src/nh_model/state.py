from __future__ import annotations

import copy
from dataclasses import dataclass, field as dc_field
from typing import Any, Dict, Optional

from nh_core import HarmonicField


@dataclass
class ModelState:
    """Runtime state of a harmonic field with local modulations."""
    base_field: HarmonicField = dc_field(default_factory=HarmonicField)
    # Local modulation accumulators (not stored in base_field)
    master_gain: float = 1.0
    f1_offset: float = 0.0
    partial_gain_offsets: Dict[int, float] = dc_field(default_factory=dict)
    spatial_rotation: float = 0.0
    residual_mix: float = 1.0

    def update_from_base_field(self, base_field: HarmonicField) -> None:
        """Replace the base field, keeping current modulation values."""
        self.base_field = copy.deepcopy(base_field)

    def apply_control(self, event: Dict[str, Any]) -> None:
        """Apply a normalized control event."""
        etype = event.get("type")
        value = event.get("value", 0.0)
        if etype == "master":
            self.master_gain = float(value)
        elif etype == "f1_offset":
            self.f1_offset = float(value)
        elif etype == "partial_gain":
            n = int(value.get("n"))
            self.partial_gain_offsets[n] = float(value.get("gain", 1.0))
        elif etype == "spatial_rotation":
            self.spatial_rotation = float(value)
        elif etype == "residual_mix":
            self.residual_mix = float(value)
        elif etype == "panic":
            self.reset_modulations()

    def apply_sensor(self, event: Dict[str, Any], mapping: Optional[Dict[str, Any]] = None) -> None:
        """Apply a normalized sensor event using a mapping graph."""
        if mapping is None:
            return
        etype = event.get("type")
        cfg = mapping.get(etype)
        if cfg is None:
            return
        raw = float(event.get("value", 0.0))
        scaled = raw * cfg.get("scale", 1.0) + cfg.get("offset", 0.0)
        param = cfg.get("param")
        if param == "master_gain":
            self.master_gain = scaled
        elif param == "f1_offset":
            self.f1_offset = scaled
        elif param == "spatial_rotation":
            self.spatial_rotation = scaled
        elif param == "residual_mix":
            self.residual_mix = scaled
        elif param == "partial_gain":
            n = cfg.get("n", 1)
            self.partial_gain_offsets[n] = scaled

    def reset_modulations(self) -> None:
        self.master_gain = 1.0
        self.f1_offset = 0.0
        self.partial_gain_offsets.clear()
        self.spatial_rotation = 0.0
        self.residual_mix = 1.0

    def to_snapshot(self) -> HarmonicField:
        """Return a thread-safe snapshot with modulations applied."""
        field = copy.deepcopy(self.base_field)
        field.f1 += self.f1_offset
        for n, partial in field.partials.items():
            partial.gain *= self.master_gain * self.partial_gain_offsets.get(n, 1.0)
            if partial.spatial and "az" in partial.spatial:
                partial.spatial["az"] = (partial.spatial["az"] + self.spatial_rotation) % 360.0
        return field

    def from_snapshot(self, snapshot: HarmonicField) -> None:
        """Restore base field from a snapshot (modulations are reset)."""
        self.base_field = copy.deepcopy(snapshot)
        self.reset_modulations()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "base_field": self.base_field.to_dict(),
            "master_gain": self.master_gain,
            "f1_offset": self.f1_offset,
            "partial_gain_offsets": self.partial_gain_offsets,
            "spatial_rotation": self.spatial_rotation,
            "residual_mix": self.residual_mix,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ModelState":
        return cls(
            base_field=HarmonicField.from_dict(d.get("base_field", {})),
            master_gain=d.get("master_gain", 1.0),
            f1_offset=d.get("f1_offset", 0.0),
            partial_gain_offsets=d.get("partial_gain_offsets", {}),
            spatial_rotation=d.get("spatial_rotation", 0.0),
            residual_mix=d.get("residual_mix", 1.0),
        )
