"""Comprehensive pytest suite for the refactored synth_pure pipeline and VoiceCache.

Run:
    /home/nicolas/Projects/digital-beacon/.venv/bin/python -m pytest tests/test_synth_pure_refactor.py -v --tb=short
"""

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

# Make imports work when running pytest from project root (matches documented API usage).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.synth_pure import (
    N_HARMONICS,
    SAMPLE_RATE,
    analyze,
    get_cache,
    prepare_analysis,
    synthesize,
    synthesize_cached,
    synthesize_prepared,
)
from tools.voice_cache import VoiceCache


# ---------------------------------------------------------------------------
# Module-level / conftest-style shared fixtures (no external conftest.py needed)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def fixture_wav_path() -> Path:
    """Absolute path to the canonical 2.0 s fixture WAV (mono, 44100 Hz)."""
    p = Path("/tmp/fixture_voice.wav")
    assert p.exists(), "Missing /tmp/fixture_voice.wav — create fixture before running tests"
    return p


@pytest.fixture(scope="module")
def fixture_audio(fixture_wav_path: Path):
    """Return (y, sr) tuple for the fixture. Loaded once per test module."""
    y, sr = sf.read(str(fixture_wav_path), always_2d=False)
    if y.ndim > 1:
        y = y[:, 0]
    return y.astype(np.float32), int(sr)


@pytest.fixture
def temp_voice_wav(tmp_path: Path) -> Path:
    """Isolated copy of the fixture so that mtime/size mutations in cache tests
    never touch the shared /tmp/fixture_voice.wav used by other tests.
    """
    src = Path("/tmp/fixture_voice.wav")
    dst = tmp_path / "voice_copy.wav"
    dst.write_bytes(src.read_bytes())
    return dst


@pytest.fixture
def test_cache_dir(tmp_path: Path) -> Path:
    """Temporary cache directory (automatically cleaned via tmp_path)."""
    d = tmp_path / "test_voicecache"
    d.mkdir(parents=True, exist_ok=True)
    yield d
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------------------
# 1. Backward compatibility
# ---------------------------------------------------------------------------

def test_backward_compat(fixture_audio):
    """Call the original synthesize() entry point with the full legacy parameter set.

    Assert non-empty finite output whose length matches duration * SAMPLE_RATE
    (within 5-sample tolerance). Uses soundfile to load, exactly as callers do.
    """
    y, sr = fixture_audio
    out = synthesize(
        y, sr,
        thresh_db=-30,
        f0_min=70,
        f0_max=400,
        max_voices=8,
        noise_floor_db=-50.0,
        gain_curve="sqrt",
        spectral_tilt_db=-12.0,
    )
    assert isinstance(out, np.ndarray)
    assert out.size > 0
    assert np.all(np.isfinite(out))
    expected_len = int(2.0 * SAMPLE_RATE)
    assert abs(len(out) - expected_len) <= 5


# ---------------------------------------------------------------------------
# 2. Split correctness (prepare + synthesize_prepared == synthesize)
# ---------------------------------------------------------------------------

def test_split_prepare_synthesize(fixture_audio):
    """prepare_analysis() followed by synthesize_prepared() must be numerically
    identical (atol=1e-5) to a direct synthesize() call when using identical
    F0/analysis and synth parameters.
    """
    y, sr = fixture_audio
    prepared = prepare_analysis(y, sr)
    out_split = synthesize_prepared(
        prepared,
        thresh_db=-30,
        noise_floor_db=-50.0,
        max_voices=8,
        gain_curve="sqrt",
        spectral_tilt_db=-12.0,
    )
    out_direct = synthesize(
        y, sr,
        thresh_db=-30,
        f0_min=70,
        f0_max=400,
        max_voices=8,
        noise_floor_db=-50.0,
        gain_curve="sqrt",
        spectral_tilt_db=-12.0,
    )
    assert np.allclose(out_split, out_direct, atol=1e-5)


# ---------------------------------------------------------------------------
# 3. Parameter overrides
# ---------------------------------------------------------------------------

def test_per_harmonic_gains_changes_output(fixture_audio):
    """per_harmonic_gains override must change the rendered waveform."""
    y, sr = fixture_audio
    prepared = prepare_analysis(y, sr)
    out_def = synthesize_prepared(
        prepared,
        thresh_db=-30, noise_floor_db=-50.0, max_voices=8,
        gain_curve="sqrt", spectral_tilt_db=-12.0,
    )
    out_mod = synthesize_prepared(
        prepared,
        thresh_db=-30, noise_floor_db=-50.0, max_voices=8,
        gain_curve="sqrt", spectral_tilt_db=-12.0,
        per_harmonic_gains={1: 1.0, 2: 0.0},
    )
    assert not np.allclose(out_def, out_mod)


def test_wave_shapes_square_changes_output(fixture_audio):
    """wave_shapes override must change the rendered waveform (square vs default sine)."""
    y, sr = fixture_audio
    prepared = prepare_analysis(y, sr)
    out_def = synthesize_prepared(
        prepared,
        thresh_db=-30, noise_floor_db=-50.0, max_voices=8,
        gain_curve="sqrt", spectral_tilt_db=-12.0,
    )
    out_mod = synthesize_prepared(
        prepared,
        thresh_db=-30, noise_floor_db=-50.0, max_voices=8,
        gain_curve="sqrt", spectral_tilt_db=-12.0,
        wave_shapes={1: "square"},
    )
    assert not np.allclose(out_def, out_mod)


def test_wave_shapes_triangle_and_saw(fixture_audio):
    """triangle and saw wave_shapes must produce finite, non-silent output.

    Audibility of non-sine wave shapes is verified by ear-test per the task brief,
    not by automated spectral analysis.
    """
    y, sr = fixture_audio
    prepared = prepare_analysis(y, sr)
    for shape in ("triangle", "saw"):
        out = synthesize_prepared(
            prepared,
            thresh_db=-30, noise_floor_db=-50.0, max_voices=8,
            gain_curve="sqrt", spectral_tilt_db=-12.0,
            wave_shapes={1: shape},
        )
        assert np.all(np.isfinite(out))
        assert np.max(np.abs(out)) > 1e-6


def test_gain_curve_changes_output(fixture_audio):
    """Different gain_curve values must affect output (use lower thresh so
    weaker harmonics produce g_lin < 1.0 where sqrt vs linear diverges).
    """
    y, sr = fixture_audio
    prepared = prepare_analysis(y, sr)
    out_s = synthesize_prepared(
        prepared,
        thresh_db=-60, noise_floor_db=-90.0, max_voices=8,
        gain_curve="sqrt", spectral_tilt_db=-12.0,
    )
    out_l = synthesize_prepared(
        prepared,
        thresh_db=-60, noise_floor_db=-90.0, max_voices=8,
        gain_curve="linear", spectral_tilt_db=-12.0,
    )
    assert not np.allclose(out_s, out_l)


# ---------------------------------------------------------------------------
# 4. VoiceCache behavior
# ---------------------------------------------------------------------------

def test_cache_store_and_get(temp_voice_wav, test_cache_dir):
    """VoiceCache round-trips a prepared dict (RAM + disk .npz).

    Arrays compare with np.allclose, scalars with ==. Same keys.
    (Equivalent usage: VoiceCache(cache_dir=/tmp/test_voicecache))
    """
    y, sr = sf.read(str(temp_voice_wav), always_2d=False)
    if y.ndim > 1:
        y = y[:, 0]
    y = y.astype(np.float32)
    prepared = prepare_analysis(y, sr)

    c = VoiceCache(cache_dir=test_cache_dir)
    c.store(temp_voice_wav, prepared)

    got = c.get(temp_voice_wav)
    assert got is not None
    assert set(got.keys()) == set(prepared.keys())

    for k, v in got.items():
        if isinstance(v, np.ndarray):
            assert np.allclose(v, prepared[k], atol=1e-12, rtol=0)
        else:
            assert v == prepared[k]


def test_cache_miss_on_mtime_change(temp_voice_wav, test_cache_dir):
    """Touching the source WAV (mtime update) must cause a cache miss on get()."""
    y, sr = sf.read(str(temp_voice_wav), always_2d=False)
    if y.ndim > 1:
        y = y[:, 0]
    prepared = prepare_analysis(y.astype(np.float32), sr)

    c = VoiceCache(cache_dir=test_cache_dir)
    c.store(temp_voice_wav, prepared)
    assert c.get(temp_voice_wav) is not None

    # Update mtime (atime/mtime to now)
    os.utime(temp_voice_wav, None)
    # Small sleep not required; utime is synchronous
    assert c.get(temp_voice_wav) is None


def test_cache_miss_on_size_change(temp_voice_wav, test_cache_dir):
    """Changing the source WAV file size must cause a cache miss on get()."""
    y, sr = sf.read(str(temp_voice_wav), always_2d=False)
    if y.ndim > 1:
        y = y[:, 0]
    prepared = prepare_analysis(y.astype(np.float32), sr)

    c = VoiceCache(cache_dir=test_cache_dir)
    c.store(temp_voice_wav, prepared)
    assert c.get(temp_voice_wav) is not None

    # Append bytes -> size changes
    data = temp_voice_wav.read_bytes()
    temp_voice_wav.write_bytes(data + b"\0" * 256)
    assert c.get(temp_voice_wav) is None


def test_cache_instance_isolation(temp_voice_wav, tmp_path):
    """Separate VoiceCache instances do not share RAM state.

    Store in one instance. A second instance using a *different* cache_dir
    returns None (no RAM sharing, no on-disk entry in its dir).

    When using the *same* cache_dir a fresh instance still succeeds via the
    on-disk .npz (disk cache is shared across processes/instances).
    """
    y, sr = sf.read(str(temp_voice_wav), always_2d=False)
    if y.ndim > 1:
        y = y[:, 0]
    prepared = prepare_analysis(y.astype(np.float32), sr)

    d_shared = tmp_path / "vc_shared"
    d_other = tmp_path / "vc_other"

    c1 = VoiceCache(cache_dir=d_shared)
    c1.store(temp_voice_wav, prepared)

    c2 = VoiceCache(cache_dir=d_other)
    assert c2.get(temp_voice_wav) is None  # RAM isolation + different on-disk location

    c3 = VoiceCache(cache_dir=d_shared)
    assert c3.get(temp_voice_wav) is not None  # disk cache shared


# ---------------------------------------------------------------------------
# 5. CLI smoke test
# ---------------------------------------------------------------------------

def test_cli_smoke():
    """Subprocess execution of synth_pure.py CLI must succeed and produce output WAV."""
    out_wav = Path("/tmp/synth_pure_test.wav")
    if out_wav.exists():
        out_wav.unlink()

    venv_python = "/home/nicolas/Projects/digital-beacon/.venv/bin/python"
    cmd = [
        venv_python,
        "tools/synth_pure.py",
        "/tmp/fixture_voice.wav",
        "--out", str(out_wav),
        "--log", "WARNING",
    ]

    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    try:
        assert proc.returncode == 0, f"CLI failed: rc={proc.returncode} stderr={proc.stderr[-500:]}"
        assert out_wav.exists(), "output WAV not created"
        assert out_wav.stat().st_size > 100, "output WAV implausibly small"
    finally:
        if out_wav.exists():
            out_wav.unlink()


# ---------------------------------------------------------------------------
# 6. build_voice_compare_v3 import test
# ---------------------------------------------------------------------------

def test_build_voice_compare_v3_imports():
    """Import tools.build_voice_compare_v3 and check the expected public symbols.

    synthesize is the alias re-exported from synth_pure.
    No pipeline execution required.
    """
    import tools.build_voice_compare_v3 as bvc

    # synthesize (aliased from synth_pure)
    assert hasattr(bvc, "synthesize")
    assert callable(getattr(bvc, "synthesize"))

    # SYNTH_PARAMS must contain at least the documented keys
    assert hasattr(bvc, "SYNTH_PARAMS")
    sp = bvc.SYNTH_PARAMS
    assert isinstance(sp, dict)
    for key in ("thresh_db", "f0_min", "f0_max"):
        assert key in sp

    assert callable(bvc.discover_inputs)
    assert callable(bvc.process_one)
    assert callable(bvc.build_html)


# Quick sanity that the top-level imports we declared actually succeeded
def test_top_level_api_imports():
    """The documented import set from synth_pure and VoiceCache is available."""
    assert callable(synthesize)
    assert callable(prepare_analysis)
    assert callable(synthesize_prepared)
    assert callable(synthesize_cached)
    assert callable(get_cache)
    assert callable(analyze)
    assert isinstance(N_HARMONICS, int) and N_HARMONICS > 0
    assert isinstance(SAMPLE_RATE, int) and SAMPLE_RATE > 0
    assert VoiceCache is not None
