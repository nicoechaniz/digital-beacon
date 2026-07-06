import time
from typing import Any, Dict, Optional

from nh_core import HarmonicField
from nh_renderers.renderer import Renderer
from pythonosc.udp_client import SimpleUDPClient


class SuperColliderOSCAdapter(Renderer):
    """Renderer adapter that sends harmonic field snapshots to SuperCollider/ATK via OSC.

    Addresses match beacon-spatial and digital-beacon conventions:
    - /beacon/f1 <f>
    - /beacon/gain/<n> <g>
    - /beacon/az/<n> <a>
    - /beacon/dist/<n> <d>
    - /beacon/on/<n> <0/1>
    - /beacon/master <m>
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 57120, max_partials: int = 32):
        self.host = host
        self.port = port
        self.max_partials = max_partials
        self._client = None
        self._running = False

    def start(self) -> None:
        self._client = SimpleUDPClient(self.host, self.port)
        self._running = True

    def stop(self) -> None:
        self._client = None
        self._running = False

    def render(self, field: HarmonicField, transport: Dict[str, Any] = None) -> None:
        if self._client is None:
            return
        self._client.send_message("/beacon/f1", [float(field.f1)])
        for n in range(1, self.max_partials + 1):
            partial = field.partials.get(n)
            if partial is None:
                self._client.send_message(f"/beacon/gain/{n}", [0.0])
                self._client.send_message(f"/beacon/on/{n}", [0])
                continue
            spatial = partial.spatial or {}
            self._client.send_message(f"/beacon/gain/{n}", [float(partial.gain)])
            self._client.send_message(f"/beacon/az/{n}", [float(spatial.get("az", 0.0))])
            self._client.send_message(f"/beacon/dist/{n}", [float(spatial.get("dist", 1.0))])
            self._client.send_message(f"/beacon/on/{n}", [1 if spatial.get("on", True) else 0])

    @property
    def is_running(self) -> bool:
        return self._running
