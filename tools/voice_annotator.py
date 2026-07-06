"""Web-based voice spectrogram annotator for PMP therapy sessions.

Serves on :8771. Provides:
  - Full-track spectrogram overview (pre-generated, cached PNG)
  - Click-to-seek on spectrogram → HTML5 audio transport
  - Marker placement with emotion labels and notes
  - Export markers as JSON for downstream extraction

Usage:
    python tools/voice_annotator.py [--port 8771] [--wav-dir PATH]
"""
from __future__ import annotations

import argparse
import io
import json
import logging
import mimetypes
import os
import re
import signal
import sys
import threading
import time
import wave
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse, parse_qs

import numpy as np

log = logging.getLogger("voice_annotator")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DEFAULT_PORT = 8771
DEFAULT_WAV_DIR = Path.home() / "Music" / "voice-analysis" / "normalized" / "PMP"
CACHE_DIR = Path.home() / "Music" / "voice-analysis" / "viz" / "annotator_cache"
MARKERS_DIR = Path.home() / "Music" / "voice-analysis" / "markers"

SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,128}$")
SPEC_FPS = 10  # spectrogram pixels per second of audio
SPEC_FMAX = 2000  # max frequency in Hz
SPEC_HEIGHT = 400  # pixels

# ---------------------------------------------------------------------------
# Spectrogram generation
# ---------------------------------------------------------------------------

def generate_spectrogram(wav_path: Path, cache_path: Path) -> dict:
    """Generate overview spectrogram PNG. Returns info dict with time_range etc."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import librosa
    import librosa.display

    # Load audio (mono)
    with wave.open(str(wav_path), "rb") as wf:
        sr = wf.getframerate()
        n_frames = wf.getnframes()
        nch = wf.getnchannels()
        raw = wf.readframes(n_frames)
        dtype = np.int16 if wf.getsampwidth() == 2 else np.float32
        y = np.frombuffer(raw, dtype=dtype).astype(np.float32)
        if nch > 1:
            y = y.reshape(-1, nch)
            mags = np.abs(y).max(axis=0)
            best = int(np.argmax(mags))
            y = y[:, best]
        y = y.ravel()

    duration = len(y) / sr
    hop_length = max(256, int(sr / SPEC_FPS))
    n_fft = 2048

    log.info("Computing spectrogram for %s (%.1f min, hop=%d)...",
             wav_path.name, duration / 60, hop_length)

    # Compute STFT magnitude in dB
    S = np.abs(librosa.stft(y, n_fft=n_fft, hop_length=hop_length))
    S_db = librosa.amplitude_to_db(S, ref=np.max)
    freqs = librosa.fft_frequencies(sr=sr, n_fft=n_fft)

    # Truncate to SPEC_FMAX
    freq_mask = freqs <= SPEC_FMAX
    S_db = S_db[freq_mask, :]
    freqs = freqs[freq_mask]

    # Render
    width_inches = S_db.shape[1] / 100  # ~100 px/inch
    height_inches = SPEC_HEIGHT / 100
    fig, ax = plt.subplots(figsize=(max(6, width_inches), max(3, height_inches)))
    img = librosa.display.specshow(
        S_db, sr=sr, hop_length=hop_length, x_axis="time", y_axis="linear",
        fmax=SPEC_FMAX, ax=ax, cmap="magma"
    )
    ax.set_xlabel("Time")
    ax.set_ylabel("Frequency (Hz)")
    ax.set_title(f"{wav_path.stem}  ({duration / 60:.1f} min)")

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(cache_path, dpi=100, bbox_inches="tight")
    plt.close(fig)

    img_width_px = int(S_db.shape[1] * (width_inches / (S_db.shape[1] / 100)) * 100 / 100) if width_inches else S_db.shape[1]
    # Actually compute from saved image
    from PIL import Image
    with Image.open(cache_path) as im:
        actual_w, actual_h = im.size

    log.info("Spectrogram saved: %s (%dx%d px)", cache_path.name, actual_w, actual_h)

    return {
        "duration_s": duration,
        "png_width": actual_w,
        "png_height": actual_h,
        "fps": SPEC_FPS,
        "pixels_per_second": actual_w / duration if duration > 0 else 0,
        "fmax": SPEC_FMAX,
    }


# ---------------------------------------------------------------------------
# Markers
# ---------------------------------------------------------------------------

def load_markers(wav_id: str) -> list[dict]:
    """Load markers JSON file. Returns [] if not found."""
    path = MARKERS_DIR / f"{wav_id}_markers.json"
    if path.exists():
        return json.loads(path.read_text())
    return []


def save_markers(wav_id: str, markers: list[dict]) -> None:
    """Save markers to JSON file."""
    MARKERS_DIR.mkdir(parents=True, exist_ok=True)
    path = MARKERS_DIR / f"{wav_id}_markers.json"
    path.write_text(json.dumps(markers, indent=2, ensure_ascii=False))


# ---------------------------------------------------------------------------
# Sample discovery
# ---------------------------------------------------------------------------

def discover_wavs(wav_dir: Path) -> list[dict]:
    """Return [{id, label, path, size_mb, duration_min}] for all WAVs."""
    if not wav_dir.is_dir():
        return []
    out = []
    for wav_path in sorted(wav_dir.rglob("*.wav")):
        stem = wav_path.stem
        size_mb = wav_path.stat().st_size / 1e6
        dur_min = None
        try:
            with wave.open(str(wav_path), "rb") as wf:
                dur = wf.getnframes() / wf.getframerate()
                dur_min = round(dur / 60, 1)
        except Exception:
            pass
        # Prefix label with source subfolder for clarity
        source = wav_path.parent.name
        out.append({
            "id": stem,
            "label": f"{source}/{stem}",
            "path": str(wav_path),
            "size_mb": round(size_mb, 1),
            "duration_min": dur_min,
        })
    return out


# ---------------------------------------------------------------------------
# HTTP Server
# ---------------------------------------------------------------------------

class AnnotatorHandler(BaseHTTPRequestHandler):
    wav_dir: Path = DEFAULT_WAV_DIR
    samples: list[dict] = []
    spec_cache: dict[str, dict] = {}  # wav_id → info dict

    def log_message(self, format, *args):
        log.info("HTTP %s", format % args)

    def _json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status, msg):
        self._json({"error": msg}, status)

    def _serve_file(self, path: Path, content_type: str):
        if not path.is_file():
            self._error(404, "not found")
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", path.stat().st_size)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        with open(path, "rb") as f:
            self.wfile.write(f.read())

    def _serve_audio_range(self, wav_path: Path):
        """Serve WAV with Range header support for seeking."""
        if not wav_path.is_file():
            self._error(404, "audio not found")
            return

        file_size = wav_path.stat().st_size
        range_header = self.headers.get("Range")

        if not range_header:
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(file_size))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            with open(wav_path, "rb") as f:
                self.wfile.write(f.read())
            return

        # Parse Range: bytes=START-END
        match = re.match(r"bytes=(\d+)-(\d*)", range_header)
        if not match:
            self._error(416, "invalid range")
            return

        start = int(match.group(1))
        end_str = match.group(2)
        end = int(end_str) - 1 if end_str else file_size - 1
        end = min(end, file_size - 1)

        self.send_response(206)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", end - start + 1)
        self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()

        with open(wav_path, "rb") as f:
            f.seek(start)
            self.wfile.write(f.read(end - start + 1))

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        # / → HTML UI
        if path == "/":
            self._serve_html()
            return

        # /api/samples
        if path == "/api/samples":
            self._json(AnnotatorHandler.samples)
            return

        # /api/spectrogram/<wav_id>
        if path.startswith("/api/spectrogram/"):
            wav_id = path.split("/api/spectrogram/", 1)[1]
            if not SAFE_ID_RE.match(wav_id):
                self._error(400, "invalid id")
                return
            self._serve_spectrogram(wav_id)
            return

        # /api/spec_info/<wav_id>
        if path.startswith("/api/spec_info/"):
            wav_id = path.split("/api/spec_info/", 1)[1]
            if not SAFE_ID_RE.match(wav_id):
                self._error(400, "invalid id")
                return
            self._serve_spec_info(wav_id)
            return

        # /api/audio/<wav_id>
        if path.startswith("/api/audio/"):
            wav_id = path.split("/api/audio/", 1)[1]
            if not SAFE_ID_RE.match(wav_id):
                self._error(400, "invalid id")
                return
            self._serve_audio(wav_id)
            return

        # /api/markers/<wav_id>
        if path.startswith("/api/markers/"):
            wav_id = path.split("/api/markers/", 1)[1]
            if not SAFE_ID_RE.match(wav_id):
                self._error(400, "invalid id")
                return
            markers = load_markers(wav_id)
            self._json(markers)
            return

        self._error(404, "not found")

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        if path.startswith("/api/markers/"):
            wav_id = path.split("/api/markers/", 1)[1]
            if not SAFE_ID_RE.match(wav_id):
                self._error(400, "invalid id")
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length)
                markers = json.loads(raw)
                save_markers(wav_id, markers)
                self._json({"ok": True, "count": len(markers)})
            except Exception as e:
                self._error(400, f"invalid JSON: {e}")
            return

        self._error(404, "not found")

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    # -- internal helpers --

    def _find_wav_path(self, wav_id: str) -> Optional[Path]:
        for s in AnnotatorHandler.samples:
            if s["id"] == wav_id:
                return Path(s["path"])
        return None

    def _serve_spectrogram(self, wav_id: str):
        wav_path = self._find_wav_path(wav_id)
        if not wav_path:
            self._error(404, "sample not found")
            return

        cache_path = CACHE_DIR / f"{wav_id}_spec.png"
        if not cache_path.exists():
            info = generate_spectrogram(wav_path, cache_path)
            AnnotatorHandler.spec_cache[wav_id] = info
        elif wav_id not in AnnotatorHandler.spec_cache:
            # Reconstruct info from cache
            from PIL import Image
            with Image.open(cache_path) as im:
                w, h = im.size
            # Best-effort duration from samples list
            dur = 0
            for s in AnnotatorHandler.samples:
                if s["id"] == wav_id and s.get("duration_min"):
                    dur = s["duration_min"] * 60
                    break
            AnnotatorHandler.spec_cache[wav_id] = {
                "duration_s": dur,
                "png_width": w,
                "png_height": h,
                "fps": SPEC_FPS,
                "pixels_per_second": w / dur if dur > 0 else 0,
                "fmax": SPEC_FMAX,
            }

        self._serve_file(cache_path, "image/png")

    def _serve_spec_info(self, wav_id: str):
        # Trigger spectrogram generation if needed
        wav_path = self._find_wav_path(wav_id)
        if not wav_path:
            self._error(404, "sample not found")
            return
        cache_path = CACHE_DIR / f"{wav_id}_spec.png"
        if wav_id not in AnnotatorHandler.spec_cache:
            if not cache_path.exists():
                generate_spectrogram(wav_path, cache_path)
            # Reconstruct minimal info
            from PIL import Image
            with Image.open(cache_path) as im:
                w, h = im.size
            dur = 0
            for s in AnnotatorHandler.samples:
                if s["id"] == wav_id:
                    dur = (s.get("duration_min") or 0) * 60
                    break
            AnnotatorHandler.spec_cache[wav_id] = {
                "duration_s": dur,
                "png_width": w,
                "png_height": h,
                "fps": SPEC_FPS,
                "pixels_per_second": w / dur if dur > 0 else 0,
                "fmax": SPEC_FMAX,
            }
        self._json(AnnotatorHandler.spec_cache.get(wav_id, {}))

    def _serve_audio(self, wav_id: str):
        wav_path = self._find_wav_path(wav_id)
        if not wav_path:
            self._error(404, "sample not found")
            return
        self._serve_audio_range(wav_path)

    def _serve_html(self):
        html = _build_html()
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)


# ---------------------------------------------------------------------------
# HTML UI
# ---------------------------------------------------------------------------

def _build_html() -> str:
    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Voice Annotator — PMP Sessions</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
       background: #0e1116; color: #c9d1d9; overflow-x: hidden; }
.header { background: #161b22; padding: 10px 16px; display: flex; align-items: center;
          gap: 12px; border-bottom: 1px solid #30363d; position: sticky; top: 0; z-index: 20;
          flex-wrap: wrap; }
.header .title { font-weight: 600; color: #58a6ff; letter-spacing: .2px; }
.header select { background: #21262d; color: #c9d1d9; border: 1px solid #30363d;
                 padding: 6px 10px; border-radius: 6px; font-size: 14px; min-width: 320px; }
button { background: #21262d; color: #c9d1d9; border: 1px solid #30363d; padding: 6px 14px;
         border-radius: 6px; cursor: pointer; font-size: 13px;
         transition: background .12s, border-color .12s; }
button:hover { background: #30363d; border-color: #8b949e; }
button.primary { background: #238636; border-color: #2ea043; color: #fff; }
button.primary:hover { background: #2ea043; }
button.accent { background: #1f6feb; border-color: #388bfd; color: #fff; }
button.accent:hover { background: #388bfd; }
.header label { font-size: 13px; color: #8b949e; }
.player-bar { background: #161b22; padding: 8px 16px; display: flex; align-items: center;
              gap: 12px; border-bottom: 1px solid #30363d; position: sticky; top: 49px; z-index: 19;
              flex-wrap: wrap; }
.player-bar #timeDisplay { font-size: 13px; font-family: ui-monospace, 'SF Mono', monospace;
                           color: #58a6ff; min-width: 110px; }
.status { padding: 8px 16px; font-size: 13px; display: none; border-bottom: 1px solid #30363d; }
.status.loading { background: #11202e; color: #58a6ff; }
.status.loading::before { content: ""; display: inline-block; width: 11px; height: 11px;
                          margin-right: 8px; vertical-align: -1px; border: 2px solid #30516e;
                          border-top-color: #58a6ff; border-radius: 50%;
                          animation: spin .8s linear infinite; }
.status.error { background: #2d1417; color: #ff7b72; border-bottom-color: #5a1e22; }
.status.ok { background: #12261b; color: #3fb950; }
@keyframes spin { to { transform: rotate(360deg); } }
.spec-container { overflow-x: auto; overflow-y: hidden; position: relative;
                  background: #000; min-height: 120px; cursor: crosshair; }
.spec-content { position: relative; padding-top: 78px; }
.ruler { position: absolute; top: 0; left: 0; right: 0; height: 18px; background: #0d1117;
         border-bottom: 1px solid #21262d; z-index: 7; overflow: hidden; pointer-events: none; }
.ruler .tick { position: absolute; top: 0; height: 18px; border-left: 1px solid #2d333b; }
.ruler .tick span { position: absolute; left: 4px; top: 3px; font-size: 10px; color: #8b949e;
                    font-family: ui-monospace, 'SF Mono', monospace; white-space: nowrap; }
.wave { position: absolute; top: 18px; left: 0; height: 30px; width: 0;
        background: #05070a; border-bottom: 1px solid #161b22; z-index: 2; }
.spec-img { display: block; max-width: none; }
.marker-overlay { position: absolute; top: 18px; left: 0; right: 0; bottom: 0;
                  pointer-events: none; z-index: 4; }
.marker-line { position: absolute; top: 0; bottom: 0; width: 2px; margin-left: -1px;
               background: #f0883e; pointer-events: auto; cursor: pointer; }
.marker-line:hover { background: #ffa657; width: 4px; margin-left: -2px; }
.marker-line.selected { background: #58a6ff; width: 4px; margin-left: -2px;
                        box-shadow: 0 0 6px rgba(88,166,255,.6); }
.marker-labels { position: absolute; top: 50px; left: 0; right: 0; height: 16px;
                 pointer-events: none; z-index: 5; overflow: hidden; }
.marker-label { position: absolute; font-size: 10px; line-height: 13px; height: 13px;
                background: rgba(240,136,62,.92); color: #0d1117; padding: 0 4px;
                border-radius: 3px; white-space: nowrap; font-weight: 600;
                transform: translateX(-1px); top: 0; }
.marker-label.selected { background: rgba(88,166,255,.95); }
.playhead { position: absolute; top: 18px; bottom: 0; width: 2px; margin-left: -1px; display: none;
            background: #58a6ff; pointer-events: none; z-index: 6;
            box-shadow: 0 0 6px rgba(88,166,255,.7); }
.seek-flash { position: absolute; top: 18px; bottom: 0; width: 28px; opacity: 0;
              pointer-events: none; z-index: 3;
              background: linear-gradient(90deg, rgba(88,166,255,0),
                          rgba(88,166,255,.5), rgba(88,166,255,0)); }
.seek-flash.on { animation: seekflash .55s ease-out forwards; }
@keyframes seekflash { from { opacity: .75; } to { opacity: 0; } }
.placeholder { padding: 48px 16px; text-align: center; color: #8b949e; font-size: 15px; }
.marker-panel { background: #161b22; padding: 14px 16px; border-top: 1px solid #30363d; }
.marker-panel h3 { font-size: 14px; margin-bottom: 10px; color: #58a6ff; }
.marker-list { max-height: 220px; overflow-y: auto; border: 1px solid #21262d; border-radius: 6px; }
.marker-list:empty { display: none; }
.marker-item { display: flex; align-items: center; gap: 10px; padding: 6px 10px;
               border-bottom: 1px solid #21262d; font-size: 13px; }
.marker-item:last-child { border-bottom: none; }
.marker-item:hover { background: #1c2128; }
.marker-item.sel { background: #16263a; }
.marker-item .time { font-family: ui-monospace, 'SF Mono', monospace; color: #58a6ff;
                     min-width: 64px; cursor: pointer; }
.marker-item .time:hover { text-decoration: underline; }
.marker-item .emotion { background: #21262d; border: 1px solid #30363d; color: #c9d1d9;
                        padding: 2px 8px; border-radius: 10px; font-size: 12px; }
.marker-item .notes { color: #8b949e; flex: 1; font-style: italic; }
.marker-item button { background: none; border: none; color: #f85149; cursor: pointer;
                      font-size: 14px; padding: 0 4px; }
.form-row { display: flex; gap: 8px; margin-top: 12px; align-items: center; flex-wrap: wrap; }
.form-row input, .form-row select { background: #21262d; color: #c9d1d9; border: 1px solid #30363d;
                                    padding: 6px 8px; border-radius: 6px; font-size: 13px; }
.form-row select { min-width: 130px; }
.form-row input[type=text] { flex: 1; min-width: 180px; }
.info { font-size: 12px; color: #8b949e; }
.hint { font-size: 11px; color: #6e7681; }
</style>
</head>
<body>

<div class="header">
  <span class="title">Voice Annotator</span>
  <label>Session:</label>
  <select id="wavSelect"><option value="">— Select WAV —</option></select>
  <button id="loadBtn" class="primary">Load</button>
  <button id="exportBtn">Export JSON</button>
  <span class="info" id="trackInfo"></span>
</div>

<div class="player-bar">
  <button id="playBtn">▶ Play</button>
  <button id="stopBtn">⏹ Stop</button>
  <span id="timeDisplay">0:00 / 0:00</span>
  <button id="addMarkerBtn" class="accent">📌 Add Marker</button>
  <span class="hint">Click = seek · Double-click = add marker · Space = play/pause · M = marker · ←/→ = ±5s</span>
</div>

<div id="status" class="status"></div>

<div id="specContainer" class="spec-container">
  <div id="specContent" class="spec-content" style="display:none">
    <div id="ruler" class="ruler"></div>
    <canvas id="waveCanvas" class="wave" width="10" height="30"></canvas>
    <img id="specImage" class="spec-img" alt="spectrogram" style="display:none">
    <div id="markerOverlay" class="marker-overlay"></div>
    <div id="markerLabels" class="marker-labels"></div>
    <div id="seekFlash" class="seek-flash"></div>
    <div id="playhead" class="playhead"></div>
  </div>
  <div id="specPlaceholder" class="placeholder">Select a session above and click <b>Load</b> to begin.</div>
</div>

<div class="marker-panel">
  <h3>Markers (<span id="markerCount">0</span>)</h3>
  <div class="marker-list" id="markerList"></div>
  <div class="form-row">
    <select id="emotionSelect">
      <option value="">Emotion…</option>
      <option>anger</option><option>sadness</option><option>fear</option>
      <option>joy</option><option>surprise</option><option>disgust</option>
      <option>calm</option><option>excited</option><option>tense</option>
      <option>grief</option><option>relief</option><option>anxious</option>
      <option>frustrated</option><option>hopeful</option><option>neutral</option>
    </select>
    <input type="text" id="noteInput" placeholder="Notes (optional)">
    <button id="saveMarkerBtn">Save edits</button>
  </div>
</div>

<audio id="audio" preload="auto" style="display:none"></audio>

<script>
"use strict";
const STATE = {
  wavId: null, markers: [], selectedMarkerIdx: -1,
  specInfo: null, map: null, audioReady: false, playing: false
};
const $ = id => document.getElementById(id);
const audio = $("audio");
// matplotlib renders the spectrogram with constant axis margins; used as a
// fallback when on-canvas plot-area detection is unavailable (e.g. huge images).
const FALLBACK_LEFT = 85, FALLBACK_RIGHT_MARGIN = 9;
let rafId = null;

// ---------- small helpers ----------
function fmtTime(s) {
  s = Math.max(0, s || 0);
  const m = Math.floor(s / 60), sec = Math.floor(s % 60);
  return m + ":" + String(sec).padStart(2, "0");
}
function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>]/g,
    c => c === "&" ? "&amp;" : c === "<" ? "&lt;" : "&gt;");
}
function setStatus(msg, kind) {
  const s = $("status");
  if (!msg) { s.style.display = "none"; s.textContent = ""; s.className = "status"; return; }
  s.textContent = msg; s.className = "status " + (kind || "loading"); s.style.display = "block";
}

// ---------- coordinate mapping (scroll-safe, plot-area aware) ----------
// getContentPixel + timeFromPixel MUST account for scrollLeft + getBoundingClientRect
// so that clicks after horizontal scrolling map to correct absolute time.
function getContentPixel(e) {
  const c = $("specContainer");
  const r = c.getBoundingClientRect();
  return e.clientX - r.left + c.scrollLeft;
}
function timeFromPixel(px) {
  const m = STATE.map;
  if (!m || m.plotW <= 0 || !m.duration) return 0;
  const t = (px - m.plotLeft) / m.plotW * m.duration;
  return Math.max(0, Math.min(t, m.duration));
}
function xFromTime(t) {
  const m = STATE.map;
  if (!m || !m.duration) return 0;
  return m.plotLeft + (Math.max(0, Math.min(t, m.duration)) / m.duration) * m.plotW;
}

// ---------- plot-area detection ----------
// Find the first row containing long contiguous dark run (axis spine).
function detectPlotArea(img, imgW, imgH) {
  const MAXC = 32000;
  if (imgW > MAXC || imgH > MAXC) return null;
  try {
    const scanH = Math.min(imgH, 140);
    const cv = document.createElement("canvas");
    cv.width = imgW; cv.height = scanH;
    const ctx = cv.getContext("2d", { willReadFrequently: true });
    ctx.drawImage(img, 0, 0);
    const data = ctx.getImageData(0, 0, imgW, scanH).data;
    const minRun = imgW * 0.5;
    for (let y = 0; y < scanH; y++) {
      let run = 0, start = 0, bestRun = 0, bestStart = 0;
      const base = y * imgW * 4;
      for (let x = 0; x < imgW; x++) {
        const o = base + x * 4;
        if (data[o] < 80 && data[o + 1] < 80 && data[o + 2] < 80) {
          if (run === 0) start = x;
          run++;
          if (run > bestRun) { bestRun = run; bestStart = start; }
        } else { run = 0; }
      }
      if (bestRun > minRun) return { plotLeft: bestStart, plotRight: bestStart + bestRun - 1 };
    }
  } catch (e) { return null; }
  return null;
}
function computeMap(img) {
  const imgW = img.naturalWidth || img.width;
  const imgH = img.naturalHeight || img.height;
  let plotLeft = FALLBACK_LEFT, plotRight = imgW - FALLBACK_RIGHT_MARGIN;
  const det = detectPlotArea(img, imgW, imgH);
  if (det && det.plotLeft >= 0 && det.plotLeft < imgW * 0.25 &&
      (det.plotRight - det.plotLeft) > imgW * 0.5) {
    plotLeft = det.plotLeft; plotRight = det.plotRight;
  }
  const dur = (audio.duration && isFinite(audio.duration))
    ? audio.duration : ((STATE.specInfo && STATE.specInfo.duration_s) || 0);
  STATE.map = { imgW, imgH, plotLeft, plotW: Math.max(1, plotRight - plotLeft), duration: dur };
}

// ---------- init / wiring ----------
async function init() {
  try {
    const samples = await (await fetch("/api/samples")).json();
    const sel = $("wavSelect");
    samples.forEach(s => {
      const opt = document.createElement("option");
      opt.value = s.id;
      const dur = s.duration_min != null ? s.duration_min + "min" : "?";
      opt.textContent = s.label + " (" + dur + ", " + s.size_mb + "MB)";
      sel.appendChild(opt);
    });
  } catch (e) { setStatus("Error: could not list sessions — " + e.message, "error"); }
  wireEvents();
}

function wireEvents() {
  $("loadBtn").onclick = loadWav;
  $("exportBtn").onclick = exportMarkers;
  $("playBtn").onclick = () => { audio.paused ? audio.play() : audio.pause(); };
  $("stopBtn").onclick = () => { audio.pause(); seekTo(0, true); };
  $("addMarkerBtn").onclick = () => addMarker(audio.currentTime || 0);
  $("saveMarkerBtn").onclick = saveCurrentMarker;
  $("wavSelect").onchange = () => { if ($("wavSelect").value) loadWav(); };

  const sc = $("specContainer");
  sc.addEventListener("click", e => {
    if (!STATE.map || e.target.classList.contains("marker-line")) return;
    const px = getContentPixel(e);
    seekTo(timeFromPixel(px), false);
  });
  sc.addEventListener("dblclick", e => {
    if (!STATE.map || e.target.classList.contains("marker-line")) return;
    const px = getContentPixel(e);
    addMarker(timeFromPixel(px));
  });

  $("markerList").addEventListener("click", e => {
    const t = e.target;
    if (t.dataset && t.dataset.del !== undefined) deleteMarker(+t.dataset.del);
    else if (t.classList.contains("time")) selectMarker(+t.dataset.i);
  });

  audio.addEventListener("loadedmetadata", () => {
    STATE.audioReady = true;
    if (STATE.map && audio.duration && isFinite(audio.duration)) {
      STATE.map.duration = audio.duration;
      drawRuler(); renderMarkers(); renderPlayheadAt(audio.currentTime);
    }
    updateTimeText();
  });
  audio.addEventListener("play", () => { STATE.playing = true; setPlayBtn(); });
  audio.addEventListener("pause", () => {
    STATE.playing = false; setPlayBtn(); renderPlayheadAt(audio.currentTime); updateTimeText();
  });
  audio.addEventListener("ended", () => { STATE.playing = false; setPlayBtn(); });
  audio.addEventListener("seeking", () => renderPlayheadAt(audio.currentTime));
  audio.addEventListener("seeked", () => { renderPlayheadAt(audio.currentTime); updateTimeText(); });
  audio.addEventListener("timeupdate", () => {
    if (audio.paused) { renderPlayheadAt(audio.currentTime); updateTimeText(); }
  });
  audio.addEventListener("error", () => {
    setStatus("Error: audio failed to load — playback unavailable.", "error");
  });

  document.addEventListener("keydown", e => {
    const tag = e.target.tagName;
    if (tag === "INPUT" || tag === "SELECT" || tag === "TEXTAREA") return;
    if (e.code === "Space") { e.preventDefault(); audio.paused ? audio.play() : audio.pause(); }
    else if (e.code === "KeyM") { if (STATE.map) addMarker(audio.currentTime || 0); }
    else if (e.code === "ArrowLeft") { seekTo((audio.currentTime || 0) - 5, true); }
    else if (e.code === "ArrowRight") { seekTo((audio.currentTime || 0) + 5, true); }
  });
}

// ---------- loading flow ----------
function resetView() {
  audio.pause();
  stopPlayheadLoop();
  $("specContent").style.display = "none";
  $("specImage").style.display = "none";
  $("specContent").style.width = "auto";
  $("ruler").innerHTML = "";
  $("markerOverlay").innerHTML = "";
  $("markerLabels").innerHTML = "";
  $("playhead").style.display = "none";
  const cv = $("waveCanvas"); cv.width = 10; cv.style.width = "0px"; cv.style.height = "30px";
  $("specContainer").scrollLeft = 0;
  $("specPlaceholder").style.display = "none";
}

async function loadWav() {
  const wavId = $("wavSelect").value;
  if (!wavId) return;
  STATE.wavId = wavId; STATE.markers = []; STATE.selectedMarkerIdx = -1;
  STATE.map = null; STATE.specInfo = null; STATE.audioReady = false;
  resetView();
  setStatus("Preparing spectrogram… (first load of a long session can take a while)", "loading");

  let info;
  try {
    const r = await fetch("/api/spec_info/" + wavId);
    if (!r.ok) throw new Error("HTTP " + r.status);
    info = await r.json();
  } catch (e) {
    setStatus("Error: could not load spectrogram info — " + e.message, "error");
    return;
  }
  if (STATE.wavId !== wavId) return;
  if (info && info.error) { setStatus("Error: " + info.error, "error"); return; }
  STATE.specInfo = info || {};
  $("trackInfo").textContent = STATE.specInfo.duration_s
    ? (STATE.specInfo.duration_s / 60).toFixed(1) + " min" : "";

  setStatus("Loading spectrogram image…", "loading");
  let ok = false;
  try {
    ok = await loadImage(wavId);
  } catch (e) {
    ok = false;
  }
  if (!ok || STATE.wavId !== wavId) {
    // status already set to error inside loadImage or here
    if (!$("status").textContent || !$("status").className.includes("error")) {
      setStatus("Error: spectrogram image failed to load.", "error");
    }
    return;
  }

  setStatus("Loading audio…", "loading");
  loadAudio(wavId);
  loadMarkers(wavId);
  loadWaveform(wavId);
}

function loadImage(wavId) {
  return new Promise(resolve => {
    const img = $("specImage");
    img.onload = () => {
      try {
        computeMap(img);
        $("specContent").style.width = STATE.map.imgW + "px";
        $("specContent").style.display = "block";
        img.style.display = "block";
        $("specPlaceholder").style.display = "none";
        drawRuler();
        renderMarkers();
        renderPlayheadAt(0);
        startPlayheadLoop();
        resolve(true);
      } catch (err) {
        setStatus("Error processing spectrogram: " + err.message, "error");
        resolve(false);
      }
    };
    img.onerror = () => {
      setStatus("Error: spectrogram image failed to load (check server logs).", "error");
      resolve(false);
    };
    img.src = "/api/spectrogram/" + wavId;
  });
}

function loadAudio(wavId) {
  audio.src = "/api/audio/" + wavId;
  audio.load();
  audio.addEventListener("canplay", () => { if (STATE.wavId === wavId) setStatus("", ""); }, { once: true });
}

async function loadMarkers(wavId) {
  try {
    const r = await fetch("/api/markers/" + wavId);
    STATE.markers = (r.ok ? (await r.json()) : []) || [];
  } catch (e) { STATE.markers = []; }
  if (STATE.wavId !== wavId) return;
  STATE.selectedMarkerIdx = -1;
  renderMarkers();
}

async function loadWaveform(wavId) {
  if (!STATE.map || STATE.wavId !== wavId) return;
  const plotW = Math.max(1, STATE.map.plotW);
  const nCols = Math.max(64, Math.min(Math.round(plotW), 12000));
  try {
    const resp = await fetch("/api/audio/" + wavId);
    if (STATE.wavId !== wavId) { try { resp.body && resp.body.cancel && resp.body.cancel(); } catch(e){} return; }
    if (!resp.ok) throw new Error("audio HTTP " + resp.status);
    const ab = await resp.arrayBuffer();
    if (STATE.wavId !== wavId) return;
    // Compute waveform from decoded AudioBuffer (standard, format agnostic)
    let audioBuf = null;
    try {
      const AC = window.AudioContext || window.webkitAudioContext;
      const ctx = new AC();
      audioBuf = await new Promise((resolve, reject) => {
        ctx.decodeAudioData(ab, resolve, reject);
      });
    } catch (de) {
      console.warn("decodeAudioData failed for waveform:", de);
      return;
    }
    if (STATE.wavId !== wavId || !audioBuf) return;
    const data = audioBuf.getChannelData(0);
    const peaks = computePeaksFromBuffer(data, nCols);
    if (STATE.wavId !== wavId) return;
    drawWave(peaks);
  } catch (e) {
    console.warn("waveform thumbnail unavailable:", e);
    // non-fatal, do not leave spinner / do not show blocking error
  }
}

// ---------- waveform thumbnail from audio buffer ----------
function computePeaksFromBuffer(samples, nCols) {
  const n = samples.length;
  if (n === 0 || nCols < 1) return { mn: new Float32Array(1), mx: new Float32Array(1), nCols: 1 };
  const mn = new Float32Array(nCols);
  const mx = new Float32Array(nCols);
  let gpeak = 0;
  for (let c = 0; c < nCols; c++) {
    const i0 = Math.floor(c * n / nCols);
    const i1 = Math.min(n, Math.floor((c + 1) * n / nCols));
    let lo = 1, hi = -1, seen = false;
    const step = Math.max(1, Math.floor((i1 - i0) / 32) || 1);
    for (let i = i0; i < i1; i += step) {
      const v = samples[i];
      if (!seen) { lo = hi = v; seen = true; continue; }
      if (v < lo) lo = v;
      if (v > hi) hi = v;
    }
    if (!seen) { lo = hi = 0; }
    mn[c] = lo; mx[c] = hi;
    gpeak = Math.max(gpeak, Math.abs(lo), Math.abs(hi));
  }
  if (gpeak > 1e-8) {
    const g = 1 / gpeak;
    for (let c = 0; c < nCols; c++) { mn[c] *= g; mx[c] *= g; }
  }
  return { mn, mx, nCols };
}

function drawWave(p) {
  const m = STATE.map; if (!m) return;
  const cv = $("waveCanvas");
  cv.width = p.nCols; cv.height = 30;
  cv.style.left = m.plotLeft + "px";
  cv.style.width = m.plotW + "px";
  cv.style.height = "30px";
  const ctx = cv.getContext("2d");
  ctx.clearRect(0, 0, cv.width, cv.height);
  const mid = 15, amp = 13.5;
  ctx.strokeStyle = "rgba(88,166,255,0.22)";
  ctx.beginPath(); ctx.moveTo(0, mid); ctx.lineTo(p.nCols, mid); ctx.stroke();
  ctx.strokeStyle = "#58a6ff"; ctx.lineWidth = 1;
  ctx.beginPath();
  for (let c = 0; c < p.nCols; c++) {
    let y0 = mid - p.mx[c] * amp, y1 = mid - p.mn[c] * amp;
    if (y1 < y0 + 0.5) y1 = y0 + 0.5;
    ctx.moveTo(c + 0.5, y0); ctx.lineTo(c + 0.5, y1);
  }
  ctx.stroke();
}

// ---------- time ruler (mm:ss labels) ----------
function niceInterval(dur, w) {
  const approx = dur * 110 / Math.max(1, w);
  const steps = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600];
  for (const s of steps) if (s >= approx) return s;
  return 3600;
}
function drawRuler() {
  const ruler = $("ruler"); ruler.innerHTML = "";
  const m = STATE.map; if (!m || !m.duration) return;
  const interval = niceInterval(m.duration, m.plotW);
  for (let t = 0; t <= m.duration + 0.001; t += interval) {
    const x = xFromTime(t);
    const tick = document.createElement("div");
    tick.className = "tick"; tick.style.left = x + "px";
    const span = document.createElement("span");
    span.textContent = fmtTime(t);
    tick.appendChild(span);
    ruler.appendChild(tick);
  }
}

// ---------- playhead: requestAnimationFrame loop (never loses sync on seek) ----------
function renderPlayheadAt(t) {
  if (!STATE.map) return 0;
  const x = xFromTime(t);
  const ph = $("playhead");
  ph.style.left = x + "px";
  ph.style.display = "block";
  return x;
}
function ensureVisible(x) {
  const c = $("specContainer");
  const vl = c.scrollLeft, vr = vl + c.clientWidth;
  if (x < vl + 40 || x > vr - 40) c.scrollLeft = Math.max(0, x - c.clientWidth * 0.5);
}
function followPlayhead(x) {
  const c = $("specContainer");
  const vl = c.scrollLeft, vr = vl + c.clientWidth;
  if (x < vl + 60 || x > vr - 60) c.scrollLeft = Math.max(0, x - c.clientWidth * 0.5);
}
function playheadLoop() {
  if (!STATE.map) { rafId = null; return; }
  const t = (audio && isFinite(audio.currentTime)) ? audio.currentTime : 0;
  const x = renderPlayheadAt(t);
  updateTimeText();
  if (!audio.paused && !audio.ended) {
    followPlayhead(x);
  }
  rafId = requestAnimationFrame(playheadLoop);
}
function startPlayheadLoop() {
  stopPlayheadLoop();
  rafId = requestAnimationFrame(playheadLoop);
}
function stopPlayheadLoop() {
  if (rafId) { cancelAnimationFrame(rafId); rafId = null; }
}

function seekTo(t, scroll) {
  const m = STATE.map; if (!m) return;
  t = Math.max(0, Math.min(t, m.duration || 0));
  try { audio.currentTime = t; } catch (e) {}
  const x = renderPlayheadAt(t);
  flashSeek(x);
  updateTimeText();
  if (scroll) ensureVisible(x);
}
function flashSeek(x) {
  const f = $("seekFlash");
  f.style.left = (x - 14) + "px";
  f.classList.remove("on");
  void f.offsetWidth;   // restart the CSS animation
  f.classList.add("on");
}
function setPlayBtn() { $("playBtn").textContent = audio.paused ? "▶ Play" : "⏸ Pause"; }
function updateTimeText() {
  const d = (STATE.map && STATE.map.duration) || (isFinite(audio.duration) ? audio.duration : 0);
  $("timeDisplay").textContent = fmtTime(audio.currentTime || 0) + " / " + fmtTime(d);
}

// ---------- markers ----------
function addMarker(t) {
  if (!STATE.map) return;
  t = Math.max(0, Math.min(+t || 0, STATE.map.duration || (+t || 0)));
  const emotion = $("emotionSelect").value || "neutral";
  const notes = $("noteInput").value || "";
  STATE.markers.push({ time_s: t, emotion, notes, created: new Date().toISOString() });
  STATE.markers.sort((a, b) => a.time_s - b.time_s);
  STATE.selectedMarkerIdx = STATE.markers.findIndex(m => m.time_s === t);
  saveMarkers();
  renderMarkers();
}
function selectMarker(i) {
  const m = STATE.markers[i]; if (!m) return;
  STATE.selectedMarkerIdx = i;
  $("emotionSelect").value = m.emotion || "";
  $("noteInput").value = m.notes || "";
  seekTo(m.time_s, true);
  renderMarkers();
}
function saveCurrentMarker() {
  const i = STATE.selectedMarkerIdx;
  if (i < 0 || !STATE.markers[i]) return;
  STATE.markers[i].emotion = $("emotionSelect").value || STATE.markers[i].emotion;
  STATE.markers[i].notes = $("noteInput").value;
  saveMarkers();
  renderMarkers();
}
function deleteMarker(i) {
  if (i < 0 || i >= STATE.markers.length) return;
  STATE.markers.splice(i, 1);
  if (STATE.selectedMarkerIdx === i) STATE.selectedMarkerIdx = -1;
  else if (STATE.selectedMarkerIdx > i) STATE.selectedMarkerIdx--;
  saveMarkers();
  renderMarkers();
}
async function saveMarkers() {
  if (!STATE.wavId) return;
  try {
    await fetch("/api/markers/" + STATE.wavId, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(STATE.markers)
    });
  } catch (e) { setStatus("Warning: could not save markers — " + e.message, "error"); }
}

function renderMarkers() {
  $("markerCount").textContent = STATE.markers.length;
  const overlay = $("markerOverlay"), labelsBox = $("markerLabels");
  if (!overlay || !labelsBox) return;
  overlay.innerHTML = "";
  labelsBox.innerHTML = "";
  if (!STATE.map) return;

  const labelEls = [];
  const xs = [];
  STATE.markers.forEach((mk, i) => {
    const x = xFromTime(mk.time_s);
    const line = document.createElement("div");
    line.className = "marker-line" + (i === STATE.selectedMarkerIdx ? " selected" : "");
    line.style.left = x + "px";
    line.title = fmtTime(mk.time_s) + " — " + (mk.emotion || "") +
                 (mk.notes ? " · " + mk.notes : "");
    line.addEventListener("click", ev => { ev.stopPropagation(); selectMarker(i); });
    overlay.appendChild(line);

    const lab = document.createElement("div");
    lab.className = "marker-label" + (i === STATE.selectedMarkerIdx ? " selected" : "");
    lab.textContent = (i + 1) + ": " + (mk.emotion || "?");
    lab.style.left = x + "px";
    labelsBox.appendChild(lab);
    labelEls.push(lab);
    xs.push(x);
  });

  // Label collision avoidance: HIDE labels that would render on top of each other.
  if (labelEls.length > 1) {
    labelsBox.getBoundingClientRect(); // force layout
    const order = xs.map((_, idx) => idx).sort((a, b) => xs[a] - xs[b]);
    let lastRight = -999;
    const GAP = 3;
    for (const i of order) {
      const lab = labelEls[i];
      const w = lab.offsetWidth || (lab.textContent.length * 6 + 8);
      const left = xs[i];
      const isSelected = (i === STATE.selectedMarkerIdx);
      if (left < lastRight + GAP && !isSelected) {
        lab.style.display = "none";
      } else {
        lab.style.display = "";
        lastRight = left + w;
      }
    }
  }

  const list = $("markerList");
  list.innerHTML = STATE.markers.map((m, i) => `
    <div class="marker-item${i === STATE.selectedMarkerIdx ? " sel" : ""}">
      <span class="time" data-i="${i}">${fmtTime(m.time_s)}</span>
      <span class="emotion">${esc(m.emotion || "?")}</span>
      <span class="notes">${esc(m.notes || "")}</span>
      <button data-del="${i}" title="Delete">✕</button>
    </div>`).join("");
}

function exportMarkers() {
  if (!STATE.markers.length) { alert("No markers to export."); return; }
  const blob = new Blob([JSON.stringify(STATE.markers, null, 2)], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = (STATE.wavId || "markers") + "_markers.json";
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}

init();
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Voice Spectrogram Annotator")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--wav-dir", type=str, default=str(DEFAULT_WAV_DIR))
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")

    wav_dir = Path(args.wav_dir).expanduser().resolve()
    AnnotatorHandler.wav_dir = wav_dir
    AnnotatorHandler.samples = discover_wavs(wav_dir)
    log.info("Found %d WAVs in %s", len(AnnotatorHandler.samples), wav_dir)

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    MARKERS_DIR.mkdir(parents=True, exist_ok=True)

    server = ThreadingHTTPServer(("127.0.0.1", args.port), AnnotatorHandler)

    def shutdown(*_):
        log.info("Shutting down...")
        server.shutdown()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    log.info("Voice Annotator ready: http://127.0.0.1:%d", args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
