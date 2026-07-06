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
