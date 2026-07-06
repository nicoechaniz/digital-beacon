"""Gentle compressor DSP chain for review-copy audio.

Applied ONLY to already-gain-normalized audio destined for human ears.
Never used for harmonic descriptor computation.

Chain:
  1. Gentle compressor (feed-forward, peak-detector, soft knee)
  2. Auto makeup gain (capped)
  3. True-peak limiter

Output: 16-bit PCM 44.1 kHz mono WAV.
"""

from __future__ import annotations

import dataclasses
import logging
import math
from typing import Optional

import numpy as np

log = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class CompressorParams:
    """User-tunable compressor settings."""

    threshold_db: float = -18.0          # dBFS
    ratio: float = 2.0                   # 1.5 … 2.0
    knee_db: float = 6.0                 # dB
    attack_ms: float = 25.0              # 20 … 30 ms
    release_ms: float = 250.0            # 200 … 300 ms
    makeup_cap_db: float = 6.0           # max auto makeup gain
    tp_limit_db: float = -1.0            # true-peak ceiling

    def __post_init__(self):
        if not 1.5 <= self.ratio <= 2.0:
            raise ValueError(f"ratio must be in [1.5, 2.0], got {self.ratio}")
        if self.makeup_cap_db < 0:
            raise ValueError("makeup_cap_db must be >= 0")


@dataclasses.dataclass(frozen=True)
class StageMetrics:
    """Peak and RMS levels before / after a stage."""

    peak_before: float
    peak_after: float
    rms_before: float
    rms_after: float


@dataclasses.dataclass(frozen=True)
class CompressorResult:
    """Output audio + per-stage metrics."""

    audio: np.ndarray                    # float32, shape (samples,), mono
    sample_rate: int
    params: CompressorParams
    gain_reduction_db: float             # max GR observed (peak)
    avg_gain_reduction_db: float         # mean GR across all samples (dB)
    rms_gain_reduction_db: float         # RMS GR across all samples (dB)
    makeup_gain_db: float
    stage_metrics: list[StageMetrics]
    true_peak_db: float                  # after limiter
    peak_exceeded: bool                  # TP > ceiling?


# ─── helpers ──────────────────────────────────────────────────────────

def _db_to_linear(db: float) -> float:
    return 10.0 ** (db / 20.0)


def _linear_to_db(x: float) -> float:
    return 20.0 * math.log10(max(x, 1e-12))


def _peak_db(x: np.ndarray) -> float:
    if x.size == 0:
        return -120.0
    return _linear_to_db(float(np.max(np.abs(x))))


def _rms_db(x: np.ndarray) -> float:
    if x.size == 0:
        return -120.0
    return _linear_to_db(float(np.sqrt(np.mean(x * x))))


def _true_peak_db(x: np.ndarray, sr: int) -> float:
    """Upsample 4× and return peak in dBFS."""
    if x.size == 0:
        return -120.0
    from scipy import signal
    # resample_poly: upsample by 4, anti-alias low-pass
    up = signal.resample_poly(x, up=4, down=1)
    return _linear_to_db(float(np.max(np.abs(up))))


# ─── core compressor ──────────────────────────────────────────────────

class _FeedForwardCompressor:
    """Feed-forward peak compressor with soft knee."""

    def __init__(self, params: CompressorParams, sr: int):
        self.p = params
        self.sr = sr
        # attack/release coefficients (exponential smoothing)
        self.atc = math.exp(-1.0 / (sr * params.attack_ms / 1000.0))
        self.rel = math.exp(-1.0 / (sr * params.release_ms / 1000.0))
        self.threshold_lin = _db_to_linear(params.threshold_db)
        self.knee_lo = _db_to_linear(params.threshold_db - params.knee_db / 2.0)
        self.knee_hi = _db_to_linear(params.threshold_db + params.knee_db / 2.0)

    def _gain_reduction(self, x: np.ndarray) -> np.ndarray:
        """Return per-sample gain factor (0..1) to apply."""
        if x.size == 0:
            return np.array([], dtype=x.dtype)
        # Peak detector (rectified, smoothed)
        abs_x = np.abs(x)
        env = np.zeros_like(abs_x)
        env[0] = abs_x[0]
        for i in range(1, abs_x.size):
            if abs_x[i] > env[i - 1]:
                env[i] = self.atc * env[i - 1] + (1.0 - self.atc) * abs_x[i]
            else:
                env[i] = self.rel * env[i - 1] + (1.0 - self.rel) * abs_x[i]

        # Gain computer (soft knee)
        gr = np.ones_like(env)  # gain reduction factor (linear)
        for i in range(env.size):
            level = env[i]
            if level < self.knee_lo:
                gr[i] = 1.0
            elif level < self.knee_hi:
                # knee interpolation
                db = _linear_to_db(level)
                knee_db = self.p.knee_db
                lo = self.p.threshold_db - knee_db / 2.0
                hi = self.p.threshold_db + knee_db / 2.0
                t = (db - lo) / knee_db
                # use (db - lo) as the overshoot (relative to knee start, always non-negative)
                # partial GR proportional to t
                overshoot = db - lo
                gr_db = overshoot * (self.p.ratio - 1.0) / self.p.ratio
                gr[i] = _db_to_linear(-gr_db)
            else:
                db = _linear_to_db(level)
                gr_db = max(0.0, (db - self.p.threshold_db) * (self.p.ratio - 1.0) / self.p.ratio)
                gr[i] = _db_to_linear(-gr_db)

        # Clamp gain reduction to transparent limit (6 dB max)
        min_gr = _db_to_linear(-6.0)
        if np.any(gr < min_gr):
            max_gr_observed = -_linear_to_db(float(np.min(gr)))
            log.warning(
                "Gain reduction exceeds 6 dB (%.2f dB); clamping to transparent limit.",
                max_gr_observed,
            )
            gr = np.clip(gr, min_gr, 1.0)
        return gr

    def process(self, x: np.ndarray) -> np.ndarray:
        gr = self._gain_reduction(x)
        return x * gr

    def max_gr_db(self, x: np.ndarray) -> float:
        if x.size == 0:
            return 0.0
        gr = self._gain_reduction(x)
        min_gr = float(np.min(gr))
        return -_linear_to_db(min_gr) if min_gr > 0 else 0.0

    def avg_gr_db(self, x: np.ndarray) -> float:
        """Mean gain reduction in dB across non-unity GR samples."""
        if x.size == 0:
            return 0.0
        gr = self._gain_reduction(x)
        active = gr < 1.0
        if not np.any(active):
            return 0.0
        gr_db = -np.log10(np.maximum(gr[active], 1e-12)) * 20.0
        return float(np.mean(gr_db))

    def rms_gr_db(self, x: np.ndarray) -> float:
        """RMS of gain reduction in dB across all samples (including zeros)."""
        if x.size == 0:
            return 0.0
        gr = self._gain_reduction(x)
        gr_db = -np.log10(np.maximum(gr, 1e-12)) * 20.0
        return float(np.sqrt(np.mean(gr_db * gr_db)))


# ─── true-peak limiter ────────────────────────────────────────────────

class _TruePeakLimiter:
    """Simple lookahead-free brick-wall limiter with 4× upsample TP check."""

    def __init__(self, ceiling_db: float, sr: int, oversample: int = 4):
        self.ceiling_lin = _db_to_linear(ceiling_db)
        self.sr = sr
        self.oversample = oversample

    def process(self, x: np.ndarray) -> np.ndarray:
        # fast attack limiter: gain factor = ceiling / abs(x) when abs(x) > ceiling
        abs_x = np.abs(x)
        gain = np.ones_like(x)
        over = abs_x > self.ceiling_lin
        gain[over] = self.ceiling_lin / abs_x[over]
        # smooth the gain envelope lightly to avoid crackle (1 ms release)
        rel = math.exp(-1.0 / (self.sr * 0.001))
        for i in range(1, gain.size):
            if gain[i] < gain[i - 1]:
                gain[i] = min(gain[i - 1], gain[i])
            else:
                gain[i] = rel * gain[i - 1] + (1.0 - rel) * gain[i]
        y = x * gain

        # After sample-level limiting, catch inter-sample peaks with 4x upsample check.
        # If true peak exceeds ceiling, apply additional scalar attenuation.
        if y.size > 0:
            tp_db = _true_peak_db(y, self.sr)
            tp_lin = _db_to_linear(tp_db)
            if tp_lin > self.ceiling_lin:
                extra = self.ceiling_lin / tp_lin
                y = y * extra
        return y

    def peak_after(self, x: np.ndarray) -> float:
        return _true_peak_db(self.process(x), self.sr)


# ─── public API ───────────────────────────────────────────────────────

def apply_compressor_chain(
    audio: np.ndarray,
    sample_rate: int,
    params: Optional[CompressorParams] = None,
) -> CompressorResult:
    """Apply the full review-copy compressor chain to *audio*.

    *audio* is expected float32 mono, already gain-normalized (peak ≈ -1 dBFS or lower).
    Returns 16-bit PCM-ready float32 audio (peak ≤ 0 dBFS after limiter).
    """
    if audio.ndim != 1:
        raise ValueError("audio must be 1-D mono")
    if audio.dtype != np.float32:
        audio = audio.astype(np.float32)
    if params is None:
        params = CompressorParams()

    stages: list[StageMetrics] = []

    # ── Stage 1: Compressor ───────────────────────────────────────
    peak_before = _peak_db(audio)
    rms_before = _rms_db(audio)
    comp = _FeedForwardCompressor(params, sample_rate)
    compressed = comp.process(audio)
    peak_after = _peak_db(compressed)
    rms_after = _rms_db(compressed)
    stages.append(StageMetrics(peak_before, peak_after, rms_before, rms_after))
    gr_db = comp.max_gr_db(audio)
    avg_gr_db = comp.avg_gr_db(audio)
    rms_gr_db = comp.rms_gr_db(audio)
    log.debug("Compressor GR=%.2f dB (avg=%.2f dB rms=%.2f dB)", gr_db, avg_gr_db, rms_gr_db)

    # ── Stage 2: Makeup gain ────────────────────────────────────────
    # compensate the observed GR, capped
    makeup_db = min(gr_db, params.makeup_cap_db)
    makeup_lin = _db_to_linear(makeup_db)
    made_up = compressed * makeup_lin
    stages.append(StageMetrics(
        peak_after, _peak_db(made_up), rms_after, _rms_db(made_up)
    ))

    # ── Stage 3: True-peak limiter ──────────────────────────────────
    peak_before_lim = _peak_db(made_up)
    rms_before_lim = _rms_db(made_up)
    limiter = _TruePeakLimiter(params.tp_limit_db, sample_rate)
    limited = limiter.process(made_up)
    stages.append(StageMetrics(
        peak_before_lim, _peak_db(limited), rms_before_lim, _rms_db(limited)
    ))

    tp_db = _true_peak_db(limited, sample_rate)
    peak_exceeded = tp_db > params.tp_limit_db + 0.01  # tiny tolerance

    # Ensure no float overshoot
    limited = np.clip(limited, -1.0, 1.0)

    return CompressorResult(
        audio=limited,
        sample_rate=sample_rate,
        params=params,
        gain_reduction_db=gr_db,
        avg_gain_reduction_db=avg_gr_db,
        rms_gain_reduction_db=rms_gr_db,
        makeup_gain_db=makeup_db,
        stage_metrics=stages,
        true_peak_db=tp_db,
        peak_exceeded=peak_exceeded,
    )


def write_review_copy_wav(
    result: CompressorResult,
    path: str,
) -> None:
    """Write result as 16-bit PCM 44.1 kHz mono WAV."""
    import wave
    import struct

    # Convert float32 [-1,1] to int16
    pcm = (result.audio * 32767.0).astype(np.int16)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(result.sample_rate)
        w.writeframes(pcm.tobytes())
    log.info("Wrote review copy: %s", path)
