import pytest

from nh_core import HarmonicField, Partial
from nh_model import ModelState


def test_default_state():
    state = ModelState()
    assert state.master_gain == 0.0
    assert state.f1_offset == 0.0


def test_apply_control_master():
    state = ModelState()
    state.apply_control({"type": "master", "value": 0.5})
    assert state.master_gain == 0.5


def test_apply_sensor_mapping():
    state = ModelState()
    mapping = {
        "eeg_focus": {"param": "master_gain", "scale": 0.2, "offset": 0.0},
    }
    state.apply_sensor({"type": "eeg_focus", "value": 0.75}, mapping)
    assert state.master_gain == pytest.approx(0.15)


def test_snapshot_applies_modulations():
    field = HarmonicField(f1=65.0)
    field.partials[1] = Partial(n=1, gain=1.0)
    field.partials[2] = Partial(n=2, gain=0.5)

    state = ModelState(base_field=field)
    state.master_gain = 2.0
    state.f1_offset = 5.0
    state.partial_gain_offsets[2] = 3.0

    snapshot = state.to_snapshot()
    assert snapshot.f1 == 70.0
    assert snapshot.partials[1].gain == 2.0
    assert snapshot.partials[2].gain == pytest.approx(0.5 * 2.0 * 3.0)


def test_update_from_base_field():
    field = HarmonicField(f1=65.0)
    field.partials[1] = Partial(n=1, gain=1.0)
    state = ModelState(base_field=field)
    state.master_gain = 0.5

    new_field = HarmonicField(f1=80.0)
    new_field.partials[3] = Partial(n=3, gain=1.0)
    state.update_from_base_field(new_field)

    assert state.base_field.f1 == 80.0
    assert state.master_gain == 0.5  # modulation preserved


def test_round_trip_dict():
    field = HarmonicField(f1=65.0)
    field.partials[1] = Partial(n=1, gain=1.0)
    state = ModelState(base_field=field, master_gain=0.8)
    d = state.to_dict()
    restored = ModelState.from_dict(d)
    assert restored.base_field.f1 == 65.0
    assert restored.master_gain == 0.8


def test_panic_resets():
    state = ModelState()
    state.master_gain = 0.5
    state.f1_offset = 10.0
    state.apply_control({"type": "panic"})
    assert state.master_gain == 0.0
    assert state.f1_offset == 0.0


def test_apply_control_pad_on_off():
    state = ModelState()
    state.apply_control({"type": "pad_on", "value": {"n": 3, "vel": 127}})
    assert state.partial_gain_offsets[3] == 1.0
    state.apply_control({"type": "pad_off", "value": {"n": 3, "vel": 0}})
    assert state.partial_gain_offsets[3] == 0.0


def test_apply_control_pad_toggle():
    state = ModelState()
    state.apply_control({"type": "pad_toggle", "value": {"n": 5, "active": True}})
    assert state.partial_gain_offsets[5] == 1.0
    state.apply_control({"type": "pad_toggle", "value": {"n": 5, "active": False}})
    assert state.partial_gain_offsets[5] == 0.0


def test_apply_control_pad_ignores_missing_n():
    state = ModelState()
    state.apply_control({"type": "pad_on", "value": {"n": 0}})
    assert state.partial_gain_offsets == {}


def test_snapshot_master_zero_is_silent():
    """Default master (0) zeroes every partial gain, so the render is silent."""
    field = HarmonicField(f1=65.0)
    field.partials[1] = Partial(n=1, gain=1.0)
    field.partials[2] = Partial(n=2, gain=0.7)
    state = ModelState(base_field=field)  # master defaults to 0
    snapshot = state.to_snapshot()
    assert all(p.gain == 0.0 for p in snapshot.partials.values())
