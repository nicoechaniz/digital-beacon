import pytest

from nh_control import ControlEvent, LaunchpadAdapter, MappingGraph


class FakeMidoNote:
    def __init__(self, type_, note, velocity):
        self.type = type_
        self.note = note
        self.velocity = velocity


class FakeMidoCC:
    def __init__(self, control, value):
        self.type = "control_change"
        self.control = control
        self.value = value


def test_mapping_graph():
    graph = MappingGraph()
    graph.add("eeg_focus", "master_gain", scale=0.2, offset=0.0)
    out = graph.apply({"type": "eeg_focus", "value": 0.75})
    assert out["type"] == "master_gain"
    assert out["value"] == pytest.approx(0.15)


def test_launchpad_note_on():
    adapter = LaunchpadAdapter(stride=16, split_mode=True)
    msg = FakeMidoNote("note_on", 0, 127)
    ev = adapter.on_midi_message(msg)
    assert ev is not None
    assert ev.type == "pad_on"
    assert ev.value["n"] == 1


def test_launchpad_split_toggle():
    adapter = LaunchpadAdapter(stride=16, split_mode=True)
    # Row 4 (upper half) pad note = row 4 * 16 + 0 = 64
    msg = FakeMidoNote("note_on", 64, 127)
    ev = adapter.on_midi_message(msg)
    assert ev.type == "pad_toggle"
    assert ev.value["active"] is True


def test_launchpad_panic_cc():
    adapter = LaunchpadAdapter(stride=16)
    msg = FakeMidoCC(111, 127)
    ev = adapter.on_midi_message(msg)
    assert ev.type == "panic"


def test_control_event_round_trip():
    ev = ControlEvent(source="test", type="x", value=1)
    d = ev.to_dict()
    restored = ControlEvent.from_dict(d)
    assert restored.type == "x"
