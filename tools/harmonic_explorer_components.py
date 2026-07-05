"""Reusable components for the Harmonic Field Explorer.

This module decouples the explorer's core concerns so they can be reused by
other tools (voice shaper, beacon dashboard, etc.):

  AudioLoader          - partial/cached mono WAV loading
  HarmonicAnalyzer     - harmonicity score + candidate fundamental search
  SpectrogramRenderer  - spectrum / spectrogram PNG generation
  HarmonicController   - OSC control of the digital_beacon Shaper + Launchpad

Dependencies are imported lazily where possible so the module can be parsed
without librosa/numpy present.
"""

from __future__ import annotations

import io
import logging
import re
import threading
import wave
from pathlib import Path
from typing import Optional

try:
    import librosa
    import numpy as np
    from scipy.signal import resample_poly
    HAS_DEPS = True
except ImportError:  # pragma: no cover
    librosa = None
    np = None
    resample_poly = None
    HAS_DEPS = False

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    HAS_MPL = True
except ImportError:  # pragma: no cover
    HAS_MPL = False

try:
    import soundfile as sf
    HAS_SF = True
except ImportError:  # pragma: no cover
    sf = None
    HAS_SF = False

try:
    import mido
    HAS_MIDO = True
except ImportError:  # pragma: no cover
    mido = None
    HAS_MIDO = False

try:
    from pythonosc.udp_client import SimpleUDPClient
    HAS_OSC = True
except ImportError:  # pragma: no cover
    SimpleUDPClient = None
    HAS_OSC = False

log = logging.getLogger(__name__)

SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_\-. ]{1,128}$")


def valid_id(sample_id: str) -> bool:
    return bool(SAFE_ID_RE.match(sample_id))


def id_to_stem(sample_id: str) -> str:
    return sample_id


# ─────────────────────────────────────────────────────────────────────────
# AudioLoader
# ─────────────────────────────────────────────────────────────────────────

class AudioLoader:
    """Load mono WAV samples with optional partial windowing.

    Centered windows are used by default for analysis so the start/end transients
    (e.g. handling noise) do not dominate the result. Full loads are *not* cached
    to avoid memory bloat with long field recordings; partial loads are never
    cached.
    """

    def __init__(self, sample_dir: Path):
        self.sample_dir = Path(sample_dir)
        self._cache: dict[str, dict] = {}
        self._lock = threading.RLock()

    def _find_wav(self, sample_id: str) -> Path:
        if not valid_id(sample_id):
            raise ValueError(f"invalid sample id: {sample_id!r}")
        stem = id_to_stem(sample_id)
        for wav in self.sample_dir.glob("*.wav"):
            if wav.stem == stem:
                return wav
        raise FileNotFoundError(f"sample not found: {stem}")

    def load(self, sample_id: str, max_duration_s: Optional[float] = None,
             offset_s: Optional[float] = None, centered: bool = True) -> dict:
        """Return {y, sr, duration, stem}.

        If max_duration_s is None the full file is loaded. If offset_s is None
        and centered is True, the window is centered in the file; otherwise it
        starts at offset_s.
        """
        if not HAS_SF:
            raise RuntimeError("soundfile is required")

        wav_path = self._find_wav(sample_id)
        info = sf.info(wav_path)
        sr = info.samplerate
        duration = info.duration

        max_duration_s = max_duration_s or duration
        if max_duration_s >= duration:
            offset_s = 0.0
        elif offset_s is None and centered:
            offset_s = max(0.0, (duration - max_duration_s) / 2)
        else:
            offset_s = offset_s or 0.0

        offset_s = max(0.0, min(offset_s, duration - max_duration_s))
        start_frame = int(offset_s * sr)
        frames = int(max_duration_s * sr)

        if frames <= 0:
            raise ValueError("max_duration_s too small")

        if frames >= duration * sr:
            y, sr = sf.read(wav_path, dtype="float32", always_2d=False)
        else:
            y, sr = sf.read(wav_path, dtype="float32", start=start_frame,
                            frames=frames, always_2d=False)

        if y.ndim > 1:
            y = np.mean(y, axis=1)

        return {"y": np.asarray(y, dtype=np.float32), "sr": sr,
                "duration": len(y) / sr, "stem": id_to_stem(sample_id)}

    def full_duration(self, sample_id: str) -> float:
        wav_path = self._find_wav(sample_id)
        return sf.info(wav_path).duration


# ─────────────────────────────────────────────────────────────────────────
# HarmonicAnalyzer
# ─────────────────────────────────────────────────────────────────────────

class HarmonicAnalyzer:
    """Score how much spectral energy falls on a natural harmonic grid f0*N."""

    def __init__(self, n_fft: int = 8192, hop: Optional[int] = None):
        if not HAS_DEPS:
            raise RuntimeError("librosa/numpy/scipy are required")
        self.n_fft = n_fft
        self.hop = hop or n_fft // 4

    def _stft_power(self, y: np.ndarray, sr: int) -> tuple[np.ndarray, np.ndarray]:
        S = np.abs(librosa.stft(y, n_fft=self.n_fft, hop_length=self.hop))
        power = (S ** 2).mean(axis=1)
        freqs = librosa.fft_frequencies(sr=sr, n_fft=self.n_fft)
        return freqs, power

    def harmonicity(self, y: np.ndarray, sr: int, f0: float,
                    bandwidth_hz: float, n_harmonics: int = 64) -> float:
        """Fraction of total spectral power inside the harmonic mask."""
        if f0 <= 0 or bandwidth_hz <= 0 or n_harmonics <= 0:
            return 0.0
        freqs, power = self._stft_power(y, sr)
        total = power.sum()
        if total <= 0:
            return 0.0

        mask = np.zeros_like(freqs, dtype=bool)
        for n in range(1, n_harmonics + 1):
            h = n * f0
            if h > sr / 2:
                break
            mask |= (np.abs(freqs - h) <= bandwidth_hz / 2)

        masked = power[mask].sum()
        return float(masked / total)

    def candidates(self, y: np.ndarray, sr: int,
                   f1_min: float = 20.0, f1_max: float = 200.0,
                   bandwidth_hz: float = 10.0, n_harmonics: int = 32,
                   n_top: int = 5) -> list[dict]:
        """Return top-N candidate fundamentals maximizing harmonicity."""
        freqs, power = self._stft_power(y, sr)
        # Use FFT bin spacing as the resolution of the search.
        df = freqs[1] - freqs[0]
        candidates = np.arange(f1_min, f1_max + df, df)
        scores = []
        for f0 in candidates:
            mask = np.zeros_like(freqs, dtype=bool)
            for n in range(1, n_harmonics + 1):
                h = n * f0
                if h > sr / 2:
                    break
                mask |= (np.abs(freqs - h) <= bandwidth_hz / 2)
            score = power[mask].sum() / power.sum() if power.sum() > 0 else 0.0
            scores.append((f0, score))

        scores.sort(key=lambda x: x[1], reverse=True)
        # Simple dedup: keep only candidates separated by > 1 Hz.
        kept = []
        for f0, score in scores:
            if all(abs(f0 - k) > 1.0 for k, _ in kept):
                kept.append((f0, score))
            if len(kept) >= n_top:
                break
        return [{"f0": float(f0), "score": float(score)} for f0, score in kept]


# ─────────────────────────────────────────────────────────────────────────
# SpectrogramRenderer
# ─────────────────────────────────────────────────────────────────────────

class SpectrogramRenderer:
    """Render spectrum / spectrogram PNGs with the harmonic grid baked in."""

    def __init__(self, loader: AudioLoader, out_dir: Optional[Path] = None):
        if not HAS_MPL:
            raise RuntimeError("matplotlib is required")
        self.loader = loader
        self.out_dir = out_dir or (Path.home() / "Music" / "field-recordings" / "analysis" / "explorer")
        self.out_dir.mkdir(parents=True, exist_ok=True)

    def _long_term_spectrum(self, y: np.ndarray, sr: int, n_fft: int = 8192,
                            hop: Optional[int] = None) -> tuple[np.ndarray, np.ndarray]:
        hop = hop or n_fft // 4
        S = np.abs(librosa.stft(y, n_fft=n_fft, hop_length=hop))
        power = (S ** 2).mean(axis=1)
        freqs = librosa.fft_frequencies(sr=sr, n_fft=n_fft)
        return freqs, np.sqrt(power)

    def spectrum(self, sample_id: str, f0: float, bandwidth_hz: float,
                 n_harmonics: int, max_duration_s: float = 120.0) -> Path:
        info = self.loader.load(sample_id, max_duration_s=max_duration_s, centered=True)
        y, sr = info["y"], info["sr"]
        freqs, amp = self._long_term_spectrum(y, sr)
        db = 20 * np.log10(np.maximum(amp, 1e-12))
        db = db - db.max()

        fig, ax = plt.subplots(figsize=(12, 5))
        ax.fill_between(freqs, db, -120, color="#58a6ff", alpha=0.4)
        ax.plot(freqs, db, color="#58a6ff", lw=0.8)
        for n in range(1, n_harmonics + 1):
            h = n * f0
            if h > freqs[-1]:
                break
            color = "cyan" if n <= 8 else "white"
            ax.axvline(h, color=color, linestyle="--", linewidth=1.0, alpha=0.8)
            if n <= 8:
                ax.text(h, -3, f" {n}f0", color=color, fontsize=8, rotation=90, va="top")

        ax.set_xlim(0, 2000)
        ax.set_ylim(-80, 5)
        ax.set_xlabel("Frequency (Hz)")
        ax.set_ylabel("Magnitude (dB)")
        ax.set_title(f"{id_to_stem(sample_id)} — f0={f0:.1f} Hz bw={bandwidth_hz:.1f} Hz")
        fig.tight_layout()
        out = self.out_dir / f"{id_to_stem(sample_id)}_spec_avg_f0-{f0:.1f}_bw-{bandwidth_hz:.1f}.png"
        fig.savefig(out, dpi=120)
        plt.close(fig)
        return out

    def spectrogram(self, sample_id: str, f0: float, bandwidth_hz: float,
                    n_harmonics: int, max_duration_s: float = 60.0) -> Path:
        info = self.loader.load(sample_id, max_duration_s=max_duration_s, centered=True)
        y, sr = info["y"], info["sr"]

        spec_sr = 8000
        if sr > spec_sr:
            y = librosa.resample(y, orig_sr=sr, target_sr=spec_sr)
        else:
            spec_sr = sr

        n_fft = 2048
        hop = 512
        S = np.abs(librosa.stft(y, n_fft=n_fft, hop_length=hop))
        S_db = librosa.amplitude_to_db(S, ref=np.max)
        freqs = librosa.fft_frequencies(sr=spec_sr, n_fft=n_fft)
        times = librosa.frames_to_time(np.arange(S.shape[1]), sr=spec_sr, hop_length=hop)

        fig, ax = plt.subplots(figsize=(14, 6))
        img = ax.imshow(
            S_db,
            aspect="auto",
            origin="lower",
            extent=[times[0], times[-1], freqs[0], freqs[-1]],
            cmap="magma",
            vmin=-80,
            vmax=0,
            interpolation="nearest",
        )
        fig.colorbar(img, ax=ax, format="%+2.0f dB")

        # Exact harmonic grid + passbands
        for n in range(1, n_harmonics + 1):
            h = n * f0
            if h > 2000:
                break
            color = "cyan" if n <= 8 else "white"
            alpha = 0.9 if n <= 8 else 0.4
            ax.axhline(h, color=color, linestyle="--", linewidth=1.2, alpha=alpha)
            if n <= 8:
                ax.text(times[-1] * 0.01, h, f" {n}f0", color=color, fontsize=8, va="bottom")
            ax.axhspan(h - bandwidth_hz / 2, h + bandwidth_hz / 2, alpha=0.20, color="lime")

        ax.set_xlim(0, times[-1])
        ax.set_ylim(0, 2000)
        ax.set_xlabel("Time (s)", fontsize=9)
        ax.set_ylabel("Frequency (Hz)", fontsize=9)
        ax.set_title(f"{id_to_stem(sample_id)} spectrogram — f0={f0:.1f} Hz bw={bandwidth_hz:.1f} Hz", fontsize=10)
        fig.subplots_adjust(left=0.055, right=0.99, top=0.94, bottom=0.055)

        out = self.out_dir / f"{id_to_stem(sample_id)}_spec_f0-{f0:.1f}_bw-{bandwidth_hz:.1f}.png"
        fig.savefig(out, dpi=130)
        plt.close(fig)
        return out


# ─────────────────────────────────────────────────────────────────────────
# HarmonicController  (OSC + Launchpad)
# ─────────────────────────────────────────────────────────────────────────

class HarmonicController:
    """Control the digital_beacon Shaper and Beacon via OSC.

    Launchpad Mini layout (programmer mode, 8x8 grid = 64 notes):
      - Bottom 32 pads (rows 0-3): toggle mode, maps to harmonics 1-32.
      - Top 32 pads (rows 4-7): sound-on-press mode, maps to harmonics 1-32.
    """

    def __init__(self, f1: float = 40.0, f1_min: float = 20.0, f1_max: float = 200.0,
                 beacon_host: str = "127.0.0.1", beacon_port: int = 9001,
                 sclang_host: str = "127.0.0.1", sclang_port: int = 57120,
                 default_voice_gain: float = 0.6):
        if not HAS_OSC:
            raise RuntimeError("pythonosc is required for OSC control")
        self.f1 = f1
        self.f1_min = f1_min
        self.f1_max = f1_max
        self.gain = default_voice_gain
        self._default_gain = default_voice_gain
        self._beacon_client = SimpleUDPClient(beacon_host, beacon_port)
        self._sclang_client = SimpleUDPClient(sclang_host, sclang_port)
        self._voice_id = 0
        self._lock = threading.Lock()
        self._launchpad: Optional[object] = None
        self._launchpad_thread: Optional[threading.Thread] = None
        self._running = False

    def set_f1(self, f1: float) -> None:
        f1 = max(self.f1_min, min(self.f1_max, float(f1)))
        self.f1 = f1
        # Broadcast so f1_bridge and any co-listeners pick it up.
        self._beacon_client.send_message("/beacon/f1", [float(f1)])
        # Also push directly to sclang so the SC band centers retune.
        self._sclang_client.send_message("/beacon/f1", [float(f1)])
        log.info("controller set f1=%.2f Hz", f1)

    def _next_voice_id(self) -> int:
        with self._lock:
            self._voice_id += 1
            return self._voice_id

    def voice_on(self, harmonic_n: int, gain: Optional[float] = None) -> int:
        """Send voice_on for harmonic_n. Returns voice_id."""
        freq = harmonic_n * self.f1
        voice_id = self._next_voice_id()
        g = gain if gain is not None else self.gain
        # Address both the broadcast listener and sclang directly.
        self._beacon_client.send_message("/beacon/voice_on", [int(harmonic_n), int(voice_id), float(freq), float(g)])
        self._sclang_client.send_message("/beacon/voice_on", [int(harmonic_n), int(voice_id), float(freq), float(g)])
        return voice_id

    def voice_off(self, voice_id: int) -> None:
        self._beacon_client.send_message("/beacon/voice_off", [int(voice_id)])
        self._sclang_client.send_message("/beacon/voice_off", [int(voice_id)])

    def panic(self) -> None:
        self._beacon_client.send_message("/beacon/panic", [])
        self._sclang_client.send_message("/digital/panic", [])

    # ─── Launchpad integration ─────────────────────────────────────────────────

    def start_launchpad(self, port_pattern: str = "Launchpad") -> bool:
        if not HAS_MIDO:
            log.warning("mido not available; Launchpad disabled")
            return False
        if self._launchpad is not None:
            return True

        in_name = None
        for name in mido.get_input_names():
            if port_pattern.lower() in name.lower():
                in_name = name
                break
        if not in_name:
            log.warning("Launchpad not found (pattern=%r)", port_pattern)
            return False

        try:
            self._in_port = mido.open_input(in_name)
        except Exception as exc:
            log.error("Could not open Launchpad input: %s", exc)
            return False

        self._out_port = None
        if in_name in mido.get_output_names():
            try:
                self._out_port = mido.open_output(in_name)
            except Exception:
                pass
        if self._out_port is None:
            for out_name in mido.get_output_names():
                if in_name.split(" MIDI ")[0] in out_name:
                    try:
                        self._out_port = mido.open_output(out_name)
                        break
                    except Exception:
                        continue

        self._held_toggle: dict[int, int] = {}  # bottom-half note -> voice_id
        self._held_sop: dict[int, int] = {}     # top-half note -> voice_id
        self._running = True
        self._launchpad_thread = threading.Thread(target=self._run, name="explorer-launchpad", daemon=True)
        self._launchpad_thread.start()
        log.info("Launchpad started: %s", in_name)
        return True

    def stop_launchpad(self) -> None:
        self._running = False
        if self._out_port:
            try:
                self._out_port.close()
            except Exception:
                pass
        if self._in_port:
            try:
                self._in_port.close()
            except Exception:
                pass
        self._launchpad_thread = None

    def _run(self) -> None:
        for msg in self._in_port:
            if not self._running:
                break
            self._handle_midi(msg)

    def _handle_midi(self, msg) -> None:
        if msg.type not in ("note_on", "note_off"):
            return
        n = self._note_to_harmonic(msg.note)
        if n is None:
            return
        is_top = (msg.note // 16) >= 4
        velocity = msg.velocity if msg.type == "note_on" else 0

        if is_top:
            # Top half: sound-on-press (voice_on while held, voice_off on release).
            if velocity > 0:
                if msg.note in self._held_sop:
                    return
                voice_id = self.voice_on(n)
                self._held_sop[msg.note] = voice_id
                if self._out_port:
                    self._out_port.send(mido.Message("note_on", note=msg.note, velocity=60))
            else:
                voice_id = self._held_sop.pop(msg.note, None)
                if voice_id is not None:
                    self.voice_off(voice_id)
                    if self._out_port:
                        self._out_port.send(mido.Message("note_on", note=msg.note, velocity=0))
        else:
            # Bottom half: toggle on each press.
            if velocity > 0:
                existing = self._held_toggle.get(msg.note)
                if existing is not None:
                    self.voice_off(existing)
                    self._held_toggle.pop(msg.note, None)
                    if self._out_port:
                        self._out_port.send(mido.Message("note_on", note=msg.note, velocity=0))
                else:
                    voice_id = self.voice_on(n)
                    self._held_toggle[msg.note] = voice_id
                    if self._out_port:
                        self._out_port.send(mido.Message("note_on", note=msg.note, velocity=60))

    def _note_to_harmonic(self, note: int) -> Optional[int]:
        # Programmer-mode stride 16. Bottom-left row 0 = note 0.
        x = note % 16
        y = note // 16
        if x >= 8 or y >= 8:
            return None
        # Use the row within the half (0-3) for the harmonic index.
        row = y % 4
        n = 1 + x + row * 8
        return n if 1 <= n <= 32 else None


# ─────────────────────────────────────────────────────────────────────────
# HarmonicPerformanceEngine  (standalone Shaper + Launchpad + Beacon bridge)
# ─────────────────────────────────────────────────────────────────────────

def _import_digital_beacon():
    try:
        from digital_beacon.state import VoiceParameterStore
        from digital_beacon.audio_engine import AudioEngine
        from digital_beacon.config import (
            DEFAULT_F1, F1_MIN, F1_MAX, DEFAULT_VOICE_GAIN,
            AUDIO_SAMPLE_RATE, AUDIO_BLOCK_SIZE, AUDIO_DEVICE,
        )
        return VoiceParameterStore, AudioEngine, DEFAULT_F1, F1_MIN, F1_MAX, DEFAULT_VOICE_GAIN, AUDIO_SAMPLE_RATE, AUDIO_BLOCK_SIZE, AUDIO_DEVICE
    except Exception as exc:
        raise RuntimeError(f"digital_beacon not available: {exc}")


class HarmonicPerformanceEngine:
    """Standalone performance engine for the explorer.

    Combines:
      - a local Shaper additive audio engine (sounddevice sines)
      - Launchpad Mini pad control
      - OSC forwarding to the SC beacon for retuning and voice activation

    This lets the explorer be used as a live instrument without requiring the
    full digital_beacon stack to be running.
    """

    def __init__(self, f1: float = 40.0, audio_device: Optional[int | str] = None,
                 enable_launchpad: bool = True, enable_beacon_osc: bool = True):
        (VoiceParameterStore, AudioEngine, default_f1, f1_min, f1_max,
         default_gain, sr, block, device) = _import_digital_beacon()

        self._f1_min = f1_min
        self._f1_max = f1_max
        self._default_gain = default_gain
        self._store = VoiceParameterStore()
        self._audio = AudioEngine(self._store, sample_rate=sr, block_size=block, device=audio_device or device)
        self._audio.start()
        self._default_gain = default_gain

        self._osc_controller: Optional[HarmonicController] = None
        if enable_beacon_osc:
            try:
                self._osc_controller = HarmonicController(f1=f1)
            except Exception as exc:
                log.warning("Beacon OSC control disabled: %s", exc)

        self._held_voices: dict[int, int] = {}  # harmonic_n -> voice_id
        self._voice_id_counter = 0
        self._lock = threading.Lock()
        self._launchpad: Optional[HarmonicController] = None
        if enable_launchpad:
            self._launchpad = HarmonicController(f1=f1)
            self._launchpad.start_launchpad()
            # Override the launchpad's voice_on/off so it drives the local engine too.
            self._launchpad._held_local = {}
            self._launchpad.voice_on = self._launchpad_voice_on
            self._launchpad.voice_off = self._launchpad_voice_off

        self.set_f1(f1)

    def set_gain(self, gain: float) -> None:
        self._default_gain = float(gain)
        if self._launchpad:
            self._launchpad.gain = self._default_gain
        if self._osc_controller:
            self._osc_controller.gain = self._default_gain

    def set_f1(self, f1: float) -> None:
        f1 = max(self._f1_min, min(self._f1_max, float(f1)))
        self._store.update_f1(f1)
        if self._osc_controller:
            self._osc_controller.set_f1(f1)
        if self._launchpad:
            self._launchpad.f1 = f1
        log.info("performance engine set f1=%.2f Hz", f1)

    def _next_voice_id(self) -> int:
        with self._lock:
            self._voice_id_counter += 1
            return self._voice_id_counter

    def voice_on(self, harmonic_n: int, gain: Optional[float] = None) -> int:
        freq = harmonic_n * self._store.f1
        voice_id = self._next_voice_id()
        g = gain if gain is not None else self._default_gain
        self._store.voice_on(harmonic_n, voice_id, freq, g)
        if self._osc_controller:
            self._osc_controller.voice_on(harmonic_n, g)
        return voice_id

    def voice_off(self, voice_id: int) -> None:
        self._store.voice_off(voice_id)
        if self._osc_controller:
            self._osc_controller.voice_off(voice_id)

    def panic(self) -> None:
        self._store.panic()
        if self._osc_controller:
            self._osc_controller.panic()

    def _launchpad_voice_on(self, harmonic_n: int, gain: Optional[float] = None) -> int:
        voice_id = self.voice_on(harmonic_n, gain)
        self._launchpad._held_local[harmonic_n] = voice_id
        return voice_id

    def _launchpad_voice_off(self, voice_id: int) -> None:
        # Find harmonic_n by voice_id
        for n, vid in list(self._launchpad._held_local.items()):
            if vid == voice_id:
                self._launchpad._held_local.pop(n, None)
                break
        self.voice_off(voice_id)

    def stop(self) -> None:
        self.panic()
        self._audio.stop()
        if self._launchpad:
            self._launchpad.stop_launchpad()


# ─────────────────────────────────────────────────────────────────────────
# Utility helpers
# ─────────────────────────────────────────────────────────────────────────

def encode_wav(y: np.ndarray, sr: int) -> bytes:
    """Encode float32 mono audio as 16-bit WAV bytes."""
    y = np.clip(y, -1.0, 1.0)
    y_int = (y * 32767).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(y_int.tobytes())
    return buf.getvalue()


def mask_harmonic_series(y: np.ndarray, sr: int, f0: float,
                         bandwidth_hz: float = 5.0, n_harmonics: int = 32) -> np.ndarray:
    """Keep only STFT bins within bandwidth_hz of any f0*N harmonic."""
    if not HAS_DEPS:
        raise RuntimeError("librosa/numpy are required")
    n_fft = 16384
    hop = n_fft // 4
    S = librosa.stft(y, n_fft=n_fft, hop_length=hop)
    freqs = librosa.fft_frequencies(sr=sr, n_fft=n_fft)
    mask = np.zeros_like(freqs, dtype=bool)
    for n in range(1, n_harmonics + 1):
        h = n * f0
        if h > sr / 2:
            break
        mask |= (np.abs(freqs - h) <= bandwidth_hz / 2)
    S_masked = S * mask[:, None]
    y_out = librosa.istft(S_masked, hop_length=hop, length=len(y))
    return y_out.astype(np.float32)
