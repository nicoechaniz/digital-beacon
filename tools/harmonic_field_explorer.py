"""harmonic_field_explorer.py — Interactive harmonic-mask browser for field recordings.

Load a sample, pick a fundamental F0 and bandwidth, then hear only the energy
that falls on the natural harmonic series f0·N. Designed to discover the tonal
center(s) hidden in nature recordings (water, frogs, insects, wind).

Endpoints:
  GET  /samples                -> JSON list of samples with analysis hints
  GET  /play/<id>              -> original WAV (mono 48kHz)
  POST /mask                   -> JSON body {id, f0, bandwidth_hz, n_harmonics}
                                 returns masked WAV
  GET  /spectrum/<id>?f0=...&bw=...&nh=... -> PNG with spectrum + harmonic lines

Usage:
    .venv/bin/python tools/harmonic_field_explorer.py
    open http://127.0.0.1:8780
"""
from __future__ import annotations

import argparse
import json
import logging
import mimetypes
import signal
import sys
import threading
import traceback
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse, unquote

import numpy as np

log = logging.getLogger("harmonic_field")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    import librosa
    import soundfile as sf
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    # Disable mathtext so titles/labels with special chars do not trigger the parser.
    matplotlib.rcParams['text.usetex'] = False
    matplotlib.rcParams['mathtext.default'] = 'regular'
    matplotlib.rcParams['axes.formatter.use_mathtext'] = False

    from harmonic_explorer_components import (
        AudioLoader,
        HarmonicAnalyzer,
        SpectrogramRenderer,
        HarmonicController,
        HarmonicPerformanceEngine,
        encode_wav,
        mask_harmonic_series,
    )
    _HAVE_DEPS = True
except Exception as exc:
    _HAVE_DEPS = False
    _DEPS_ERROR = exc


DEFAULT_PORT = 8780
DEFAULT_SAMPLE_DIR = Path.home() / "Music" / "field-recordings" / "wav"
DEFAULT_ANALYSIS = Path.home() / "Music" / "field-recordings" / "analysis" / "field_analysis.json"
SAFE_ID_RE = r"^[A-Za-z0-9_ .:-]{1,128}$"


_cache: dict[str, dict] = {}
_cache_lock = threading.Lock()
_analysis: dict[str, dict] = {}
_engine: Optional[HarmonicPerformanceEngine] = None


def _valid_id(sample_id: str) -> bool:
    import re
    return bool(re.match(SAFE_ID_RE, sample_id))


def _id_to_stem(sample_id: str) -> str:
    from urllib.parse import unquote
    return unquote(sample_id)


def _load_analysis(analysis_path: Path):
    global _analysis
    if not analysis_path.exists():
        return
    try:
        data = json.loads(analysis_path.read_text())
        for item in data:
            label = item.get("label", "")
            if label:
                _analysis[label] = item
    except Exception as exc:
        log.warning("Could not load analysis JSON: %s", exc)


def _load_sample(sample_id: str, sample_dir: Path, max_duration_s: Optional[float] = None,
                 offset_s: Optional[float] = None) -> dict:
    """Thin wrapper around AudioLoader for backward compatibility."""
    loader = AudioLoader(sample_dir)
    return loader.load(sample_id, max_duration_s=max_duration_s, offset_s=offset_s, centered=True)


def _harmonicity_score(y: np.ndarray, sr: int, f0: float, bandwidth_hz: float, n_harmonics: int = 32) -> float:
    """Backward-compatible wrapper around HarmonicAnalyzer."""
    return HarmonicAnalyzer(n_fft=8192).harmonicity(y, sr, f0, bandwidth_hz, n_harmonics)


def _find_candidates(y: np.ndarray, sr: int, f1_min: float = 20.0, f1_max: float = 200.0,
                     bandwidth_hz: float = 10.0, n_harmonics: int = 32, n_top: int = 5) -> list:
    """Backward-compatible wrapper around HarmonicAnalyzer."""
    return HarmonicAnalyzer(n_fft=8192).candidates(y, sr, f1_min, f1_max, bandwidth_hz, n_harmonics, n_top)


def _spectrogram_png(sample_id: str, f0: float, bandwidth_hz: float,
                     n_harmonics: int, sample_dir: Path, out_dir: Path,
                     max_duration_s: float = 60.0) -> Path:
    """Backward-compatible wrapper around SpectrogramRenderer."""
    loader = AudioLoader(sample_dir)
    renderer = SpectrogramRenderer(loader, out_dir)
    return renderer.spectrogram(sample_id, f0, bandwidth_hz, n_harmonics, max_duration_s)


def _spectrum_png(sample_id: str, f0: float, bandwidth_hz: float,
                  n_harmonics: int, sample_dir: Path, out_dir: Path,
                  max_duration_s: float = 120.0) -> Path:
    """Backward-compatible wrapper around SpectrogramRenderer."""
    loader = AudioLoader(sample_dir)
    renderer = SpectrogramRenderer(loader, out_dir)
    return renderer.spectrum(sample_id, f0, bandwidth_hz, n_harmonics, max_duration_s)


def _mask_harmonic_series(y: np.ndarray, sr: int, f0: float,
                          bandwidth_hz: float = 5.0,
                          n_harmonics: int = 32) -> np.ndarray:
    """Backward-compatible wrapper around harmonic_explorer_components.mask_harmonic_series."""
    return mask_harmonic_series(y, sr, f0, bandwidth_hz, n_harmonics)


def _encode_wav(y: np.ndarray, sr: int) -> bytes:
    """Backward-compatible wrapper around harmonic_explorer_components.encode_wav."""
    return encode_wav(y, sr)


@dataclass
class ServerConfig:
    sample_dir: Path
    analysis_path: Path
    port: int


class Handler(BaseHTTPRequestHandler):
    cfg: ServerConfig

    def log_message(self, fmt, *args):
        log.info(fmt % args)

    def _send_json(self, status: int, body: dict):
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(body, default=str).encode("utf-8"))
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_bytes(self, status: int, data: bytes, content_type: str):
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _read_json_body(self) -> Optional[dict]:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return None
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return None

    def _get_samples(self):
        samples = []
        for wav in sorted(self.cfg.sample_dir.glob("*.wav")):
            label = wav.stem
            analysis = _analysis.get(label, {})
            samples.append({
                "id": label,
                "label": label,
                "duration": analysis.get("duration_s", 0.0),
                "proposed_f0": analysis.get("proposed_f1_hz", 40.0),
                "peaks": analysis.get("peaks_hz", []),
            })
        return samples

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path == "/":
            self._serve_ui()
            return
        if path == "/samples":
            self._send_json(200, {"samples": self._get_samples()})
            return
        if path.startswith("/play/"):
            sample_id = unquote(path[6:])
            if not _valid_id(sample_id):
                self._send_json(400, {"error": "invalid id"})
                return
            try:
                info = _load_sample(sample_id, self.cfg.sample_dir)
                wav = _encode_wav(info["y"], info["sr"])
                self._send_bytes(200, wav, "audio/wav")
            except Exception as exc:
                log.error("/play error: %s", exc)
                self._send_json(500, {"error": str(exc)})
            return
        if path.startswith("/spectrum/"):
            sample_id = unquote(path[10:])
            if not _valid_id(sample_id):
                self._send_json(400, {"error": "invalid id"})
                return
            try:
                f0 = float(query.get("f0", [40.0])[0])
                bw = float(query.get("bw", [15.0])[0])
                nh = int(query.get("nh", [64])[0])
                max_dur = float(query.get("max_dur", [120.0])[0])
                out_dir = Path.home() / "Music" / "field-recordings" / "analysis" / "explorer"
                png = _spectrum_png(sample_id, f0, bw, nh, self.cfg.sample_dir, out_dir, max_dur)
                self._send_bytes(200, png.read_bytes(), "image/png")
            except Exception as exc:
                log.error("/spectrum error: %s", exc)
                self._send_json(500, {"error": str(exc)})
            return
        if path.startswith("/spectrogram/"):
            sample_id = unquote(path[13:])
            if not _valid_id(sample_id):
                self._send_json(400, {"error": "invalid id"})
                return
            try:
                f0 = float(query.get("f0", [40.0])[0])
                bw = float(query.get("bw", [15.0])[0])
                nh = int(query.get("nh", [64])[0])
                max_dur = float(query.get("max_dur", [60.0])[0])
                out_dir = Path.home() / "Music" / "field-recordings" / "analysis" / "explorer"
                png = _spectrogram_png(sample_id, f0, bw, nh, self.cfg.sample_dir, out_dir, max_dur)
                self._send_bytes(200, png.read_bytes(), "image/png")
            except Exception as exc:
                log.error("/spectrogram error: %s", exc)
                self._send_json(500, {"error": str(exc)})
            return
        if path == "/health":
            self._send_json(200, {"ok": True})
            return
        if path.startswith("/harmonicity/"):
            sample_id = unquote(path[13:])
            if not _valid_id(sample_id):
                self._send_json(400, {"error": "invalid id"})
                return
            try:
                f0 = float(query.get("f0", [40.0])[0])
                bw = float(query.get("bw", [15.0])[0])
                nh = int(query.get("nh", [64])[0])
                info = _load_sample(sample_id, self.cfg.sample_dir, max_duration_s=120.0)
                score = _harmonicity_score(info["y"], info["sr"], f0, bw, nh)
                self._send_json(200, {"f0": f0, "bandwidth_hz": bw, "n_harmonics": nh, "harmonicity": score})
            except Exception as exc:
                log.error("/harmonicity error: %s", exc)
                self._send_json(500, {"error": str(exc)})
            return
        if path.startswith("/candidates/"):
            sample_id = unquote(path[12:])
            if not _valid_id(sample_id):
                self._send_json(400, {"error": "invalid id"})
                return
            try:
                bw = float(query.get("bw", [15.0])[0])
                nh = int(query.get("nh", [64])[0])
                info = _load_sample(sample_id, self.cfg.sample_dir, max_duration_s=120.0)
                candidates = _find_candidates(info["y"], info["sr"], bandwidth_hz=bw, n_harmonics=nh)
                self._send_json(200, {"candidates": candidates})
            except Exception as exc:
                log.error("/candidates error: %s", exc)
                self._send_json(500, {"error": str(exc)})
            return
        self._send_json(404, {"error": "not found"})

    def do_HEAD(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        if path.startswith("/play/") or path == "/mask":
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
        elif path in ("/", "/spectrum/", "/spectrogram/") or path.startswith("/spectrum/") or path.startswith("/spectrogram/"):
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/save":
            self._handle_save()
            return
        if parsed.path == "/set_f1":
            self._handle_set_f1()
            return
        if parsed.path == "/voice_on":
            self._handle_voice_on()
            return
        if parsed.path == "/voice_off":
            self._handle_voice_off()
            return
        if parsed.path == "/panic":
            self._handle_panic()
            return
        if parsed.path == "/set_gain":
            self._handle_set_gain()
            return
        if parsed.path != "/mask":
            self._send_json(404, {"error": "not found"})
            return
        body = self._read_json_body()
        if not body:
            self._send_json(400, {"error": "empty or invalid JSON body"})
            return
        sample_id = body.get("id", "")
        if not _valid_id(sample_id):
            self._send_json(400, {"error": "invalid id"})
            return
        try:
            f0 = float(body.get("f0", 40.0))
            bw = float(body.get("bandwidth_hz", 15.0))
            nh = int(body.get("n_harmonics", 64))
            max_dur = float(body.get("max_duration_s", 60.0))
            f0 = max(20.0, min(2000.0, f0))
            bw = max(0.5, min(200.0, bw))
            nh = max(1, min(128, nh))
            max_dur = max(1.0, min(300.0, max_dur))
            info = _load_sample(sample_id, self.cfg.sample_dir, max_duration_s=max_dur)
            y = info["y"]
            sr = info["sr"]
            y_masked = _mask_harmonic_series(y, sr, f0, bw, nh)
            wav = _encode_wav(y_masked, sr)
            self._send_bytes(200, wav, "audio/wav")
        except Exception as exc:
            log.error("/mask error: %s\n%s", exc, traceback.format_exc())
            self._send_json(500, {"error": str(exc)})

    def _handle_set_f1(self):
        body = self._read_json_body()
        if not body:
            self._send_json(400, {"error": "empty or invalid JSON body"})
            return
        try:
            f1 = float(body.get("f1", 40.0))
            if _engine is not None:
                _engine.set_f1(f1)
            self._send_json(200, {"f1": f1, "engine": _engine is not None})
        except Exception as exc:
            log.error("/set_f1 error: %s", exc)
            self._send_json(500, {"error": str(exc)})

    def _handle_voice_on(self):
        body = self._read_json_body()
        if not body:
            self._send_json(400, {"error": "empty or invalid JSON body"})
            return
        try:
            n = int(body.get("n", 1))
            gain = body.get("gain")
            if gain is not None:
                gain = float(gain)
            if _engine is None:
                self._send_json(503, {"error": "performance engine not started"})
                return
            voice_id = _engine.voice_on(n, gain)
            self._send_json(200, {"voice_id": voice_id, "n": n})
        except Exception as exc:
            log.error("/voice_on error: %s", exc)
            self._send_json(500, {"error": str(exc)})

    def _handle_voice_off(self):
        body = self._read_json_body()
        if not body:
            self._send_json(400, {"error": "empty or invalid JSON body"})
            return
        try:
            voice_id = int(body.get("voice_id", 0))
            if _engine is None:
                self._send_json(503, {"error": "performance engine not started"})
                return
            _engine.voice_off(voice_id)
            self._send_json(200, {"voice_id": voice_id})
        except Exception as exc:
            log.error("/voice_off error: %s", exc)
            self._send_json(500, {"error": str(exc)})

    def _handle_panic(self):
        if _engine is not None:
            _engine.panic()
        self._send_json(200, {"ok": True})

    def _handle_set_gain(self):
        body = self._read_json_body()
        if not body:
            self._send_json(400, {"error": "empty or invalid JSON body"})
            return
        try:
            gain = float(body.get("gain", 0.6))
            if _engine is not None:
                _engine.set_gain(gain)
            self._send_json(200, {"gain": gain, "engine": _engine is not None})
        except Exception as exc:
            log.error("/set_gain error: %s", exc)
            self._send_json(500, {"error": str(exc)})

    def _handle_save(self):
        """Generate and save both the masked WAV and the spectrogram PNG to disk."""
        body = self._read_json_body()
        if not body:
            self._send_json(400, {"error": "empty or invalid JSON body"})
            return
        sample_id = body.get("id", "")
        if not _valid_id(sample_id):
            self._send_json(400, {"error": "invalid id"})
            return
        try:
            f0 = float(body.get("f0", 40.0))
            bw = float(body.get("bandwidth_hz", 5.0))
            nh = int(body.get("n_harmonics", 32))
            max_dur = float(body.get("max_duration_s", 60.0))
            f0 = max(20.0, min(2000.0, f0))
            bw = max(0.5, min(200.0, bw))
            nh = max(1, min(128, nh))
            max_dur = max(1.0, min(300.0, max_dur))
            out_dir = Path.home() / "Music" / "field-recordings" / "analysis" / "explorer"
            out_dir.mkdir(parents=True, exist_ok=True)
            stem = _id_to_stem(sample_id).replace(" ", "_")
            base = f"{stem}_f0-{f0:.1f}_bw-{bw:.1f}_nh-{nh}_dur-{max_dur:.0f}s"

            info = _load_sample(sample_id, self.cfg.sample_dir, max_duration_s=max_dur)
            y = info["y"]
            sr = info["sr"]

            wav_path = out_dir / f"{base}_masked.wav"
            y_masked = _mask_harmonic_series(y, sr, f0, bw, nh)
            wav = _encode_wav(y_masked, sr)
            wav_path.write_bytes(wav)

            png_path = _spectrogram_png(sample_id, f0, bw, nh, self.cfg.sample_dir, out_dir, max_dur)
            # rename to a more predictable path
            final_png = out_dir / f"{base}_spectrogram.png"
            png_path.rename(final_png)

            self._send_json(200, {
                "ok": True,
                "wav": str(wav_path),
                "png": str(final_png),
            })
        except Exception as exc:
            log.error("/save error: %s\n%s", exc, traceback.format_exc())
            self._send_json(500, {"error": str(exc)})

    def _serve_ui(self):
        html = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Harmonic Field Explorer</title>
<style>
* { box-sizing: border-box; }
:root {
  --bg: #0d1117;
  --panel: #161b22;
  --panel-2: #1c222b;
  --border: #30363d;
  --border-soft: #21262d;
  --text: #e6edf3;
  --muted: #9aa7b4;
  --faint: #6e7681;
  --accent: #58a6ff;
  --accent-dim: #1f6feb;
  --green: #238636;
  --green-hi: #2ea043;
  --radius: 10px;
}
html, body { height: 100%; }
body {
  font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  background: radial-gradient(1100px 520px at 22% -12%, #131a24, var(--bg)) fixed;
  color: var(--text);
  margin: 0;
  padding: 18px;
  font-size: 14px;
  line-height: 1.45;
}
.wrap { max-width: 1600px; margin: 0 auto; }

header { display: flex; align-items: baseline; gap: 14px; margin-bottom: 16px; flex-wrap: wrap; }
header h1 {
  font-size: 1.3rem; font-weight: 650; margin: 0; letter-spacing: -0.01em;
  background: linear-gradient(90deg, #58a6ff, #79c0ff);
  -webkit-background-clip: text; background-clip: text; color: transparent;
}
header .sub { font-size: 0.78rem; color: var(--faint); }

.main {
  display: grid;
  grid-template-columns: 300px minmax(0, 1fr);
  gap: 18px;
  align-items: start;
}

/* ---- Controls ---- */
.controls {
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 2px 14px 14px;
  position: sticky;
  top: 18px;
}
.section { padding: 13px 0; border-bottom: 1px solid var(--border-soft); }
.section:last-child { border-bottom: none; padding-bottom: 2px; }
.section .title {
  font-size: 0.67rem; text-transform: uppercase; letter-spacing: 0.09em;
  color: var(--faint); font-weight: 700; margin-bottom: 9px;
}

select {
  width: 100%; padding: 7px 9px; background: var(--bg); color: var(--text);
  border: 1px solid var(--border); border-radius: 7px; font-size: 0.9rem; cursor: pointer;
}
select:hover { border-color: var(--accent-dim); }

.slider {
  display: grid; grid-template-columns: 30px 1fr 52px;
  align-items: center; gap: 10px; margin: 9px 0;
}
.slider label { font-size: 0.8rem; color: var(--muted); font-weight: 600; margin: 0; }
.slider .val {
  text-align: right; color: var(--accent); font-weight: 650;
  font-variant-numeric: tabular-nums; font-size: 0.85rem;
}
input[type=range] { width: 100%; accent-color: var(--accent); height: 4px; cursor: pointer; margin: 0; }

.row { display: flex; gap: 7px; flex-wrap: wrap; margin-top: 7px; }
button {
  flex: 1 1 auto; background: var(--panel-2); color: var(--text);
  border: 1px solid var(--border); padding: 7px 10px; border-radius: 7px;
  cursor: pointer; font-size: 0.82rem; font-weight: 550;
  transition: background .12s ease, border-color .12s ease;
}
button:hover:not(:disabled) { border-color: var(--accent-dim); background: #262d38; }
button.primary { background: var(--green); border-color: transparent; color: #fff; }
button.primary:hover:not(:disabled) { background: var(--green-hi); }
button.accent { background: var(--accent-dim); border-color: transparent; color: #fff; }
button.accent:hover:not(:disabled) { background: #388bfd; }
button:disabled { opacity: 0.45; cursor: not-allowed; }

.readout {
  font-size: 0.82rem; color: var(--accent); font-weight: 600;
  margin: 10px 0 2px; min-height: 1.15em;
}
.peaks { font-size: 0.75rem; color: var(--muted); }
.peaks b { color: var(--faint); font-weight: 700; text-transform: uppercase; letter-spacing: 0.05em; font-size: 0.68rem; margin-right: 4px; }
.hint { font-size: 0.74rem; color: var(--faint); }

#candidates { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 10px; }
#candidates .chip {
  flex: 0 0 auto; padding: 4px 9px; font-size: 0.75rem; font-weight: 550;
  background: var(--bg); color: var(--muted);
  border: 1px solid var(--border); border-radius: 999px;
  font-variant-numeric: tabular-nums;
}
#candidates .chip:hover { border-color: var(--accent); color: var(--accent); background: #11223b; }

#status {
  font-size: 0.76rem; color: var(--muted); margin-top: 12px; min-height: 1.2em;
  font-variant-numeric: tabular-nums;
}

/* ---- Visualization ---- */
.viz { min-width: 0; }
.spec-container {
  position: relative; width: 100%; min-height: 260px;
  background: #000; border: 1px solid var(--border);
  border-radius: var(--radius); overflow: hidden;
}
.spec-container img { display: block; width: 100%; height: auto; }
.spec-container:not(.has-image) img { display: none; }
.spec-container .placeholder {
  position: absolute; inset: 0; display: flex; align-items: center; justify-content: center;
  color: var(--faint); font-size: 0.92rem; text-align: center; padding: 24px;
}
.spec-container.has-image .placeholder,
.spec-container.loading .placeholder { display: none; }
.spec-container.loading img { opacity: 0.35; filter: blur(1px); }
.spec-container .spinner {
  display: none; position: absolute; top: 50%; left: 50%;
  width: 34px; height: 34px; margin: -17px 0 0 -17px;
  border: 3px solid rgba(88,166,255,0.25); border-top-color: var(--accent); border-radius: 50%;
}
.spec-container.loading .spinner { display: block; animation: spin 0.7s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }

.legend {
  display: flex; gap: 20px; flex-wrap: wrap; align-items: center;
  margin-top: 11px; font-size: 0.76rem; color: var(--muted);
}
.legend .k { display: inline-flex; align-items: center; gap: 7px; }
.legend .sw { width: 18px; height: 0; border-top: 2px dashed; display: inline-block; }
.legend .sw.cyan { border-color: #22d3ee; }
.legend .sw.white { border-color: #d0d7de; }
.legend .sw.band { height: 12px; border: none; background: rgba(80,200,120,0.45); border-radius: 2px; }

@media (max-width: 820px) {
  .main { grid-template-columns: 1fr; }
  .controls { position: static; }
}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>Harmonic Field Explorer</h1>
    <span class="sub">linear 0–2000 Hz &nbsp;·&nbsp; exact n × f0 grid baked into the spectrogram</span>
  </header>

  <div class="main">
    <aside class="controls">
      <div class="section">
        <div class="title">Sample</div>
        <select id="sample"></select>
        <div class="row"><button id="load" class="accent">Reload</button></div>
        <div class="peaks" id="peaks" style="margin-top:9px"></div>
      </div>

      <div class="section">
        <div class="title">Harmonic series</div>
        <div class="slider"><label>F0</label><input type="range" id="f0" min="20" max="1000" step="0.2" value="40"><span class="val" id="f0-val">40.0</span></div>
        <div class="slider"><label>BW</label><input type="range" id="bw" min="0.5" max="80" step="0.5" value="15"><span class="val" id="bw-val">15.0</span></div>
        <div class="slider"><label>N</label><input type="range" id="nh" min="1" max="64" step="1" value="64"><span class="val" id="nh-val">64</span></div>
        <div class="readout" id="harmonicity"></div>
        <div id="candidates"></div>
      </div>

      <div class="section">
        <div class="title">View</div>
        <select id="view">
          <option value="spectrogram" selected>Spectrogram</option>
          <option value="spectrum">Long-term spectrum</option>
        </select>
        <div class="slider" style="margin-top:11px"><label>Win</label><input type="range" id="specdur" min="5" max="180" step="5" value="45"><span class="val" id="specdur-val">45s</span></div>
      </div>

      <div class="section">
        <div class="title">Playback</div>
        <div class="slider"><label>Len</label><input type="range" id="dur" min="5" max="120" step="5" value="45"><span class="val" id="dur-val">45s</span></div>
        <div class="row">
          <button id="play-orig">Original</button>
          <button id="play-live" class="accent">Live</button>
        </div>
        <div class="row">
          <button id="play-mask" class="primary">Mask</button>
          <button id="stop">Stop</button>
        </div>
      </div>

      <div class="section">
        <div class="title">Export</div>
        <div class="row"><button id="save-render" class="primary">Save WAV + PNG</button></div>
        <div class="row"><button id="download-spec">Download PNG</button></div>
      </div>

      <div class="section">
        <div class="title">Performance</div>
        <div class="slider"><label>Pad Gain</label><input type="range" id="pad-gain" min="0" max="1" step="0.01" value="0.6"><span class="val" id="pad-gain-val">0.60</span></div>
        <div class="row"><button id="panic" class="accent">Panic</button><button id="toggle-beacon">Beacon</button></div>
        <div class="row" id="harmonic-pads">
          <button data-n="1">1</button><button data-n="2">2</button><button data-n="3">3</button><button data-n="4">4</button>
          <button data-n="5">5</button><button data-n="6">6</button><button data-n="7">7</button><button data-n="8">8</button>
        </div>
        <div class="hint">Top 4 rows of Launchpad = sound-on-press. Bottom 4 rows = toggle.</div>
      </div>

      <div id="status">Loading…</div>
    </aside>

    <section class="viz">
      <div class="spec-container" id="spec-wrap">
        <div class="placeholder">Select a sample to render its spectrogram.</div>
        <img id="spectrum" alt="spectrogram">
        <div class="spinner"></div>
      </div>
      <div class="legend">
        <span class="k"><span class="sw cyan"></span> harmonics 1–8 × f0</span>
        <span class="k"><span class="sw white"></span> higher harmonics</span>
        <span class="k"><span class="sw band"></span> bandwidth passband</span>
        <span class="k" id="sample-info" style="margin-left:auto;color:var(--faint)"></span>
      </div>
    </section>
  </div>
</div>

<script>
const $ = id => document.getElementById(id);
const pf = el => parseFloat(el.value);
const pi = el => parseInt(el.value, 10);

const sampleSel = $('sample');
const f0Input = $('f0'), bwInput = $('bw'), nhInput = $('nh');
const f0Val = $('f0-val'), bwVal = $('bw-val'), nhVal = $('nh-val');
const specDurInput = $('specdur'), specDurVal = $('specdur-val');
const durInput = $('dur'), durVal = $('dur-val');
const viewSel = $('view');
const statusEl = $('status');
const peaksEl = $('peaks');
const harmonicityEl = $('harmonicity');
const candidatesEl = $('candidates');
const spectrumImg = $('spectrum');
const specWrap = $('spec-wrap');
const sampleInfoEl = $('sample-info');
const loadBtn = $('load');
const padGainInput = $('pad-gain'), padGainVal = $('pad-gain-val');
const playOrig = $('play-orig'), playLive = $('play-live'), playMask = $('play-mask'), stopBtn = $('stop');
const saveRender = $('save-render'), downloadSpec = $('download-spec');
const panicBtn = $('panic'), toggleBeaconBtn = $('toggle-beacon'), harmonicPads = $('harmonic-pads');

let audioCtx = null;
let currentAudio = null;
let currentAudioBuffer = null;
let liveNodes = null;
let currentSample = null;
let samples = [];
let debounceTimer = null;

const setStatus = m => { statusEl.textContent = m; };

function updateLabels() {
  f0Val.textContent = pf(f0Input).toFixed(1);
  bwVal.textContent = pf(bwInput).toFixed(1);
  nhVal.textContent = nhInput.value;
  specDurVal.textContent = specDurInput.value + 's';
  durVal.textContent = durInput.value + 's';
  padGainVal.textContent = pf(padGainInput).toFixed(2);
}

function sendPadGain() {
  const gain = pf(padGainInput);
  fetch('/set_gain', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ gain })
  }).catch(() => {});
}

async function init() {
  updateLabels();
  try {
    const res = await fetch('/samples');
    const data = await res.json();
    samples = data.samples || [];
    sampleSel.innerHTML = '';
    samples.forEach(s => {
      const opt = document.createElement('option');
      opt.value = s.id;
      opt.textContent = s.label + ' (' + Math.round(s.duration) + 's)';
      sampleSel.appendChild(opt);
    });
    if (samples.length) {
      loadSample();
    } else {
      setStatus('No samples found.');
    }
  } catch (e) {
    setStatus('Error loading samples: ' + e.message);
  }
}

function loadSample() {
  const id = sampleSel.value;
  const sample = samples.find(s => s.id === id);
  if (!sample) return;
  stopAll();
  currentSample = id;
  currentAudioBuffer = null;
  f0Input.value = (sample.proposed_f0 || 40).toFixed(1);
  updateLabels();
  const peaks = (sample.peaks || [])
    .map(p => (p && p.hz != null ? p.hz : p))
    .filter(v => typeof v === 'number')
    .slice(0, 8)
    .map(v => v.toFixed(0) + ' Hz');
  peaksEl.innerHTML = '<b>peaks</b>' + (peaks.length ? peaks.join(' · ') : '—');
  sampleInfoEl.textContent = sample.label + ' · ' + Math.round(sample.duration) + 's';
  updateSpectrum();
  updateHarmonicity();
  loadCandidates();
  updateLiveFilters(pf(f0Input), pf(bwInput), pi(nhInput));
  setStatus('Loaded ' + sample.label + ' — F0 ≈ ' + (sample.proposed_f0 || 40).toFixed(1) + ' Hz');
  loadAudioBuffer();
}

async function loadAudioBuffer() {
  if (!currentSample) return;
  if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
  try {
    const res = await fetch('/play/' + encodeURIComponent(currentSample));
    if (!res.ok) throw new Error('HTTP ' + res.status);
    currentAudioBuffer = await audioCtx.decodeAudioData(await res.arrayBuffer());
  } catch (e) {
    /* audio is optional for viewing; stay quiet */
  }
}

function updateSpectrum() {
  if (!currentSample) return;
  const f0 = pf(f0Input), bw = pf(bwInput), nh = pi(nhInput), maxDur = pi(specDurInput);
  const view = viewSel.value;
  const path = (view === 'spectrogram' ? '/spectrogram/' : '/spectrum/') + encodeURIComponent(currentSample);
  const url = path + '?f0=' + f0 + '&bw=' + bw + '&nh=' + nh + '&max_dur=' + maxDur + '&t=' + Date.now();
  specWrap.classList.add('loading');
  setStatus('Rendering ' + view + '…');
  spectrumImg.onload = () => {
    specWrap.classList.remove('loading');
    specWrap.classList.add('has-image');
    setStatus(view === 'spectrogram' ? 'Spectrogram ready.' : 'Spectrum ready.');
  };
  spectrumImg.onerror = () => {
    specWrap.classList.remove('loading');
    setStatus('Failed to render ' + view + '.');
  };
  spectrumImg.src = url;
}

function scheduleHeavyUpdate() {
  clearTimeout(debounceTimer);
  debounceTimer = setTimeout(() => { updateSpectrum(); updateHarmonicity(); }, 300);
}

async function updateHarmonicity() {
  if (!currentSample) return;
  const f0 = pf(f0Input), bw = pf(bwInput), nh = pi(nhInput);
  try {
    const res = await fetch('/harmonicity/' + encodeURIComponent(currentSample) + '?f0=' + f0 + '&bw=' + bw + '&nh=' + nh);
    if (!res.ok) return;
    const data = await res.json();
    harmonicityEl.textContent = 'Harmonicity: ' + (data.harmonicity * 100).toFixed(1) + '% of energy on the f0·N grid';
  } catch (e) {
    harmonicityEl.textContent = '';
  }
}

async function loadCandidates() {
  if (!currentSample) return;
  candidatesEl.innerHTML = '<span class="hint">searching for candidate fundamentals…</span>';
  const bw = pf(bwInput), nh = pi(nhInput);
  try {
    const res = await fetch('/candidates/' + encodeURIComponent(currentSample) + '?bw=' + bw + '&nh=' + nh);
    if (!res.ok) { candidatesEl.innerHTML = ''; return; }
    const data = await res.json();
    candidatesEl.innerHTML = '';
    (data.candidates || []).forEach(c => {
      const b = document.createElement('button');
      b.className = 'chip';
      b.type = 'button';
      b.textContent = c.f0.toFixed(1) + ' Hz · ' + (c.score * 100).toFixed(0) + '%';
      b.title = 'Set F0 = ' + c.f0.toFixed(1) + ' Hz';
      b.addEventListener('click', () => {
        f0Input.value = c.f0.toFixed(1);
        updateLabels();
        updateLiveFilters(pf(f0Input), pf(bwInput), pi(nhInput));
        updateSpectrum();
        updateHarmonicity();
      });
      candidatesEl.appendChild(b);
    });
    if (!candidatesEl.children.length) candidatesEl.innerHTML = '<span class="hint">no candidates found</span>';
  } catch (e) {
    candidatesEl.innerHTML = '';
  }
}

async function play(url, label) {
  stopAll();
  setStatus('Loading ' + label + '…');
  try {
    const res = await fetch(url);
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const blob = await res.blob();
    const audio = new Audio(URL.createObjectURL(blob));
    currentAudio = audio;
    audio.onended = () => setStatus(label + ' finished.');
    await audio.play();
    setStatus('Playing ' + label + '.');
  } catch (e) {
    setStatus('Error: ' + e.message);
  }
}

function stopAll() {
  if (currentAudio) { try { currentAudio.pause(); } catch (e) {} currentAudio = null; }
  stopLive();
}

function stopLive() {
  if (!liveNodes) return;
  try { liveNodes.source.stop(); } catch (e) {}
  try { liveNodes.source.disconnect(); } catch (e) {}
  liveNodes.filters.forEach(n => {
    try { n.filter.disconnect(); } catch (e) {}
    try { n.gain.disconnect(); } catch (e) {}
  });
  try { liveNodes.sumGain.disconnect(); } catch (e) {}
  try { liveNodes.masterGain.disconnect(); } catch (e) {}
  liveNodes = null;
}

function updateLiveFilters(f0, bw, nh) {
  if (!liveNodes) return;
  const active = Math.max(1, nh);
  liveNodes.sumGain.gain.setTargetAtTime(1 / Math.sqrt(active), audioCtx.currentTime, 0.02);
  liveNodes.filters.forEach((node, i) => {
    const freq = (i + 1) * f0;
    const q = freq / bw;
    const safeFreq = Math.min(freq, audioCtx.sampleRate / 2 - 1);
    node.filter.frequency.setTargetAtTime(safeFreq, audioCtx.currentTime, 0.02);
    node.filter.Q.setTargetAtTime(Math.max(0.1, q), audioCtx.currentTime, 0.02);
    node.gain.gain.setTargetAtTime(i < nh ? 1 : 0, audioCtx.currentTime, 0.02);
  });
}

async function startLiveFilter() {
  if (!currentSample) return;
  stopAll();
  setStatus('Starting live filter…');
  try {
    if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    if (!currentAudioBuffer) await loadAudioBuffer();
    if (!currentAudioBuffer) throw new Error('no audio buffer');
    if (audioCtx.state === 'suspended') await audioCtx.resume();

    const source = audioCtx.createBufferSource();
    source.buffer = currentAudioBuffer;
    source.loop = true;

    const masterGain = audioCtx.createGain();
    masterGain.gain.value = 0.75;

    const sumGain = audioCtx.createGain();
    sumGain.gain.value = 0;

    const filters = [];
    const maxFilters = 64;
    for (let i = 0; i < maxFilters; i++) {
      const f = audioCtx.createBiquadFilter();
      f.type = 'bandpass';
      f.frequency.value = 1;
      f.Q.value = 1;
      const g = audioCtx.createGain();
      g.gain.value = 0;
      source.connect(f);
      f.connect(g);
      g.connect(sumGain);
      filters.push({ filter: f, gain: g });
    }

    sumGain.connect(masterGain);
    masterGain.connect(audioCtx.destination);

    source.start(0);
    liveNodes = { source, filters, sumGain, masterGain };
    updateLiveFilters(pf(f0Input), pf(bwInput), pi(nhInput));
    setStatus('Live filter running — move F0 / BW / N for real-time changes.');
  } catch (e) {
    setStatus('Live error: ' + e.message);
  }
}

async function renderAndPlay() {
  if (!currentSample) return;
  const f0 = pf(f0Input), bw = pf(bwInput), nh = pi(nhInput), maxDur = pi(durInput);
  stopAll();
  setStatus('Rendering harmonic mask…');
  playMask.disabled = true;
  try {
    const res = await fetch('/mask', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: currentSample, f0, bandwidth_hz: bw, n_harmonics: nh, max_duration_s: maxDur })
    });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const blob = await res.blob();
    const audio = new Audio(URL.createObjectURL(blob));
    currentAudio = audio;
    audio.onended = () => setStatus('Mask playback finished.');
    await audio.play();
    setStatus('Playing mask — f0=' + f0.toFixed(1) + ' Hz, bw=' + bw.toFixed(1) + ' Hz, ' + maxDur + 's');
  } catch (e) {
    setStatus('Error: ' + e.message);
  } finally {
    playMask.disabled = false;
  }
}

async function saveRenderToDisk() {
  if (!currentSample) return;
  const f0 = pf(f0Input), bw = pf(bwInput), nh = pi(nhInput), maxDur = pi(durInput);
  setStatus('Saving WAV + PNG…');
  saveRender.disabled = true;
  try {
    const res = await fetch('/save', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: currentSample, f0, bandwidth_hz: bw, n_harmonics: nh, max_duration_s: maxDur })
    });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    const name = data.wav ? data.wav.split('/').slice(-1)[0] : 'files';
    setStatus('Saved ' + name + ' (+ PNG)');
  } catch (e) {
    setStatus('Save error: ' + e.message);
  } finally {
    saveRender.disabled = false;
  }
}

async function downloadSpectrogram() {
  if (!currentSample || !specWrap.classList.contains('has-image')) {
    setStatus('No image to download yet.');
    return;
  }
  setStatus('Preparing download…');
  try {
    const res = await fetch(spectrumImg.src);
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const blob = await res.blob();
    const f0 = pf(f0Input).toFixed(1), bw = pf(bwInput).toFixed(1), nh = pi(nhInput);
    const stem = currentSample.split(' ').join('_');
    const name = stem + '_' + viewSel.value + '_f0-' + f0 + '_bw-' + bw + '_nh-' + nh + '.png';
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = name;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setStatus('Downloaded ' + name);
  } catch (e) {
    setStatus('Download error: ' + e.message);
  }
}

function onHarmonicInput() {
  updateLabels();
  const f0 = pf(f0Input), bw = pf(bwInput), nh = pi(nhInput);
  updateLiveFilters(f0, bw, nh);
  // Forward the chosen F0 to the beacon / local performance engine.
  fetch('/set_f1', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ f1: f0 })
  }).catch(() => {});
  scheduleHeavyUpdate();
}

[f0Input, bwInput, nhInput].forEach(el => el.addEventListener('input', onHarmonicInput));
specDurInput.addEventListener('input', updateLabels);
specDurInput.addEventListener('change', updateSpectrum);
durInput.addEventListener('input', updateLabels);
viewSel.addEventListener('change', updateSpectrum);
if (padGainInput) padGainInput.addEventListener('input', () => { updateLabels(); sendPadGain(); });
loadBtn.addEventListener('click', loadSample);
sampleSel.addEventListener('change', loadSample);
playOrig.addEventListener('click', () => { if (currentSample) play('/play/' + encodeURIComponent(currentSample), 'original'); });
playLive.addEventListener('click', startLiveFilter);
playMask.addEventListener('click', renderAndPlay);
stopBtn.addEventListener('click', () => { stopAll(); setStatus('Stopped.'); });
saveRender.addEventListener('click', saveRenderToDisk);
downloadSpec.addEventListener('click', downloadSpectrogram);

// Performance controls
const activePads = new Map();

async function padDown(n) {
  try {
    const res = await fetch('/voice_on', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ n, gain: 0.6 })
    });
    if (!res.ok) return;
    const data = await res.json();
    activePads.set(n, data.voice_id);
  } catch (e) {}
}

async function padUp(n) {
  const voiceId = activePads.get(n);
  if (voiceId === undefined) return;
  activePads.delete(n);
  try {
    await fetch('/voice_off', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ voice_id: voiceId }) });
  } catch (e) {}
}

if (harmonicPads) {
  harmonicPads.querySelectorAll('button').forEach(btn => {
    const n = parseInt(btn.getAttribute('data-n'), 10);
    btn.addEventListener('mousedown', () => padDown(n));
    btn.addEventListener('mouseup', () => padUp(n));
    btn.addEventListener('mouseleave', () => padUp(n));
    btn.addEventListener('touchstart', (e) => { e.preventDefault(); padDown(n); });
    btn.addEventListener('touchend', (e) => { e.preventDefault(); padUp(n); });
  });
}

if (panicBtn) {
  panicBtn.addEventListener('click', () => {
    fetch('/panic', { method: 'POST' }).catch(() => {});
    setStatus('Panic sent.');
  });
}

if (toggleBeaconBtn) {
  let beaconOn = true;
  toggleBeaconBtn.addEventListener('click', () => {
    beaconOn = !beaconOn;
    toggleBeaconBtn.textContent = beaconOn ? 'Beacon ON' : 'Beacon OFF';
    toggleBeaconBtn.classList.toggle('accent', beaconOn);
    // F1 is already sent via slider; this button is just visual feedback for now.
    setStatus(beaconOn ? 'Beacon retune active.' : 'Beacon retune paused.');
  });
}

updateLabels();
init();
</script>
</body>
</html>
"""
        self._send_bytes(200, html.encode("utf-8"), "text/html")


def main():
    ap = argparse.ArgumentParser(description="Harmonic field explorer for field recordings")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--sample-dir", type=str, default=str(DEFAULT_SAMPLE_DIR))
    ap.add_argument("--analysis", type=str, default=str(DEFAULT_ANALYSIS))
    ap.add_argument("--no-audio", action="store_true", help="Disable local Shaper audio engine")
    ap.add_argument("--no-launchpad", action="store_true", help="Disable Launchpad Mini control")
    ap.add_argument("--no-beacon-osc", action="store_true", help="Disable OSC forwarding to beacon")
    ap.add_argument("--audio-device", type=str, default=None, help="Audio device ID or substring")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")

    if not _HAVE_DEPS:
        log.error("Missing dependencies: %s", _DEPS_ERROR)
        sys.exit(1)

    cfg = ServerConfig(
        sample_dir=Path(args.sample_dir).expanduser().resolve(),
        analysis_path=Path(args.analysis).expanduser().resolve(),
        port=args.port,
    )
    _load_analysis(cfg.analysis_path)

    global _engine
    if not args.no_audio:
        try:
            _engine = HarmonicPerformanceEngine(
                f1=40.0,
                audio_device=args.audio_device,
                enable_launchpad=not args.no_launchpad,
                enable_beacon_osc=not args.no_beacon_osc,
            )
            log.info("Performance engine started")
        except Exception as exc:
            log.warning("Could not start performance engine: %s", exc)

    Handler.cfg = cfg

    server = ThreadingHTTPServer(("127.0.0.1", cfg.port), Handler)
    server.allow_reuse_address = True
    log.info("Harmonic Field Explorer on http://127.0.0.1:%d", cfg.port)

    def _sigint(signum, frame):
        log.info("Signal %d — shutting down", signum)
        threading.Thread(target=server.shutdown, daemon=True).start()
        if _engine is not None:
            threading.Thread(target=_engine.stop, daemon=True).start()

    signal.signal(signal.SIGINT, _sigint)
    signal.signal(signal.SIGTERM, _sigint)

    try:
        server.serve_forever()
    finally:
        server.server_close()
        if _engine is not None:
            _engine.stop()
        log.info("Explorer stopped.")


if __name__ == "__main__":
    main()
