"""Renderer capability profiles."""
from dataclasses import dataclass
from typing import Optional


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

    def __post_init__(self):
        if self.spatial_mode not in ("none", "hrtf", "ambisonic"):
            raise ValueError(f"Invalid spatial_mode: {self.spatial_mode}")

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
        }

    @classmethod
    def from_dict(cls, d):
        return cls(**d)
