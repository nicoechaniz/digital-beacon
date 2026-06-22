"""OSC receiver for digital-beacon Shaper.

Listens on two ports:
  - BEACON_BROADCAST_PORT (9001, SO_REUSEPORT alongside NH visualizer):
      receives /beacon/voice/on|off|freq and /beacon/f1 from NH harmonic_beacon.
  - SHAPER_OSC_PORT (9002):
      receives /digital/* direct control messages (gain/pan/phase per harmonic).

Adapted from NaturalHarmony/harmonic_shaper/osc_receiver.py.
"""

import logging
import socket
import threading
from typing import Optional

try:
    from pythonosc import dispatcher as osc_dispatcher
    from pythonosc import osc_server
    HAS_OSC = True
except ImportError:
    HAS_OSC = False

from .state import VoiceParameterStore
from . import config

log = logging.getLogger(__name__)


class _ReusePortUDPServer(osc_server.BlockingOSCUDPServer if HAS_OSC else object):
    """OSC server with SO_REUSEPORT — co-listen with NH visualizer on 9001."""
    def server_bind(self):
        try:
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except (AttributeError, OSError):
            log.warning("SO_REUSEPORT unavailable — may conflict on port 9001")
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        super().server_bind()


class ShaperOSCReceiver:
    """Dual-port OSC receiver for the Shaper."""

    def __init__(
        self,
        store: VoiceParameterStore,
        beacon_port: int = config.BEACON_BROADCAST_PORT,
        shaper_port: int = config.SHAPER_OSC_PORT,
        host: str = config.OSC_HOST,
    ):
        if not HAS_OSC:
            raise ImportError("python-osc is required.")
        self._store = store
        self._beacon_port = beacon_port
        self._shaper_port = shaper_port
        self._host = host
        self._servers: list = []
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        self._start_beacon_listener()
        self._start_shaper_listener()

    def stop(self) -> None:
        for s in self._servers:
            try:
                s.shutdown()
            except Exception:
                pass

    # ─── Beacon listener (NH broadcasts) ──────────────────────────────────

    def _start_beacon_listener(self) -> None:
        d = osc_dispatcher.Dispatcher()
        d.map("/beacon/voice/on", self._on_voice_on)
        d.map("/beacon/voice/off", self._on_voice_off)
        d.map("/beacon/voice/freq", self._on_voice_freq)
        d.map("/beacon/f1", self._on_f1)
        d.map("/beacon/panic", lambda *_: self._store.panic())
        d.set_default_handler(lambda *_: None)

        try:
            server = _ReusePortUDPServer((self._host, self._beacon_port), d)
        except OSError as exc:
            log.error("Could not bind beacon port %d: %s", self._beacon_port, exc)
            return
        self._servers.append(server)
        t = threading.Thread(target=server.serve_forever,
                             name="shaper-beacon-osc", daemon=True)
        t.start()
        self._threads.append(t)
        log.info("Beacon OSC listener on port %d", self._beacon_port)

    def _on_voice_on(self, addr, voice_id, freq, gain, source_note, harmonic_n=None, *_):
        if harmonic_n is None:
            harmonic_n = int(source_note)
        else:
            harmonic_n = int(harmonic_n)
        self._store.voice_on(harmonic_n, int(voice_id), float(freq), gain=float(gain))
        log.debug("voice_on n=%d freq=%.2f", harmonic_n, freq)

    def _on_voice_off(self, addr, voice_id, *_):
        self._store.voice_off(int(voice_id))

    def _on_voice_freq(self, addr, voice_id, freq, *_):
        self._store.voice_freq(int(voice_id), float(freq))

    def _on_f1(self, addr, f1, *_):
        self._store.update_f1(float(f1))
        log.debug("f1 -> %.2f Hz", f1)

    # ─── Direct shaper control (/digital/*) ───────────────────────────────

    def _start_shaper_listener(self) -> None:
        d = osc_dispatcher.Dispatcher()
        d.map("/digital/harmonic/*/gain", self._on_gain)
        d.map("/digital/harmonic/*/pan", self._on_pan)
        d.map("/digital/harmonic/*/phase", self._on_phase)
        d.map("/digital/master", self._on_master)
        d.map("/digital/panic", lambda *_: self._store.panic())
        d.set_default_handler(lambda *_: None)

        try:
            server = _ReusePortUDPServer((self._host, self._shaper_port), d)
        except OSError as exc:
            log.error("Could not bind shaper port %d: %s", self._shaper_port, exc)
            return
        self._servers.append(server)
        t = threading.Thread(target=server.serve_forever,
                             name="shaper-direct-osc", daemon=True)
        t.start()
        self._threads.append(t)
        log.info("Shaper direct OSC on port %d", self._shaper_port)

    @staticmethod
    def _parse_n(addr: str) -> Optional[int]:
        parts = addr.split("/")
        try:
            return int(parts[3])
        except (IndexError, ValueError):
            return None

    def _on_gain(self, addr, value, *_):
        n = self._parse_n(addr)
        if n is not None:
            self._store.set_gain(n, float(value))

    def _on_pan(self, addr, value, *_):
        n = self._parse_n(addr)
        if n is not None:
            self._store.set_pan(n, float(value))

    def _on_phase(self, addr, value, *_):
        n = self._parse_n(addr)
        if n is not None:
            self._store.set_phase(n, float(value))

    def _on_master(self, addr, value, *_):
        self._store.set_master_gain(float(value))
