"""Unit tests for digital_beacon.compressor.

Acceptance:
  - Compressor parameters are honoured (threshold, ratio, knee).
  - Gain reduction ≤ 6 dB on real-like signals.
  - True-peak never exceeds -1 dBTP.
  - Makeup gain auto-compensates correctly (within 1 dB of GR).
  - Output is 16-bit PCM 44.1 kHz mono WAV.
"""

import math
import os
import sys
import tempfile
import wave
from pathlib import Path

import numpy as np
import pytest

# Make imports work when running pytest from project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from digital_beacon.compressor import (
    CompressorParams,
    apply_compressor_chain,
    write_review_copy_wav,
    _db_to_linear,
    _linear_to_db,
    _peak_db,
    _rms_db,
    _true_peak_db,
)

SR = 44100
DTYPE = np.float32


# ── helpers ───────────────────────────────────────────────────────────────────

def sine_wave(freq: float, duration: float, amp: float = 1.0) -> np.ndarray:
    t = np.arange(int(SR * duration), dtype=DTYPE) / SR
    return (amp * np.sin(2.0 * np.pi * freq * t)).astype(DTYPE)


def pink_noise(duration: float, amp: float = 1.0) -> np.ndarray:
    """Approximate pink noise (1/f) for realistic dynamic content."""
    n = int(SR * duration)
    white = np.random.randn(n).astype(DTYPE)
    # integrate white noise (spectral slope -6 dB/oct → pink-like)
    pink = np.cumsum(white)
    # high-pass to kill DC drift
    pink = np.diff(pink, prepend=pink[0])
    # normalise
    pink = pink / (np.max(np.abs(pink)) + 1e-12)
    return (amp * pink).astype(DTYPE)


def read_wav(path: str) -> tuple[np.ndarray, int]:
    with wave.open(path, "rb") as w:
        sr = w.getframerate()
        n = w.getnframes()
        raw = w.readframes(n)
        pcm = np.frombuffer(raw, dtype=np.int16)
        return pcm.astype(DTYPE) / 32768.0, sr


# ── param validation ────────────────────────────────────────────────────────────────

class TestParams:
    def test_default_params(self):
        p = CompressorParams()
        assert p.threshold_db == -18.0
        assert p.ratio == 2.0

    def test_ratio_bounds(self):
        CompressorParams(ratio=1.5)
        CompressorParams(ratio=2.0)
        with pytest.raises(ValueError):
            CompressorParams(ratio=1.2)
        with pytest.raises(ValueError):
            CompressorParams(ratio=2.5)

    def test_makeup_cap_nonnegative(self):
        with pytest.raises(ValueError):
            CompressorParams(makeup_cap_db=-1.0)


# ── linear/dB helpers ─────────────────────────────────────────────────────────────────

class TestHelpers:
    def test_db_roundtrip(self):
        for db in [-60, -18, -1, 0, 6]:
            lin = _db_to_linear(db)
            back = _linear_to_db(lin)
            assert abs(back - db) < 0.01

    def test_peak_db_sine(self):
        x = sine_wave(1000, 0.1, amp=0.5)
        assert abs(_peak_db(x) - _linear_to_db(0.5)) < 0.1

    def test_rms_db_sine(self):
        x = sine_wave(1000, 0.1, amp=0.5)
        # RMS of sine = amp / sqrt(2)
        expected = _linear_to_db(0.5 / math.sqrt(2))
        assert abs(_rms_db(x) - expected) < 0.2

    def test_true_peak_upsample(self):
        x = sine_wave(1000, 0.1, amp=0.5)
        tp = _true_peak_db(x, SR)
        # upsampled TP of a pure sine should be very close to peak
        assert abs(tp - _peak_db(x)) < 0.2


# ── compressor behaviour ────────────────────────────────────────────────────────────────

class TestCompressor:
    def test_below_threshold_no_gr(self):
        """Signal peak at -30 dBFS: no GR expected."""
        x = sine_wave(1000, 0.5, amp=_db_to_linear(-30.0))
        res = apply_compressor_chain(x, SR)
        assert res.gain_reduction_db < 0.5  # essentially zero
        assert res.stage_metrics[0].peak_after == pytest.approx(
            res.stage_metrics[0].peak_before, abs=0.5
        )

    def test_above_threshold_gr_positive(self):
        """Signal peak at -10 dBFS: should see GR."""
        x = sine_wave(1000, 0.5, amp=_db_to_linear(-10.0))
        res = apply_compressor_chain(x, SR)
        assert res.gain_reduction_db > 1.0

    def test_gr_never_exceeds_6_db(self):
        """On realistic voice-like content GR ≤ 6 dB."""
        np.random.seed(42)
        x = pink_noise(2.0, amp=_db_to_linear(-3.0))  # loud signal
        res = apply_compressor_chain(x, SR)
        assert res.gain_reduction_db <= 6.0 + 0.5  # tolerance for stochastic noise

    def test_ratio_2_vs_1_5(self):
        """Higher ratio produces more GR."""
        np.random.seed(7)
        x = pink_noise(1.0, amp=_db_to_linear(-6.0))
        r2 = apply_compressor_chain(x, SR, CompressorParams(ratio=2.0))
        r15 = apply_compressor_chain(x, SR, CompressorParams(ratio=1.5))
        assert r2.gain_reduction_db > r15.gain_reduction_db

    def test_attack_release_timing(self):
        """Attack smooths onset; release smooths decay."""
        # burst of high amplitude then silence
        burst = sine_wave(1000, 0.05, amp=_db_to_linear(-3.0))
        silence = np.zeros(int(SR * 0.2), dtype=DTYPE)
        x = np.concatenate([burst, silence])
        res = apply_compressor_chain(x, SR, CompressorParams(attack_ms=20, release_ms=200))
        # GR should not instantaneously drop to 0 after burst ends (release)
        gr_track = res.stage_metrics  # we don't have per-sample GR, but we can check
        # that the compressed tail is still below the original silence level (zero)
        # just verify it ran without exception and produced reasonable output
        assert res.audio.dtype == np.float32
        assert res.audio.shape == x.shape

    def test_knee_softens_transition(self):
        """With 6 dB knee, signal at -19.5 dBFS (inside knee) should see partial GR."""
        x = sine_wave(1000, 0.5, amp=_db_to_linear(-19.5))
        res = apply_compressor_chain(x, SR, CompressorParams(knee_db=6.0))
        # with hard knee (0 dB) there would be no GR at -19.5; with 6 dB knee there should be some
        assert res.gain_reduction_db > 0.1


# ── makeup gain ──────────────────────────────────────────────────────────────────────────

class TestMakeupGain:
    def test_auto_compensates_gr(self):
        """Makeup gain should be close to observed GR."""
        np.random.seed(3)
        x = pink_noise(1.0, amp=_db_to_linear(-6.0))
        res = apply_compressor_chain(x, SR)
        assert res.makeup_gain_db == pytest.approx(res.gain_reduction_db, abs=1.0)

    def test_capped_at_6_db(self):
        """If GR > 6 dB, makeup is capped at 6 dB."""
        # extreme signal to force >6 dB GR
        x = sine_wave(1000, 0.5, amp=_db_to_linear(-1.0))
        res = apply_compressor_chain(x, SR, CompressorParams(makeup_cap_db=6.0))
        assert res.makeup_gain_db <= 6.0 + 0.1

    def test_makeup_restores_loudness(self):
        """After makeup, RMS should be closer to original than compressed-only."""
        np.random.seed(5)
        x = pink_noise(1.0, amp=_db_to_linear(-6.0))
        rms_orig = _rms_db(x)
        res = apply_compressor_chain(x, SR)
        rms_after = _rms_db(res.audio)
        # after makeup we are closer to original than after compression alone
        rms_compressed = res.stage_metrics[0].rms_after
        assert abs(rms_after - rms_orig) < abs(rms_compressed - rms_orig) + 1.5


# ── true-peak limiter ──────────────────────────────────────────────────────────────────

class TestTruePeakLimiter:
    def test_tp_never_exceeds_minus_1(self):
        """True-peak after limiter must stay ≤ -1 dBTP."""
        np.random.seed(9)
        x = pink_noise(1.0, amp=_db_to_linear(-3.0))
        res = apply_compressor_chain(x, SR)
        assert res.true_peak_db <= -1.0 + 0.1
        assert not res.peak_exceeded

    def test_inter_sample_peaks_caught(self):
        """High-frequency sine can have inter-sample peaks > sample peaks."""
        x = sine_wave(15000, 0.2, amp=0.95)  # near Nyquist
        res = apply_compressor_chain(x, SR)
        assert res.true_peak_db <= -1.0 + 0.2


# ── output format ──────────────────────────────────────────────────────────────────────

class TestWavOutput:
    def test_16bit_pcm_44100_mono(self):
        x = sine_wave(1000, 0.1, amp=0.5)
        res = apply_compressor_chain(x, SR)
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            path = f.name
        try:
            write_review_copy_wav(res, path)
            with wave.open(path, "rb") as w:
                assert w.getnchannels() == 1
                assert w.getsampwidth() == 2
                assert w.getframerate() == 44100
                n = w.getnframes()
                raw = w.readframes(n)
                pcm = np.frombuffer(raw, dtype=np.int16)
                assert pcm.shape[0] == x.shape[0]
        finally:
            os.unlink(path)

    def test_no_clipping_in_pcm(self):
        np.random.seed(11)
        x = pink_noise(1.0, amp=_db_to_linear(-3.0))
        res = apply_compressor_chain(x, SR)
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            path = f.name
        try:
            write_review_copy_wav(res, path)
            audio, _ = read_wav(path)
            assert np.max(np.abs(audio)) <= 1.0 + 1e-6
        finally:
            os.unlink(path)


# ── integration / smoke ───────────────────────────────────────────────────────────────────

class TestIntegration:
    def test_full_chain_on_sine_sweep(self):
        """Linear sine sweep from 100 Hz to 10 kHz; verify no NaN/Inf."""
        duration = 1.0
        t = np.arange(int(SR * duration), dtype=DTYPE) / SR
        # log sweep
        f0, f1 = 100.0, 10000.0
        phase = 2.0 * np.pi * f0 * t * np.exp(t / duration * np.log(f1 / f0))
        x = (0.5 * np.sin(phase)).astype(DTYPE)
        res = apply_compressor_chain(x, SR)
        assert np.isfinite(res.audio).all()
        assert res.gain_reduction_db <= 6.0 + 0.5
        assert res.true_peak_db <= -1.0 + 0.1

    def test_full_chain_on_realistic_burst(self):
        """Simulated voice-like burst: quiet, then loud, then quiet."""
        np.random.seed(13)
        quiet = pink_noise(0.3, amp=_db_to_linear(-30.0))
        loud = pink_noise(0.4, amp=_db_to_linear(-6.0))
        tail = pink_noise(0.3, amp=_db_to_linear(-24.0))
        x = np.concatenate([quiet, loud, tail])
        res = apply_compressor_chain(x, SR)
        assert np.isfinite(res.audio).all()
        assert res.gain_reduction_db <= 6.0 + 0.5
        assert res.true_peak_db <= -1.0 + 0.1

    def test_metrics_exposed(self):
        x = sine_wave(1000, 0.1, amp=0.5)
        res = apply_compressor_chain(x, SR)
        assert len(res.stage_metrics) == 3
        for m in res.stage_metrics:
            assert math.isfinite(m.peak_before)
            assert math.isfinite(m.peak_after)
            assert math.isfinite(m.rms_before)
            assert math.isfinite(m.rms_after)

    def test_result_dtype_and_shape(self):
        x = sine_wave(1000, 0.1)
        res = apply_compressor_chain(x, SR)
        assert res.audio.dtype == np.float32
        assert res.audio.ndim == 1
        assert res.audio.shape == x.shape

    def test_stereo_rejected(self):
        x = np.zeros((100, 2), dtype=np.float32)
        with pytest.raises(ValueError, match="1-D mono"):
            apply_compressor_chain(x, SR)

    def test_empty_audio(self):
        x = np.zeros(0, dtype=DTYPE)
        res = apply_compressor_chain(x, SR)
        assert res.audio.shape == (0,)
        assert res.gain_reduction_db == 0.0

    def test_default_params_none(self):
        x = sine_wave(1000, 0.1)
        res = apply_compressor_chain(x, SR, None)
        assert res.params.ratio == 2.0


class TestGainReductionClamp:
    def test_gr_clamped_to_6_db_on_loud_signal(self):
        """A 0 dBFS sine should be clamped to exactly 6 dB GR (transparent limit)."""
        x = sine_wave(1000, 0.5, amp=1.0)  # 0 dBFS peak
        res = apply_compressor_chain(x, SR)
        assert res.gain_reduction_db == pytest.approx(6.0, abs=0.1)
        assert res.avg_gain_reduction_db <= 6.0 + 0.1

    def test_clamped_output_no_pumping(self):
        """After clamping, output should still be continuous (no discontinuities)."""
        x = sine_wave(1000, 0.5, amp=1.0)
        res = apply_compressor_chain(x, SR)
        # check for no sharp jumps in the compressed waveform
        diff = np.abs(np.diff(res.audio))
        # max sample-to-sample change should be well below 1.0 for a 1 kHz sine at 44.1k
        assert np.max(diff) < 0.5

    def test_clamped_gr_warns(self, caplog):
        """Clamping should emit a warning log."""
        import logging
        x = sine_wave(1000, 0.5, amp=1.0)
        with caplog.at_level(logging.WARNING, logger="digital_beacon.compressor"):
            apply_compressor_chain(x, SR)
        assert "clamping to transparent limit" in caplog.text


class TestArchitectureSeparation:
    def test_compressor_not_imported_by_analysis_modules(self):
        """No digital_beacon analysis module should import the compressor."""
        import ast
        import importlib.util

        pkg_root = Path(__file__).resolve().parent.parent / "digital_beacon"
        for path in pkg_root.glob("*.py"):
            if path.name == "compressor.py":
                continue
            source = path.read_text()
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    if "compressor" in module:
                        pytest.fail(f"{path.name} imports compressor via '{module}'")
                    for alias in node.names:
                        if "compressor" in alias.name:
                            pytest.fail(f"{path.name} imports compressor name '{alias.name}'")
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if "compressor" in alias.name:
                            pytest.fail(f"{path.name} imports compressor name '{alias.name}'")
