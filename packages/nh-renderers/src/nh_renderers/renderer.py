from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict

from nh_core import HarmonicField, RendererCapabilities


class Renderer(ABC):
    """Abstract renderer interface."""

    @abstractmethod
    def start(self) -> None:
        ...

    @abstractmethod
    def stop(self) -> None:
        ...

    @abstractmethod
    def render(self, field: HarmonicField, transport: Dict[str, Any] = None) -> None:
        """Render one frame of the harmonic field."""
        ...

    @property
    @abstractmethod
    def is_running(self) -> bool:
        ...

    @abstractmethod
    def get_capabilities(self) -> RendererCapabilities:
        """Return the capability profile for this renderer."""
        ...
