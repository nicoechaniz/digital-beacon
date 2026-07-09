"""Main entry point for the digital-beacon Shaper.

Launches:
  - VoiceParameterStore
  - AudioEngine (sounddevice, sines)
  - ShaperOSCReceiver (port 9001 for NH broadcasts + 9002 for direct /digital/*)
  - LaunchpadMiniControl (MIDI)
  - Minilab3Control (optional, MIDI)

Use:
  python -m digital_beacon.main            # default
  python -m digital_beacon.main --list-midi # show MIDI ports
  python -m digital_beacon.main --no-midi   # disable MIDI
  python -m digital_beacon.main --no-osc    # disable OSC
"""

import argparse
import logging
import signal
import sys
import time

from .state import VoiceParameterStore
from .audio_engine import AudioEngine
from .osc_receiver import ShaperOSCReceiver
from .midi_control import LaunchpadMiniControl
from .recorder import Recorder
from . import config
from .sample_manager import SampleManager

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL),
    format="%(asctime)s | %(levelName)-7s | %(name)s | %(message)s".replace("levelName", "levelname"),
)
log = logging.getLogger("digital_beacon.main")


def list_midi_ports() -> None:
    try:
        import mido
        print("MIDI input ports:")
        for name in mido.get_input_names():
            print(f"  - {name}")
        print("MIDI output ports:")
        for name in mido.get_output_names():
            print(f"  - {name}")
    except ImportError:
        print("mido not installed")


def main() -> None:
    parser = argparse.ArgumentParser(description="digital-beacon Shaper")
    parser.add_argument("--list-midi", action="store_true", help="List MIDI ports and exit")
    parser.add_argument("--list-devices", action="store_true", help="List audio devices and exit")
    parser.add_argument("--device", type=str, help="Audio device ID or substring")
    parser.add_argument("--no-midi", action="store_true", help="Disable MIDI control")
    parser.add_argument("--no-osc", action="store_true", help="Disable OSC receivers")
    parser.add_argument("--no-api", action="store_true", help="Disable web dashboard (default: on)")
    parser.add_argument("--api-host", type=str, default="127.0.0.1", help="Dashboard bind host")
    parser.add_argument("--api-port", type=int, default=8080, help="Dashboard port")
    parser.add_argument("--sample", type=str, default=None, help="Path to loopable sample for ratio modulation")
    args = parser.parse_args()

    if args.list_midi:
        list_midi_ports()
        return

    log.info("Starting digital-beacon Shaper (f1=%.1f Hz, %d bands, polyphony=%d)...",
             config.DEFAULT_F1, config.N_BANDS, config.MAX_VOICES)

    store = VoiceParameterStore()

    audio = AudioEngine(
        store,
        device=args.device or config.AUDIO_DEVICE,
    )
    if args.list_devices:
        print(audio.list_devices())
        return

    osc = ShaperOSCReceiver(store)

    sample_manager = SampleManager(store, sc_host=config.SCLANG_HOST, sc_port=config.SCLANG_OSC_PORT)
    if args.sample:
        try:
            sample_manager.load(args.sample)
        except Exception as e:
            log.warning("Could not load sample %s: %s", args.sample, e)

    launchpad = LaunchpadMiniControl(store)
    store._panic_callback = launchpad.panic

    def _shutdown(signum, frame):
        log.info("Signal %d — shutting down", signum)
        store.panic()
        audio.stop()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGHUP, _shutdown)

    audio.start()

    if not args.no_osc:
        osc.start()

    if not args.no_midi:
        launchpad.start()

    # Web dashboard (FastAPI in a thread)
    api_thread = None
    if not args.no_api:
        from .api import create_app
        from pythonosc.udp_client import SimpleUDPClient
        import threading
        import uvicorn
        # The Recorder needs both the AudioEngine (for the Shaper tap)
        # and an OSC client to tell sclang to start/stop its own Server.record.
        sc_osc_for_rec = SimpleUDPClient(config.SCLANG_HOST, config.SCLANG_OSC_PORT)
        recorder = Recorder(store, audio, sc_osc_for_rec)
        app = create_app(store, recorder=recorder, sample_manager=sample_manager)
        config_uvicorn = uvicorn.Config(app, host=args.api_host, port=args.api_port,
                                        log_level="warning", access_log=False)
        server_uvicorn = uvicorn.Server(config_uvicorn)
        api_thread = threading.Thread(target=server_uvicorn.run, name="shaper-api", daemon=True)
        api_thread.start()
        log.info("Web dashboard on http://%s:%d  (also try http://<lan-ip>:%d)",
                 args.api_host, args.api_port, args.api_port)

    log.info("Shaper running. Press Ctrl-C to stop.")
    log.info("Listening:")
    if not args.no_osc:
        log.info("  OSC :%d  (NH broadcast: /beacon/voice/* /beacon/f1)",
                 config.BEACON_BROADCAST_PORT)
        log.info("  OSC :%d  (direct: /digital/*)", config.SHAPER_OSC_PORT)
    if not args.no_midi:
        log.info("  MIDI Launchpad Mini (auto-detect)")
    if not args.no_api:
        log.info("  Web  http://%s:%d", args.api_host, args.api_port)

    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        log.info("KeyboardInterrupt — shutting down...")
    finally:
        if not args.no_midi:
            launchpad.stop()
        if not args.no_osc:
            osc.stop()
        audio.stop()
        sample_manager.stop()
        log.info("Shaper stopped.")


if __name__ == "__main__":
    main()

