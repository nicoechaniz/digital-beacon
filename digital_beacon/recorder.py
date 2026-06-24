"""Recording orchestrator for digital-beacon.

Architectural note
------------------
The beacon has TWO independent audio engines that both reach the user's
headphones:
  - SC binaural (server.record captures its bus directly, no PipeWire)
  - Shaper sines (PortAudio/sounddevice output to default sink)

Recording them through PipeWire's sink monitor (as we did initially) has
two problems:
  1. The internal mic (Built-in Audio) was leaking into the recording
     because it shares the same PipeWire graph.
  2. The mix passed through two stacked soft limiters (SC tanh + Shaper
     tanh) before reaching the monitor, which saturated/distorted the
     recorded signal.

The correct architecture (as used in DAWs):
  - SC captures itself via `server.record(path:)` — direct from the
    synthesis bus, sample-perfect, no PipeWire involvement.
  - Shaper taps its OWN final mix (post-sidechain, pre-limiter) from
    inside the audio callback (see AudioEngine.attach_recorder). This
    is the same signal that goes to the speakers, but we intercept it
    before the soft-limiter so the Recorder controls the final limiting
    itself.
  - On stop, the Recorder concatenates the SC + Shaper buffers, resamples
    Shaper to match SC's rate (if needed), sums them with proper
    headroom management, applies a soft-clip, and writes a 24-bit WAV.

Live audio is unaffected: SC keeps going through the binaural HRTF, Shaper
keeps going to the default sink. Recording is a parallel tap.
"""

import logging
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np
from pythonosc.udp_client import SimpleUDPClient

from .state import VoiceParameterStore
from . import config

log = logging.getLogger(__name__)

# Output directory — matches beacon-spatial's layout
RECORD_DIR = Path(os.path.expanduser("~/Music/beacon"))
RECORD_DIR.mkdir(parents=True, exist_ok=True)

# SC runs at 48000 Hz; Shaper is at config.AUDIO_SAMPLE_RATE (44100 default).
# We resample Shaper to 48000 in the mixdown so the SC file is untouched.
TARGET_SR = 48000


class Recorder:
    """Coordinates SC + Shaper recording and produces a final mixdown WAV.

    Thread model:
      - Recording is initiated by the FastAPI handler thread.
      - The Shaper tap is written from the PortAudio callback (C thread).
        That's why we use a list with append() — the GIL makes append
        thread-safe, and we copy the ndarray before yielding.
      - The SC record is fire-and-forget — sclang writes to disk on its
        own; we just wait for the file to exist and grow.
      - The mixdown runs in the calling thread on stop().
    """

    def __init__(
        self,
        store: VoiceParameterStore,
        audio_engine,                       # AudioEngine — for the tap
        sc_osc: SimpleUDPClient,
        beacon_default_path: Optional[Path] = None,  # for write target
    ) -> None:
        self._store = store
        self._audio = audio_engine
        self._sc_osc = sc_osc
        self._lock = threading.RLock()
        self._running = False
        self._started_at: Optional[float] = None
        self._final_path: Optional[Path] = None
        self._shaper_sink: list = []
        self._sc_path: Optional[Path] = None
        self._last_error: Optional[str] = None
        self._total: int = 0

    # ─── Public API ──────────────────────────────────────────────────────

    def status(self) -> dict:
        with self._lock:
            elapsed = (time.time() - self._started_at) if self._started_at and self._running else 0.0
            return {
                "running": self._running,
                "path": str(self._final_path) if self._final_path else None,
                "elapsed_s": elapsed,
                "last_error": self._last_error,
                "total_recordings": self._total,
                "record_dir": str(RECORD_DIR),
            }

    def start(self, name: Optional[str] = None) -> dict:
        with self._lock:
            if self._running:
                return {"ok": False, "error": "Already recording",
                        "path": str(self._final_path) if self._final_path else None}
            label = self._safe_name(name or "session")
            ts = datetime.now().strftime("%Y%m%d-%H%M%S")
            self._final_path = RECORD_DIR / f"{label}_{ts}.wav"
            self._sc_path = RECORD_DIR / f".sc_{label}_{ts}.wav"  # temp, deleted after mix
            self._shaper_sink = []
            self._started_at = time.time()
            self._last_error = None

            # 1. Tell SC to record itself into a temp file
            try:
                self._sc_osc.send_message("/beacon/record/start", [str(self._sc_path)])
            except Exception as exc:
                self._last_error = f"OSC to sclang failed: {exc}"
                self._reset()
                return {"ok": False, "error": self._last_error}

            # 2. Attach the Shaper tap
            self._audio.attach_recorder(self._shaper_sink)

            # 3. Give SC a beat to start writing the file
            time.sleep(0.1)
            self._running = True
            log.info("Recording: SC -> %s + Shaper tap", self._sc_path)
            return {
                "ok": True,
                "path": str(self._final_path),
                "started_at": self._started_at,
            }

    def stop(self) -> dict:
        with self._lock:
            if not self._running:
                return {"ok": True, "already_stopped": True,
                        "path": str(self._final_path) if self._final_path else None}
            elapsed = time.time() - (self._started_at or time.time())
            sc_path = self._sc_path
            shaper_sink = self._shaper_sink
            final_path = self._final_path
            sc_osc = self._sc_osc
            audio = self._audio
            self._running = False
            self._started_at = None

        # 1. Tell SC to stop recording (OSC is fire-and-forget — we don't
        #    block on it; the file should be finalized by the time we
        #    reach the read loop)
        try:
            sc_osc.send_message("/beacon/record/stop", [])
        except Exception as exc:
            log.warning("OSC /beacon/record/stop failed: %s", exc)

        # 2. Detach the Shaper tap
        audio.detach_recorder()

        # 3. Wait briefly for SC to finalize the WAV header
        deadline = time.time() + 3.0
        while time.time() < deadline:
            if sc_path and sc_path.exists() and sc_path.stat().st_size > 100:
                break
            time.sleep(0.05)

        # 4. Mixdown
        try:
            stats = self._mixdown(sc_path, shaper_sink, final_path)
        except Exception as exc:
            log.exception("Mixdown failed")
            self._last_error = f"Mixdown failed: {exc}"
            return {"ok": False, "error": self._last_error, "elapsed_s": elapsed}

        # 5. Cleanup temp SC file
        try:
            if sc_path and sc_path.exists():
                sc_path.unlink()
        except OSError:
            pass

        with self._lock:
            self._total += 1
            self._last_error = None
        return {
            "ok": True,
            "path": str(final_path),
            "elapsed_s": elapsed,
            "size_bytes": stats["size_bytes"],
            "sc_samples": stats["sc_samples"],
            "shaper_samples": stats["shaper_samples"],
            "headroom_db": round(stats["headroom_db"], 2),
        }

    def toggle(self, name: Optional[str] = None) -> dict:
        with self._lock:
            if self._running:
                return self.stop()
        return self.start(name)

    # ─── Mixdown ─────────────────────────────────────────────────────────

    def _mixdown(
        self,
        sc_path: Optional[Path],
        shaper_blocks: list,
        final_path: Path,
    ) -> dict:
        """Read SC WAV + Shaper tap, mix them, write final WAV.

        Uses only numpy + the stdlib `wave` module — no scipy dep.
        Resampling is linear interpolation, which is fine for monitoring/
        capture (we're not mastering here).
        """
        import wave

        sc_samples = 0
        sc_data: Optional[np.ndarray] = None

        if sc_path and sc_path.exists():
            try:
                sc_sr, sc_data = self._read_wav(sc_path)
                if sc_sr != TARGET_SR:
                    sc_data = self._resample_linear(sc_data, sc_sr, TARGET_SR)
                sc_samples = sc_data.shape[0]
            except Exception as exc:
                log.warning("Could not read SC WAV (%s): %s", sc_path, exc)

        shaper_samples = 0
        shaper_data: Optional[np.ndarray] = None
        if shaper_blocks:
            try:
                # Filter out any weirdly-shaped blocks defensively
                valid = [b for b in shaper_blocks
                         if isinstance(b, np.ndarray) and b.ndim == 2 and b.shape[1] == 2]
                if not valid:
                    raise RuntimeError("no valid 2-channel blocks in shaper tap")
                sh = np.concatenate(valid, axis=0).astype(np.float32)
                if config.AUDIO_SAMPLE_RATE != TARGET_SR:
                    sh = self._resample_linear(sh, config.AUDIO_SAMPLE_RATE, TARGET_SR)
                shaper_data = sh
                shaper_samples = sh.shape[0]
                log.info("Shaper tap: %d samples @ %dHz (after resample)",
                         sh.shape[0], TARGET_SR)
            except Exception as exc:
                log.warning("Could not process Shaper tap: %s", exc)
                shaper_data = None

        # ── Sum SC + Shaper ────────────────────────────────────────────
        log.info("Mixdown start: sc_data=%s shaper_data=%s",
                 sc_data.shape if sc_data is not None else None,
                 shaper_data.shape if shaper_data is not None else None)
        # Treat empty (0-sample) data as missing — common when SC records
        # without an active audio source (SoundIn with no input)
        if sc_data is not None and sc_data.shape[0] == 0:
            log.info("SC file is empty (no audio captured) — Shaper-only mixdown")
            sc_data = None
        if shaper_data is not None and shaper_data.shape[0] == 0:
            log.info("Shaper tap is empty — SC-only mixdown")
            shaper_data = None

        if sc_data is None and shaper_data is None:
            raise RuntimeError("Both sources empty — nothing to record")
        if sc_data is None:
            mix = shaper_data
        elif shaper_data is None:
            mix = sc_data
        else:
            # Normalize both to (N, 2)
            if sc_data.ndim == 1:
                sc_data = np.column_stack([sc_data, sc_data])
            if shaper_data.ndim == 1:
                shaper_data = np.column_stack([shaper_data, shaper_data])
            n = min(sc_data.shape[0], shaper_data.shape[0])
            mix = sc_data[:n] + shaper_data[:n]

        if mix is None:
            raise RuntimeError("mix is None — no sources")
        if mix.ndim == 1:
            mix = np.column_stack([mix, mix])
        log.info("Mix shape before clip: %s", mix.shape)

        # ── Headroom + soft-clip ──────────────────────────────────────
        peak = float(np.max(np.abs(mix))) if mix.size else 0.0
        headroom_db = 20.0 * np.log10(peak) if peak > 1e-9 else -120.0
        if peak > 1.0:
            log.warning("Mixdown peak %.3f exceeded 1.0 — applying soft-clip", peak)
            mix = np.tanh(mix) * 0.95
        else:
            mix = mix * (0.95 / max(peak, 1e-6))
            mix = np.clip(mix, -0.95, 0.95)

        # ── Write 24-bit stereo WAV (std lib `wave` + numpy pack) ─────
        mix_bytes = self._float_to_int24_bytes(mix)
        with wave.open(str(final_path), "wb") as w:
            w.setnchannels(2)
            w.setsampwidth(3)  # 24-bit
            w.setframerate(TARGET_SR)
            w.writeframes(mix_bytes)
        size_bytes = final_path.stat().st_size
        log.info("Mixdown: %s (%.2fs, %.1f KB, peak=%.3f/%.2fdB, 24-bit @ %d Hz)",
                 final_path, mix.shape[0] / TARGET_SR, size_bytes / 1024,
                 peak, headroom_db, TARGET_SR)
        return {
            "size_bytes": size_bytes,
            "sc_samples": sc_samples,
            "shaper_samples": shaper_samples,
            "headroom_db": headroom_db,
        }

    @staticmethod
    def _read_wav(path: Path) -> tuple[int, np.ndarray]:
        """Read a PCM or IEEE_FLOAT WAV via stdlib wave, return (sr, float32 stereo [N,2]).

        Handles the formats SuperCollider writes:
          - WAVE_FORMAT_PCM (0x0001): int16, int24, int32
          - WAVE_FORMAT_IEEE_FLOAT (0x0003): 32-bit or 64-bit float
          - WAVE_FORMAT_EXTENSIBLE (0xFFFE): wraps the above
        Also tolerates extra chunks between fmt and data (PEAK, fact, etc).
        """
        with open(path, "rb") as f:
            # Walk chunks manually — stdlib's wave is too strict for SC's output
            riff = f.read(12)
            assert riff[:4] == b"RIFF", "not RIFF"
            assert riff[8:12] == b"WAVE", "not WAVE"

            fmt_code = sr = nch = bps = None
            data_chunks: list[bytes] = []
            while True:
                head = f.read(8)
                if len(head) < 8:
                    break
                cid = head[:4]
                csize = int.from_bytes(head[4:8], "little")
                if cid == b"fmt ":
                    fmt = f.read(csize)
                    fmt_code = int.from_bytes(fmt[0:2], "little")
                    nch = int.from_bytes(fmt[2:4], "little")
                    sr = int.from_bytes(fmt[4:8], "little")
                    bps = int.from_bytes(fmt[14:16], "little")
                elif cid == b"data":
                    data_chunks.append(f.read(csize))
                else:
                    # Skip unknown chunk (PEAK, fact, JUNK, etc.) with padding
                    f.read(csize + (csize % 2))
                if cid == b"data":
                    # data is always the last chunk we need; rest can be ignored
                    break

        if fmt_code is None:
            raise ValueError("fmt chunk missing")
        if not data_chunks:
            # SC writes a zero-byte data chunk when there's no real audio;
            # return silence of 1 frame so the caller can still produce a file
            out_sr = sr if sr is not None else 48000
            return out_sr, np.zeros((1, 2), dtype=np.float32)

        raw = b"".join(data_chunks)

        # Decode based on format
        if fmt_code in (0x0003,):  # IEEE_FLOAT
            if bps == 32:
                arr = np.frombuffer(raw, dtype="<f4").astype(np.float32)
            elif bps == 64:
                arr = np.frombuffer(raw, dtype="<f8").astype(np.float32)
            else:
                raise ValueError(f"unsupported IEEE_FLOAT bps={bps}")
        elif fmt_code == 0x0001:  # PCM
            if bps == 16:
                arr = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
            elif bps == 24:
                b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3)
                i = (b[:, 0].astype(np.int32)
                     | (b[:, 1].astype(np.int32) << 8)
                     | (b[:, 2].astype(np.int32) << 16))
                i[i & 0x800000 != 0] -= 0x1000000
                arr = i.astype(np.float32) / 8388608.0
            elif bps == 32:
                arr = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
            else:
                raise ValueError(f"unsupported PCM bps={bps}")
        elif fmt_code == 0xFFFE:  # EXTENSIBLE — sub-format GUID tells us what's inside
            # We already extracted fmt_code / bps from the standard fmt header
            # above, so treat as if it were the wrapped format. SC writes
            # IEEE_FLOAT in extensible containers.
            if bps == 32:
                arr = np.frombuffer(raw, dtype="<f4").astype(np.float32)
            else:
                raise ValueError(f"unsupported EXTENSIBLE bps={bps}")
        else:
            raise ValueError(f"unsupported format code 0x{fmt_code:04x}")

        # Reshape to (N, ch) and ensure stereo
        nch = nch or 2
        if nch == 1:
            arr = np.column_stack([arr, arr])
        else:
            arr = arr.reshape(-1, nch)[:, :2]
            if arr.shape[1] == 1:
                arr = np.column_stack([arr[:, 0], arr[:, 0]])
        out_sr = sr if sr is not None else 48000
        return out_sr, arr.astype(np.float32)

    @staticmethod
    def _resample_linear(audio: np.ndarray, src_sr: int, dst_sr: int) -> np.ndarray:
        """Linear-interpolation resample (N, ch) from src_sr to dst_sr.

        Fine for capture, not for mastering. ~2× faster than polyphase
        and avoids the scipy dep.
        """
        if src_sr == dst_sr:
            return audio
        n_src = audio.shape[0]
        n_dst = int(round(n_src * dst_sr / src_sr))
        if n_dst < 2:
            return audio[:n_dst]
        # Index in source for each destination sample
        x_src = np.linspace(0, n_src - 1, n_dst)
        x0 = np.floor(x_src).astype(np.int64)
        x1 = np.clip(x0 + 1, 0, n_src - 1)
        frac = (x_src - x0).astype(np.float32)[:, None]
        return ((1.0 - frac) * audio[x0] + frac * audio[x1]).astype(np.float32)

    @staticmethod
    def _float_to_int24_bytes(audio: np.ndarray) -> bytes:
        """Pack float32 (N, ch) [-1, 1] into int24 little-endian bytes.

        Interleaves channels: [L0,R0, L1,R1, ...] in 24-bit LE.
        """
        clipped = np.clip(audio, -0.9999, 0.9999)
        scaled = (clipped * 8388607.0).astype(np.int32)  # (N, ch)
        if scaled.ndim == 1:
            scaled = scaled[:, None]
        n, ch = scaled.shape
        # Flatten interleaved (L,R,L,R,...) for left-right-... channel order
        flat = scaled.reshape(-1)  # (N*ch,)
        b0 = (flat & 0xFF).astype(np.uint8)
        b1 = ((flat >> 8) & 0xFF).astype(np.uint8)
        b2 = ((flat >> 16) & 0xFF).astype(np.uint8)
        out = np.empty(flat.shape[0] * 3, dtype=np.uint8)
        out[0::3] = b0
        out[1::3] = b1
        out[2::3] = b2
        return out.tobytes()

    def _reset(self) -> None:
        self._running = False
        self._started_at = None
        self._final_path = None
        self._sc_path = None
        self._shaper_sink = []
        self._audio.detach_recorder()

    @staticmethod
    def _safe_name(label: str) -> str:
        s = re.sub(r"[^A-Za-z0-9._-]+", "_", label).strip("_")
        return s or "session"