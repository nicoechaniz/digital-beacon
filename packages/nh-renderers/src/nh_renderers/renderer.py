from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict

from nh_core import HarmonicField, HarmonicScene, RendererCapabilities


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

    def render_scene(self, scene: HarmonicScene, transport: Dict[str, Any] = None) -> None:
        """Render a multi-source scene. Default: project to field and render."""
        field = scene.project_to_base_field()
        self.render(field, transport)

    @property
    @abstractmethod
    def is_running(self) -> bool:
        ...

    @abstractmethod
    def get_capabilities(self) -> RendererCapabilities:
        """Return the capability profile for this renderer."""
        ...

    def supports_scene(self) -> bool:
        """Whether this renderer natively supports multi-source scenes."""
        return False

    def supports_sources(self) -> bool:
        """Whether this renderer handles sources independently."""
        return False

    def supports_envelopes(self) -> bool:
        """Whether this renderer handles per-voice envelopes."""
        return False

    def supports_buffers(self) -> bool:
        """Whether this renderer handles sample/audio buffer playback."""
        return False
