import numpy as np
import pytest

from nh_core import HarmonicField, Partial
from nh_renderers import PythonSounddeviceRenderer


def test_renderer_requires_field():
    r = PythonSounddeviceRenderer(sr=16000, block_size=64)
    assert r.is_running is False


def test_render_sets_field():
    r = PythonSounddeviceRenderer(sr=16000, block_size=64)
    field = HarmonicField(f1=65.0)
    field.partials[1] = Partial(n=1, gain=1.0, pan=0.0)
    field.partials[3] = Partial(n=3, gain=0.5, pan=-0.5)
    r.render(field)
    assert r._last_field is field


def test_callback_silence_when_no_field():
    r = PythonSounddeviceRenderer(sr=16000, block_size=64)
    out = np.zeros((64, 2), dtype=np.float32)
    r._callback(out, 64, None, None)
    assert np.all(out == 0)


def test_callback_outputs_nonzero():
    r = PythonSounddeviceRenderer(sr=16000, block_size=64)
    field = HarmonicField(f1=100.0)
    field.partials[1] = Partial(n=1, gain=1.0)
    r.render(field)
    out = np.zeros((64, 2), dtype=np.float32)
    r._callback(out, 64, None, None)
    assert np.max(np.abs(out)) > 0.01


def test_zero_gain_is_silent():
    """A field whose partials are all at gain 0 (e.g. master 0) renders silence."""
    r = PythonSounddeviceRenderer(sr=16000, block_size=64)
    field = HarmonicField(f1=100.0)
    field.partials[1] = Partial(n=1, gain=0.0)
    field.partials[2] = Partial(n=2, gain=0.0)
    r.render(field)
    out = np.zeros((64, 2), dtype=np.float32)
    r._callback(out, 64, None, None)
    assert np.max(np.abs(out)) == 0.0


def test_high_gain_preset_never_saturates():
    """Peak-safe normalization keeps the output within full scale even for a hot preset."""
    r = PythonSounddeviceRenderer(sr=16000, block_size=512)
    field = HarmonicField(f1=110.0)
    # A deliberately hot preset: many partials, each far above unity gain.
    for n in range(1, 13):
        field.partials[n] = Partial(n=n, gain=9.0, phase=float(n * 30 % 360))
    r.render(field)
    out = np.zeros((512, 2), dtype=np.float32)
    r._callback(out, 512, None, None)
    assert np.max(np.abs(out)) <= 1.0 + 1e-6


def test_normalization_scales_with_active_gain_sum():
    """Two equal partials share the headroom: each peaks near half of one alone."""
    r = PythonSounddeviceRenderer(sr=48000, block_size=2048)
    single = HarmonicField(f1=100.0)
    single.partials[1] = Partial(n=1, gain=2.0)
    r.render(single)
    out1 = np.zeros((2048, 2), dtype=np.float32)
    r._callback(out1, 2048, None, None)

    r._phase = 0.0
    pair = HarmonicField(f1=100.0)
    pair.partials[1] = Partial(n=1, gain=2.0)
    pair.partials[2] = Partial(n=2, gain=2.0)
    r.render(pair)
    out2 = np.zeros((2048, 2), dtype=np.float32)
    r._callback(out2, 2048, None, None)

    # gain sum 2 -> norm 0.5 for the single; gain sum 4 -> norm 0.25 for the pair.
    assert np.max(np.abs(out1)) <= 1.0 + 1e-6
    assert np.max(np.abs(out2)) <= 1.0 + 1e-6
