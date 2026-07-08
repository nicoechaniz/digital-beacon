from typing import Any, Dict, List, Optional
from nh_core import HarmonicField, RendererCapabilities
from nh_renderers.renderer import Renderer


class CompositeRenderer(Renderer):
    """Renderer that forwards a snapshot to multiple renderers.

    Useful for running the local Python sounddevice renderer while also
    driving an external SuperCollider/OSC beacon engine.
    """

    def __init__(self, renderers: List[Renderer], name: str = "composite"):
        self.renderers = renderers
        self.name = name
        self._running = False

    def start(self) -> None:
        for renderer in self.renderers:
            renderer.start()
        self._running = True

    def stop(self) -> None:
        for renderer in self.renderers:
            renderer.stop()
        self._running = False

    def render(self, field: HarmonicField, transport: Optional[Dict[str, Any]] = None) -> None:
        for renderer in self.renderers:
            try:
                renderer.render(field, transport)
            except Exception:
                pass

    @property
    def is_running(self) -> bool:
        return all(renderer.is_running for renderer in self.renderers)

    def get_capabilities(self) -> RendererCapabilities:
        if not self.renderers:
            return RendererCapabilities()
        # Return the capabilities of the primary (local) renderer.
        return self.renderers[0].get_capabilities()
