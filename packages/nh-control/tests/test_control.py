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
    # bottom-left pad (row 7, col 0) -> momentary, n=1
    msg = FakeMidoNote("note_on", 112, 127)
    ev = adapter.on_midi_message(msg)
    assert ev is not None
    assert ev.type == "pad_on"
    assert ev.value["n"] == 1


def test_launchpad_split_toggle():
    adapter = LaunchpadAdapter(stride=16, split_mode=True)
    # top of toggle half (row 3, col 0) -> toggle, n=1
    msg = FakeMidoNote("note_on", 48, 127)
    ev = adapter.on_midi_message(msg)
    assert ev.type == "pad_toggle"
    assert ev.value["active"] is True
    assert ev.value["n"] == 1


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


def test_launchpad_led_feedback():
    adapter = LaunchpadAdapter(stride=16, split_mode=True)
    # lower momentary -> green
    msg = FakeMidoNote("note_on", 112, 127)
    ev = adapter.on_midi_message(msg)
    led = adapter.led_for_event(ev)
    assert led is not None
    assert led["note"] == 112
    assert led["velocity"] == adapter.COLOR_GREEN

    # upper toggle -> orange
    msg2 = FakeMidoNote("note_on", 48, 127)
    ev2 = adapter.on_midi_message(msg2)
    led2 = adapter.led_for_event(ev2)
    assert led2 is not None
    assert led2["note"] == 48
    assert led2["velocity"] == adapter.COLOR_ORANGE

    # toggle off -> off
    msg3 = FakeMidoNote("note_on", 48, 127)  # toggle again
    ev3 = adapter.on_midi_message(msg3)
    led3 = adapter.led_for_event(ev3)
    assert led3["velocity"] == adapter.COLOR_OFF

    # panic led
    evp = ControlEvent(source="launchpad", type="panic", value=None)
    ledp = adapter.led_for_event(evp)
    assert ledp == {"type": "all_off"}
