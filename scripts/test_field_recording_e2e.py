#!/usr/bin/env python3
"""E2E test: load a Costa Rica field recording and verify modulation.

This script runs the digital_beacon API in-process via FastAPI TestClient,
loads a real field recording, applies the 'tune-to-sample' mapping preset,
and checks that the sample drives the beacon/shaper descriptors.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from digital_beacon.api import create_app
from digital_beacon.sample_manager import SampleManager
from digital_beacon.state import VoiceParameterStore
from fastapi.testclient import TestClient


def test_field_recording_modulation():
    store = VoiceParameterStore()
    sm = SampleManager(store)
    app = create_app(store, sample_manager=sm)
    client = TestClient(app)

    path = "/home/nicolas/Music/field-recordings/wav/07-03-2026 16.40.wav"
    r = client.post("/api/sample/load", json={"path": path})
    assert r.status_code == 200 and r.json()["ok"], r.json()

    r = client.post("/api/sample/preset", json={"name": "tune-to-sample"})
    assert r.status_code == 200 and r.json()["ok"], r.json()

    time.sleep(2.0)

    r = client.get("/api/sample/state")
    state = r.json()
    assert state["running"], "sample layer not running"
    assert state["descriptor"]["f0_hz"] > 0, "f0 not detected"
    assert store.f1 > 40.0, "shaper f1 not retuned by sample"
    print("E2E OK:")
    print("  f0 detected:", state["descriptor"]["f0_hz"])
    print("  shaper f1:", store.f1)
    print("  targets:", len(state["targets"]))


if __name__ == "__main__":
    test_field_recording_modulation()
