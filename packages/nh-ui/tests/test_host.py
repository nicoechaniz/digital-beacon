import pytest
from pathlib import Path
from fastapi.testclient import TestClient
from nh_core import HarmonicField, Partial, RendererCapabilities
from nh_presets import Preset, save
from nh_runtime import BaseFieldServer
from nh_ui.server import PRESETS_DIR, make_app


@pytest.fixture
def runtime():
    r = BaseFieldServer(
        base_field=HarmonicField(f1=65.0),
        renderer_capabilities=RendererCapabilities(max_partials=32, supports_phase=True, supports_spatial=True),
    )
    r.model.update_from_base_field(HarmonicField(f1=65.0))
    return r


@pytest.fixture
def client(runtime):
    app = make_app(runtime)
    return TestClient(app)


def test_root_serves_ui(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "NaturalHarmony UI" in r.text


def test_list_presets(client):
    r = client.get("/nh/v1/presets")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_get_preset_404(client):
    r = client.get("/nh/v1/presets/nonexistent")
    assert r.status_code == 404


def test_load_preset(client, runtime):
    # Create a minimal preset file
    from nh_presets import Preset, save
    from nh_core import HarmonicField, Partial
    field = HarmonicField(f1=80.0)
    field.partials[1] = Partial(n=1, gain=1.0)
    p = Preset(harmonic_field=field)
    PRESETS_DIR.mkdir(parents=True, exist_ok=True)
    preset_path = PRESETS_DIR / 'test_load.json'
    save(p, str(preset_path))
    try:
        r = client.post("/nh/v1/presets/test_load/load")
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert data["f1"] == 80.0
        assert runtime.base_field.f1 == 80.0
    finally:
        preset_path.unlink(missing_ok=True)


def test_websocket_runtime(client):
    with client.websocket_connect("/nh/v1/ws") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "renderer_capabilities"
        msg = ws.receive_json()
        assert msg["type"] == "base_field"
        assert msg["payload"]["f1"] == 65.0


def test_websocket_control_event_applies_to_model(client, runtime):
    with client.websocket_connect("/nh/v1/ws") as ws:
        ws.receive_json()  # capabilities
        ws.receive_json()  # field
        ws.send_json({"type": "control_event", "payload": {"type": "f1_offset", "value": 10.0}})
    # After websocket close, the runtime model should reflect the change.
    assert runtime.model.f1_offset == 10.0


def test_websocket_sensor_event_with_mapping(client, runtime):
    runtime.sensor_mapping = {"muse_focus": {"param": "master_gain", "scale": 1.0, "offset": 0.0}}
    with client.websocket_connect("/nh/v1/ws") as ws:
        ws.receive_json()  # capabilities
        ws.receive_json()  # field
        ws.send_json({"type": "sensor_event", "payload": {"source": "muse", "type": "muse_focus", "value": 0.75}})
    assert runtime.model.master_gain == 0.75




def test_websocket_control_event_master(client, runtime):
    with client.websocket_connect("/nh/v1/ws") as ws:
        ws.receive_json()
        ws.receive_json()
        ws.send_json({"type": "control_event", "payload": {"type": "master", "value": 0.75}})
    assert runtime.model.master_gain == 0.75


def test_websocket_control_event_partial_gain(client, runtime):
    with client.websocket_connect("/nh/v1/ws") as ws:
        ws.receive_json()
        ws.receive_json()
        ws.send_json({"type": "control_event", "payload": {"type": "partial_gain", "value": {"n": 3, "gain": 0.5}}})
    assert runtime.model.partial_gain_offsets[3] == 0.5


def test_websocket_panic_resets_model(client, runtime):
    runtime.model.f1_offset = 5.0
    runtime.model.master_gain = 0.5
    runtime.model.partial_gain_offsets[1] = 0.2
    with client.websocket_connect("/nh/v1/ws") as ws:
        ws.receive_json()
        ws.receive_json()
        ws.send_json({"type": "control_event", "payload": {"type": "panic"}})
    assert runtime.model.f1_offset == 0.0
    assert runtime.model.master_gain == 0.0
    assert runtime.model.partial_gain_offsets == {}


def test_launchpad_pad_events_map_to_partial_gain(client, runtime):
    """Pad events from LaunchpadAdapter (via nh_ui) affect model partial gains (deterministic, no hw)."""
    from nh_control import LaunchpadAdapter
    adapter = LaunchpadAdapter(stride=16, split_mode=True)
    # lower pad -> momentary pad_on n=1 -> gain 1
    msg = type('M', (), {'type': 'note_on', 'note': 0, 'velocity': 127})()
    ev = adapter.on_midi_message(msg)
    assert ev.type == 'pad_on'
    assert ev.value['n'] == 1
    with client.websocket_connect("/nh/v1/ws") as ws:
        ws.receive_json()  # caps
        ws.receive_json()  # field
        ws.send_json({"type": "control_event", "payload": ev.to_dict()})
    assert runtime.model.partial_gain_offsets.get(1) == 1.0

    # upper pad -> second press on toggle n=1 -> off , gain 0
    msg2 = type('M', (), {'type': 'note_on', 'note': 64, 'velocity': 127})()
    ev2 = adapter.on_midi_message(msg2)  # first upper press -> active true
    ev2 = adapter.on_midi_message(msg2)  # second upper press -> active false
    assert ev2.type == 'pad_toggle'
    assert ev2.value['n'] == 1
    assert ev2.value['active'] is False
    with client.websocket_connect("/nh/v1/ws") as ws:
        ws.receive_json()
        ws.receive_json()
        ws.send_json({"type": "control_event", "payload": ev2.to_dict()})
    assert runtime.model.partial_gain_offsets.get(1) == 0.0


def test_nh_ui_imports_launchpad_adapter():
    """nh-ui can integrate nh_control.LaunchpadAdapter without hardware."""
    from nh_control import LaunchpadAdapter
    from nh_ui.server import broadcast_control_event, set_launchpad_control_handler
    a = LaunchpadAdapter()
    assert hasattr(a, 'led_for_event')
    assert a.COLOR_ORANGE == 21
    set_launchpad_control_handler(None)
    broadcast_control_event({"type": "pad_toggle", "value": {"n": 3, "active": True}})
    # no crash, no clients ok
    assert True


def test_connect_does_not_raise_master(client, runtime):
    """Connecting must never auto-raise the master; audio starts silent for safety."""
    assert runtime.model.master_gain == 0.0
    with client.websocket_connect("/nh/v1/ws") as ws:
        ws.receive_json()  # capabilities
        ws.receive_json()  # base_field
    assert runtime.model.master_gain == 0.0


def test_broadcast_control_event_reaches_client(client):
    """A control_event broadcast (e.g. from the physical launchpad) mirrors to UI clients.

    Exercises the thread-safe feedback loop: the broadcast is issued from the test
    thread and must be scheduled onto the UI loop captured at connect time.
    """
    from nh_ui.server import broadcast_control_event
    with client.websocket_connect("/nh/v1/ws") as ws:
        ws.receive_json()  # capabilities
        ws.receive_json()  # base_field
        broadcast_control_event(
            {"source": "launchpad", "type": "pad_toggle", "value": {"n": 5, "active": True}}
        )
        received = None
        for _ in range(30):  # base_field ticks are interleaved; find our event
            msg = ws.receive_json()
            if msg["type"] == "control_event":
                received = msg
                break
    assert received is not None, "control_event was not mirrored to the client"
    assert received["payload"]["type"] == "pad_toggle"
    assert received["payload"]["value"]["n"] == 5
