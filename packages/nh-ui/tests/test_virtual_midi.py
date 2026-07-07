"""Test Launchpad via virtual MIDI.

Requires: snd-virmidi loaded (sudo modprobe snd-virmidi).
Server must be started with NH_LAUNCHPAD_PORT=VirMIDI.
"""

import asyncio, json, os, signal, subprocess, sys, time
import pytest, requests, websockets

SERVER_URL = "http://127.0.0.1:8080"
RUNTIME_WS = "ws://127.0.0.1:8765/"


def _wait_for_server(timeout=15):
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
def server_with_virtual_midi():
    env = os.environ.copy()
    env["NH_LAUNCHPAD_PORT"] = "VirMIDI"

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
        pytest.fail(f"Server did not start. Output: {out}")

    yield proc

    proc.send_signal(signal.SIGINT)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


@pytest.mark.asyncio
async def test_virtual_midi_pad_updates_field(server_with_virtual_midi):
    """Sending a MIDI note to the virtual port updates the field."""
    # Load a preset so the base field has partials
    r = requests.post(f"{SERVER_URL}/nh/v1/presets/beacon_spatial__3 jun/load")
    assert r.status_code == 200

    # Send MIDI note to the virtual port
    subprocess.run(
        ["amidi", "-p", "hw:5,0,0", "-S", "90 70 7F"],
        capture_output=True, timeout=5
    )
    await asyncio.sleep(0.2)
    subprocess.run(
        ["amidi", "-p", "hw:5,0,0", "-S", "80 70 00"],
        capture_output=True, timeout=5
    )

    # Verify field has non-zero gains
    async with websockets.connect(RUNTIME_WS) as ws:
        for _ in range(5):
            await asyncio.wait_for(ws.recv(), timeout=1)
        await ws.send(json.dumps({
            "type": "control_event",
            "payload": {"type": "master", "value": 0.8}
        }))
        await asyncio.sleep(0.3)
        for _ in range(5):
            raw = await asyncio.wait_for(ws.recv(), timeout=2)
            data = json.loads(raw)
            if data.get("type") == "base_field":
                p1 = data.get("payload", {}).get("partials", {}).get("1", {})
                gain = p1.get("gain", 0) if isinstance(p1, dict) else 0
                assert gain > 0, f"Expected partial 1 gain > 0, got {gain}"
                return
    pytest.fail("No base_field received with non-zero gains")
