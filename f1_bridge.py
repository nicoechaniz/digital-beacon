"""f1_bridge.py — forwards /beacon/f1 to /beacon/vsource.

Listens on port 9001 (NaturalHarmony broadcasts /beacon/f1 there when CC74
moves in NH, or any other source that emits it). Computes the varispeed
rate as f1 / DEFAULT_F1 and sends /beacon/vsource [rate] to sclang :57120.

This is what makes "the beacon field follows f1" work without any change
to NaturalHarmony.

Today (2026-06-22) f1 stays at DEFAULT_F1 so the bridge is a no-op
relay (it still forwards, just with rate=1.0). When the 12-point discrete
modulation lands, this becomes the actual modulation path.
"""

import logging
import signal
import socket
import sys
import time

from pythonosc import dispatcher as osc_dispatcher
from pythonosc.osc_server import BlockingOSCUDPServer
from pythonosc.udp_client import SimpleUDPClient

from digital_beacon import config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | f1_bridge | %(message)s",
)
log = logging.getLogger("f1_bridge")


class _ReusePortOSCServer(BlockingOSCUDPServer):
    """BlockingOSCUDPServer with SO_REUSEPORT — co-listens with the Shaper
    (and the NH visualizer) on port 9001."""
    def server_bind(self):
        try:
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except (AttributeError, OSError):
            log.warning("SO_REUSEPORT unavailable on 9001")
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        super().server_bind()


class F1Bridge:
    def __init__(self):
        self._client = SimpleUDPClient(config.SCLANG_HOST, config.SCLANG_OSC_PORT)
        self._current_rate = 1.0
        self._current_f1 = config.DEFAULT_F1

    def on_f1(self, addr, f1, *_):
        try:
            f1 = float(f1)
        except (TypeError, ValueError):
            log.warning("Bad f1 value: %r", f1)
            return
        f1 = max(config.F1_MIN, min(config.F1_MAX, f1))
        rate = f1 / config.DEFAULT_F1
        # Clamp to a sane range so we don't get insane varispeeds
        rate = max(0.1, min(4.0, rate))
        if abs(rate - self._current_rate) < 0.001 and abs(f1 - self._current_f1) < 0.01:
            return  # no change
        self._current_rate = rate
        self._current_f1 = f1
        self._client.send_message("/beacon/vsource", [float(rate)])
        # Also push /beacon/f1 to sclang so the SC band centers retune
        self._client.send_message("/beacon/f1", [float(f1)])
        log.info("f1=%.2f Hz -> vsrate=%.4f", f1, rate)

    def on_voice_on(self, addr, *args):
        # Forward the voice_on message too — sclang doesn't listen on 9001
        # directly, but this hook lets us add processing later.
        pass

    def start(self, host: str = config.OSC_HOST, port: int = config.BEACON_BROADCAST_PORT):
        d = osc_dispatcher.Dispatcher()
        d.map("/beacon/f1", self.on_f1)
        d.set_default_handler(lambda *_: None)
        try:
            server = _ReusePortOSCServer((host, port), d)
        except OSError as exc:
            log.error("Cannot bind %s:%d: %s", host, port, exc)
            sys.exit(1)
        log.info("f1_bridge listening on %s:%d  ->  sclang %s:%d",
                 host, port, config.SCLANG_HOST, config.SCLANG_OSC_PORT)
        log.info("DEFAULT_F1=%.2f Hz. Send /beacon/f1 [hz] to modulate.", config.DEFAULT_F1)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.shutdown()
            log.info("f1_bridge stopped.")


def main():
    bridge = F1Bridge()

    def _sigint(signum, frame):
        log.info("Signal %d — stopping", signum)
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _sigint)
    signal.signal(signal.SIGHUP, _sigint)
    bridge.start()


if __name__ == "__main__":
    main()
