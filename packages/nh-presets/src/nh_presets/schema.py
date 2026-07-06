from __future__ import annotations

import json
from dataclasses import dataclass, field as dc_field
from typing import Any, Dict, Optional

from nh_core import HarmonicField, Partial, RendererCapabilities, Residual, Transport


@dataclass
class Preset:
    """Canonical v1 preset wrapper around a HarmonicField."""
    version: str = "1"
    harmonic_field: HarmonicField = dc_field(default_factory=HarmonicField)
    renderer_capabilities_required: Optional[RendererCapabilities] = None
    metadata: Dict[str, Any] = dc_field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "harmonic_field": self.harmonic_field.to_dict(),
            "renderer_capabilities_required": (
                self.renderer_capabilities_required.to_dict()
                if self.renderer_capabilities_required else None
            ),
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Preset":
        caps = d.get("renderer_capabilities_required")
        return cls(
            version=d.get("version", "1"),
            harmonic_field=HarmonicField.from_dict(d.get("harmonic_field", {})),
            renderer_capabilities_required=RendererCapabilities.from_dict(caps) if caps else None,
            metadata=d.get("metadata", {}),
        )


def load(path: str) -> Preset:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return Preset.from_dict(data)


def save(preset: Preset, path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(preset.to_dict(), f, indent=2, ensure_ascii=False)
