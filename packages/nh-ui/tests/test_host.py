import pytest
from pathlib import Path
from fastapi.testclient import TestClient
from nh_core import HarmonicField, Partial, RendererCapabilities
from nh_presets import Preset, save
from nh_runtime import BaseFieldServer
from nh_ui.server import make_app


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
    preset_path = Path('/home/nicolas/Projects/digital-beacon/data/migrated_presets/test_load.json')
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
    assert runtime.model.master_gain == 1.0
    assert runtime.model.partial_gain_offsets == {}
