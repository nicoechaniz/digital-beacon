"""End-to-end audio pipeline test using alsa loopback.

Requires snd-aloop loaded:  sudo modprobe snd-aloop
The server is started with NH_DEVICE=9 (loopback playback hw:4,0).
Audio played by the Python renderer routes through the loopback to
hw:4,1 capture device (must use FLOAT_LE stereo).
"""

import asyncio
import json
import os
import signal
import subprocess
import sys
import time

import numpy as np
import pytest
import requests
import soundfile as sf
import websockets

pytestmark = pytest.mark.skipif(
    os.getenv("NH_RUN_HARDWARE_E2E") != "1",
    reason="hardware/ALSA loopback e2e; set NH_RUN_HARDWARE_E2E=1 to run",
)

SERVER_URL = "http://127.0.0.1:8080"
SR = 48000
CAPTURE_FILE = "/tmp/nh_pytest_capture.wav"


def _wait_for_server(timeout: float = 15) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            r = requests.get(f"{SERVER_URL}/nh/v1/presets", timeout=3)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


@pytest.fixture(scope="module")
def server_with_loopback():
    """Start the nh-ui server with loopback audio device."""
    env = os.environ.copy()
    env["NH_DEVICE"] = "9"

    proc = subprocess.Popen(
        [sys.executable, "-m", "nh_ui.main"],
        cwd="/home/nicolas/Projects/digital-beacon",
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    if not _wait_for_server(20):
        proc.kill()
        out = proc.stdout.read(500).decode() if proc.stdout else ""
        pytest.fail(f"Server did not start within 20s. Output: {out}")

    yield proc

    proc.send_signal(signal.SIGINT)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def _record_loopback(duration: float) -> np.ndarray:
    """Record from loopback capture using arecord."""
    subprocess.run(
        ["arecord", "-D", "hw:4,1", "-f", "FLOAT_LE", "-r", str(SR),
         "-c", "2", "-d", str(int(duration)), CAPTURE_FILE],
        capture_output=True, timeout=int(duration) + 5
    )
    if not os.path.exists(CAPTURE_FILE):
        return np.array([])
    audio, _ = sf.read(CAPTURE_FILE)
    return audio.mean(axis=1)  # stereo -> mono


def _analyze(audio: np.ndarray, expected_f0: float) -> dict:
    if len(audio) == 0:
        return {"rms": 0.0, "dominant_freq": 0.0, "match": False}
    rms = float(np.sqrt(np.mean(audio ** 2)))
    spec = np.abs(np.fft.rfft(audio))
    freqs = np.fft.rfftfreq(len(audio), 1 / SR)
    peak_idx = np.argmax(spec[1:]) + 1
    dominant = float(freqs[peak_idx])
    match = any(abs(dominant - expected_f0 * h) < expected_f0 * h * 0.03 for h in range(1, 9))
    return {"rms": rms, "dominant_freq": dominant, "match": match}


@pytest.mark.asyncio
async def test_preset_load_and_audio(server_with_loopback):
    """Load a preset, raise master, verify audio contains expected frequency."""
    # Load preset
    preset_id = "beacon_spatial__3 jun"
    resp = requests.post(f"{SERVER_URL}/nh/v1/presets/{preset_id}/load")
    assert resp.status_code == 200
    expected_f1 = resp.json()["f1"]

    # Connect to runtime WS, drain buffered, send master, record
    async with websockets.connect("ws://127.0.0.1:8765/") as ws:
        # Drain initial messages (fewer now since master is already at 0.6)
        for _ in range(5):
            await asyncio.wait_for(ws.recv(), timeout=2)

        # Set master gain
        await ws.send(json.dumps({
            "type": "control_event",
            "payload": {"type": "master", "value": 0.8}
        }))
        await asyncio.sleep(0.5)

        # Record 2s of audio
        audio = _record_loopback(2.0)

    result = _analyze(audio, expected_f1)
    assert result["rms"] > 0.01, f"Audio too quiet (RMS={result['rms']:.5f})"
    assert result["match"], f"Expected f0 near {expected_f1} Hz, got {result['dominant_freq']:.1f} Hz"
