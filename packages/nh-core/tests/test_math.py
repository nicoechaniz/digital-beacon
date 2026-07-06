from nh_core import (
    cents_difference,
    freq_for_harmonic,
    octave_reduce,
    playable_frequency,
)


def test_freq_for_harmonic_basic():
    assert freq_for_harmonic(65.0, 3) == 195.0


def test_cents_octave():
    assert cents_difference(130.0, 65.0) == 1200.0


def test_octave_reduce_below_base():
    assert octave_reduce(130.0, base=65.0) == 65.0


def test_playable_clamp():
    assert playable_frequency(10.0) == 20.0
    assert playable_frequency(100000.0) == 20000.0
