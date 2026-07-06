"""Renderer capability profiles."""
from dataclasses import dataclass, field as dc_field
from typing import List, Optional


@dataclass
class RendererCapabilities:
    """What a renderer can do."""
    max_partials: int = 32
    supports_phase: bool = True
    supports_spatial: bool = True
    spatial_mode: str = "none"  # none | hrtf | ambisonic
    supports_residual: bool = True
    max_tines: int = 0
    sample_rate: Optional[float] = None
    block_size: Optional[int] = None
    available_renderers: List[str] = dc_field(default_factory=list)
    default_renderer: Optional[str] = None

    def __post_init__(self):
        if self.spatial_mode not in ("none", "hrtf", "ambisonic"):
            raise ValueError(f"Invalid spatial_mode: {self.spatial_mode}")
        if self.available_renderers and self.default_renderer and self.default_renderer not in self.available_renderers:
            raise ValueError(f"default_renderer {self.default_renderer} not in available_renderers")

    def to_dict(self):
        return {
            "max_partials": self.max_partials,
            "supports_phase": self.supports_phase,
            "supports_spatial": self.supports_spatial,
            "spatial_mode": self.spatial_mode,
            "supports_residual": self.supports_residual,
            "max_tines": self.max_tines,
            "sample_rate": self.sample_rate,
            "block_size": self.block_size,
            "available_renderers": self.available_renderers,
            "default_renderer": self.default_renderer,
        }

    @classmethod
    def from_dict(cls, d):
        return cls(**d)
