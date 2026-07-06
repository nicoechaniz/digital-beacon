import pytest
from nh_core import (
    HarmonicField,
    Partial,
    RendererCapabilities,
    Residual,
    cents_difference,
    freq_for_harmonic,
    octave_reduce,
    playable_frequency,
)


def test_freq_for_harmonic():
    assert freq_for_harmonic(65.0, 1) == 65.0
    assert freq_for_harmonic(65.0, 4) == 260.0


def test_cents_difference():
    assert cents_difference(130.0, 65.0) == pytest.approx(1200.0)
    assert cents_difference(65.0, 0.0) == 0.0


def test_octave_reduce():
    assert octave_reduce(520.0, base=65.0) == pytest.approx(65.0)
    assert octave_reduce(130.0, base=65.0) == pytest.approx(65.0)


def test_playable_frequency():
    assert playable_frequency(5.0) == 20.0
    assert playable_frequency(25000.0) == 20000.0
    assert playable_frequency(440.0) == 440.0


def test_harmonic_field_defaults():
    field = HarmonicField(f1=50.0)
    assert field.f1 == 50.0
    assert field.partials == {}


def test_partial_effective_freq():
    p = Partial(n=3)
    assert p.effective_freq(50.0) == 150.0
    p_explicit = Partial(n=3, freq=155.0)
    assert p_explicit.effective_freq(50.0) == 155.0


def test_projection_variable_counts():
    for count in [13, 32, 100]:
        field = HarmonicField(f1=65.0)
        for n in range(1, count + 1):
            field.partials[n] = Partial(n=n, gain=1.0 / n, phase=0.1 * n)
        assert len(field.partials) == count


def test_project_to_capabilities_limits_partials():
    field = HarmonicField(f1=65.0)
    for n in range(1, 33):
        field.partials[n] = Partial(n=n, gain=1.0 / n, phase=0.1 * n)

    caps_13 = RendererCapabilities(max_partials=13)
    projected = field.project_to_capabilities(caps_13)
    assert len(projected.partials) == 13

    caps_32 = RendererCapabilities(max_partials=32)
    projected_back = projected.project_to_capabilities(caps_32)
    assert len(projected_back.partials) == 13  # lossy, cannot recover


def test_project_drops_phase_and_spatial():
    field = HarmonicField(f1=65.0)
    field.partials[1] = Partial(n=1, gain=1.0, phase=0.5, pan=0.25, spatial={"az": 30.0})

    caps = RendererCapabilities(max_partials=1, supports_phase=False, supports_spatial=False)
    projected = field.project_to_capabilities(caps)
    assert projected.partials[1].phase == 0.0
    assert projected.partials[1].pan == 0.0
    assert projected.partials[1].spatial is None


def test_project_drops_residual():
    field = HarmonicField(f1=65.0, residual=Residual(kind="audio", audio_path="/tmp/x.wav"))
    caps = RendererCapabilities(max_partials=1, supports_residual=False)
    projected = field.project_to_capabilities(caps)
    assert projected.residual.kind == "none"


def test_round_trip_dict():
    field = HarmonicField(f1=65.0)
    field.partials[1] = Partial(n=1, gain=1.0, phase=0.1)
    field.partials[2] = Partial(n=2, gain=0.5, spatial={"az": 0.0})
    d = field.to_dict()
    restored = HarmonicField.from_dict(d)
    assert restored.f1 == 65.0
    assert len(restored.partials) == 2
    assert restored.partials[1].gain == 1.0
    assert restored.partials[2].spatial == {"az": 0.0}
