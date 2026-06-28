"""HTTP server for the interactive Voice → Shaper UI.

Single-file stdlib server (no Flask/uvicorn). Binds to 127.0.0.1:8770 so it
does not collide with:
  - :8765  existing voice-analysis static file server (`python3 -m http.server`)
  - :8080  digital_beacon/api.py dashboard
  - :9001/:9002 OSC bridges

Endpoints
---------
  GET  /                  → inline HTML/JS UI (placeholder; filled by frontend task)
  GET  /samples           → JSON list of discovered voice samples (id, label, stats)
                            (served from in-memory cache; populated at startup)
  POST /samples/refresh   → force re-scan of voice_dir and refresh the cache;
                            returns {"ok": true, "count": N, "voice_dir": "..."}
  GET  /orig/<id>         → original WAV audio, streamed from the sample cache
                            (looks up path from the in-memory discovery list;
                            returns 404 {\"error\": \"sample not found\"} if unknown)
  POST /render            → STUB: returns 501 with JSON {error, detail}
                            until the render handler is wired (depends on the
                            synth_pure refactor + VoiceCache)
  GET  /viz/<path>        → static file server for existing PNGs under
                            ~/Music/voice-analysis/viz/
  GET  /health            → JSON health check

Sample discovery mirrors `tools/build_voice_compare_v3.py`: glob `*.wav`
under `~/Music/voice-analysis/` and skip derivative suffixes (_synth,
_orig, _mono, _clean, _filt, _0-9s, _side_by_side).

Usage
-----
    python tools/voice_shaper_server.py [--port 8770] [--host 127.0.0.1] [--voice-dir PATH]

    # curl checks
    curl -s http://127.0.0.1:8770/samples | python3 -m json.tool
    curl -s -X POST http://127.0.0.1:8770/samples/refresh | python3 -m json.tool
    curl -s -X POST http://127.0.0.1:8770/render \
        -H 'Content-Type: application/json' \
        -d '{"sample_id":"nico_voz_sample_02"}' -i

Ctrl-C triggers a graceful shutdown (server.shutdown() is called from a
worker thread after a SIGINT handler parks the serve loop).
"""

from __future__ import annotations

import argparse
import json
import logging
import mimetypes
import re
import signal
import sys
import threading
import time
import wave
import io
import base64
from http import HTTPStatus
from urllib.parse import urlparse, parse_qs
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Discovery + paths
# ---------------------------------------------------------------------------

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8770
DEFAULT_VOICE_DIR = Path.home() / "Music" / "voice-analysis"

# Mirror tools/build_voice_compare_v3.py:SKIP_SUFFIXES so the server picks
# up exactly the same set of "original" recordings as the dashboard builder.
SKIP_SUFFIXES = ("_synth", "_orig", "_mono", "_clean", "_filt", "_0-9s", "_side_by_side")

# Strict id charset for /orig/<id> and /samples[<id>]. Path traversal and
# extension smuggling both fail to match.
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

log = logging.getLogger("voice_shaper_server")

_SAMPLES_CACHE: Optional[list[dict[str, Any]]] = None
_voice_cache = None


def discover_samples(voice_dir: Path) -> list[dict[str, Any]]:
    """Return [{id, label, path, size_bytes, duration_s}] for usable samples.

    `id` and `label` are derived from the WAV stem (file name without
    extension). Size and duration are best-effort — duration is None when
    the file cannot be probed.
    """
    if not voice_dir.is_dir():
        log.warning("voice dir does not exist: %s", voice_dir)
        return []

    out: list[dict[str, Any]] = []
    for wav_path in sorted(voice_dir.glob("*.wav")):
        stem = wav_path.stem
        if any(stem.endswith(suf) for suf in SKIP_SUFFIXES):
            log.debug("skip derivative: %s", wav_path.name)
            continue
        try:
            size = wav_path.stat().st_size
        except OSError as exc:
            log.warning("stat failed for %s: %s", wav_path, exc)
            continue

        duration_s = _probe_duration_seconds(wav_path)
        out.append(
            {
                "id": stem,
                "label": stem.replace("_", " "),
                "filename": wav_path.name,
                "path": str(wav_path),
                "size_bytes": size,
                "duration_s": duration_s,
            }
        )

    log.info("discovered %d usable voice samples in %s", len(out), voice_dir)
    return out


def _probe_duration_seconds(wav_path: Path) -> Optional[float]:
    """Best-effort duration probe without pulling in soundfile/librosa.

    Reads the WAV header. Returns None on any parse error so a corrupt
    file does not break /samples.
    """
    try:
        with wave.open(str(wav_path), "rb") as w:
            frames = w.getnframes()
            rate = w.getframerate()
            if rate <= 0:
                return None
            return round(frames / float(rate), 3)
    except (wave.Error, OSError, EOFError) as exc:
        log.debug("duration probe failed for %s: %s", wav_path, exc)
        return None


def pick_loud_channel(y):
    """Reduce a stereo buffer to mono by selecting the channel with audio.

    R24 captures (and many other sources) record stereo with voice on Ch2
    and Ch1 holding monitor bleed at ~-90 dB. Hardcoding ``y[:, 0]`` would
    feed librosa a near-silent buffer and the synthesizer would produce
    nothing. Instead: if one channel is at least 5x louder than the other,
    use it; otherwise average.

    Returns y unchanged when it is already mono. Mirrors
    build_voice_compare_v3.pick_mono_channel.
    """
    if y.ndim == 1:
        return y
    mag_l = float(abs(y[:, 0]).max() if hasattr(y[:, 0], 'max') else abs(y[:, 0]))
    mag_r = float(abs(y[:, 1]).max() if hasattr(y[:, 1], 'max') else abs(y[:, 1]))
    if mag_r > mag_l * 5:
        return y[:, 1]
    if mag_l > mag_r * 5:
        return y[:, 0]
    return y.mean(axis=1)


def generate_spectrogram_png(y, sr, title="", n_fft=2048, hop_length=512, fmax=4000):
    """Generate a spectrogram PNG as base64 string.
    Returns base64-encoded PNG bytes.
    """
    import numpy as np
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import librosa
    import librosa.display
    import io
    import base64
    S = np.abs(librosa.stft(y, n_fft=n_fft, hop_length=hop_length))
    S_db = librosa.amplitude_to_db(S, ref=np.max(S))
    fig, ax = plt.subplots(figsize=(12, 4), facecolor='#0e1116')
    ax.set_facecolor('#000')
    img = librosa.display.specshow(
        S_db, sr=sr, hop_length=hop_length, x_axis='time', y_axis='hz',
        cmap='magma', ax=ax, fmax=fmax
    )
    fig.colorbar(img, ax=ax, format='%+2.0f dB', label='dB')
    ax.set_title(title, color='#c9d1d9', fontsize=12)
    buf = io.BytesIO()
    fig.savefig(buf, format='png', dpi=100, bbox_inches='tight', facecolor='#0e1116')
    plt.close(fig)
    buf.seek(0)
    return base64.b64encode(buf.read()).decode('ascii')


def refresh_samples_cache(voice_dir: Path) -> int:
    """Call discover_samples and store the result in the module-level cache.

    Returns the count of samples (for logging and response payloads).
    discover_samples() signature is unchanged; this is the only writer.
    """
    global _SAMPLES_CACHE
    _SAMPLES_CACHE = discover_samples(voice_dir)
    count = len(_SAMPLES_CACHE)
    log.info("samples cache refreshed: %d samples from %s", count, voice_dir)
    return count


# ---------------------------------------------------------------------------
# Inline UI placeholder (replaced by the frontend task)
# ---------------------------------------------------------------------------

PLACEHOLDER_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Voice → Shaper</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 16px 20px;
    background: #0e1116; color: #c9d1d9;
    font: 14px/1.45 -apple-system, "Segoe UI", system-ui, sans-serif;
    overflow-x: auto;
  }
  .container { max-width: 980px; margin: 0 auto; }
  h1 { margin: 0 0 12px; font-size: 20px; color: #58a6ff; font-weight: 600; }
  h2 { margin: 0 0 8px; font-size: 13px; color: #8b949e; text-transform: uppercase; letter-spacing: .04em; font-weight: 600; }
  .card {
    background: #161b22; border: 1px solid #30363d; border-radius: 8px;
    padding: 14px 16px; margin-bottom: 12px;
  }
  .row { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
  label { font-size: 11px; color: #8b949e; display: block; margin-bottom: 3px; }
  select, input[type=number] {
    background: #0d1117; color: #c9d1d9; border: 1px solid #30363d;
    border-radius: 4px; padding: 4px 6px; font: inherit;
  }
  input[type=range] { accent-color: #58a6ff; }
  .globals-row { display: flex; gap: 12px; flex-wrap: wrap; }
  .globals-row .ctrl { min-width: 110px; }
  .hstrip { display: flex; flex-wrap: wrap; gap: 8px; }
  .hcol {
    display: flex; flex-direction: column; align-items: center;
    background: #0d1117; border: 1px solid #30363d; border-radius: 6px;
    padding: 6px 4px; min-width: 50px;
  }
  .hcol .hname { font-size: 10px; color: #8b949e; margin-bottom: 1px; text-align:center; }
  .hcol .hfreq { font-size: 9px; color: #555; margin-bottom: 3px; }
  .hcol .hval { font-family: monospace; font-size: 12px; color: #58a6ff; margin: 2px 0 3px; }
  .hcol select { width: 100%; font-size: 10px; padding: 1px 2px; }
  .vslider {
    appearance: slider-vertical;
    writing-mode: vertical-lr;
    direction: rtl;
    height: 140px;
    width: 24px;
    accent-color: #58a6ff;
    background: #21262d;
  }
  .actions { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 4px; }
  button {
    background: #21262d; border: 1px solid #30363d; color: #c9d1d9;
    padding: 8px 14px; border-radius: 4px; font: inherit; cursor: pointer;
  }
  button.play {
    background: #3fb950; color: #0e1116; border: none; font-weight: 600;
  }
  button.primary {
    background: #58a6ff; color: #0e1116; border: none; font-weight: 600;
  }
  button:disabled { opacity: 0.6; cursor: default; }
  .status { margin-top: 8px; font-size: 12px; font-family: monospace; min-height: 1.4em; }
  .status.err { color: #f85149; }
  .status.ok { color: #3fb950; }
  .sub { color: #8b949e; font-size: 12px; }
  .ctrl { margin-bottom: 6px; }
</style>
</head>
<body>
<div class="container">
  <h1>Voice &rarr; Shaper &mdash; Interactive Mixer</h1>

  <div class="card">
    <div class="row">
      <div style="flex:1; min-width:220px;">
        <label>Sample</label>
        <select id="sample" style="width:100%;"></select>
      </div>
      <div><span id="meta" class="sub"></span></div>
    </div>
  </div>

  <div class="card">
    <h2>Global Controls</h2>
    <div class="globals-row">
      <div class="ctrl">
        <label>Gain Curve</label>
        <select id="gain_curve">
          <option value="linear" selected>linear</option>
          <option value="sqrt">sqrt</option>
          <option value="square">square</option>
        </select>
      </div>
      <div class="ctrl">
        <label>Thresh (dB)</label>
        <input id="thresh_db" type="number" min="-96" max="0" step="0.1" value="-30">
      </div>
      <div class="ctrl">
        <label>Tilt (dB/oct)</label>
        <input id="tilt_db" type="number" min="-48" max="0" step="0.5" value="-12">
      </div>
      <div class="ctrl">
        <label>Noise Floor (dB)</label>
        <input id="noise_floor_db" type="number" min="-96" max="0" step="1" value="-40">
      </div>
      <div class="ctrl">
        <label>Max Voices</label>
        <input id="max_voices" type="number" min="1" max="32" step="1" value="32">
      </div>
      <div class="ctrl">
        <label>Master Gain <span id="val-master-gain">0.70</span></label>
        <input id="master_gain" type="range" min="0" max="2" step="0.05" value="0.7"
               style="width:130px;">
      </div>
      <div class="ctrl">
        <label>Noise Mix <span id="val-noise-mix">-12.0 dB</span></label>
        <input id="noise_mix_db" type="range" min="-120" max="0" step="1" value="-12"
               style="width:130px;">
        <label style="font-size:11px;margin-left:4px;"><input type="checkbox" id="noise_bypass"> mute</label>
      </div>
    </div>
  </div>

  <div class="card">
    <h2>Harmonics</h2>
    <div id="harmonics" class="hstrip"></div>
  </div>

<audio id="original" controls style="width:100%;margin-bottom:8px;"></audio>
<audio id="synth" controls style="width:100%;margin-bottom:8px;"></audio>

  <div class="actions">
    <button id="play-orig" class="play">▶ Play Original</button>
    <button id="play-synth" class="play">▶ Play Synth</button>
    <button id="play-both" class="play">▶ Play Both</button>
    <button id="render-btn" class="primary">Render</button>
    <label style="display:inline-flex;align-items:center;gap:4px;margin-left:8px;font-size:12px;">
      <input type="checkbox" id="auto-render"> Auto-render on change
    </label>
    <button id="reset-btn">Reset to Defaults</button>
    <button id="save-btn">Save Preset</button>
    <button id="load-btn">Load Preset</button>
    <button id="download-btn">Download WAV</button>
  </div>

  <div class="card">
    <h2>Rendered</h2>
    <audio id="render-audio" controls style="width:100%;"></audio>
    <div class="sub" style="margin-top:4px;">
      <a id="download-link" style="display:none; color:#58a6ff;" href="#" download>Download WAV</a>
    </div>
  </div>

  <div id="status" class="status"></div>
  <div id="spec-container" style="display:none;margin-top:12px;">
    <h2>Spectrograms</h2>
    <div class="panel" style="display:flex;flex-direction:column;gap:8px;">
      <img id="spec-orig" style="width:100%;border-radius:4px;" alt="Original spectrogram">
      <img id="spec-synth" style="width:100%;border-radius:4px;" alt="Synth spectrogram">
    </div>
  </div>
</div>

<script>
const DEFAULT_STATE = {
  currentSample: '',
  f0_mean: 0,
  gain_curve: 'linear',
  thresh_db: -30,
  tilt_db: -12,
  max_voices: 32,
  noise_mix_db: -12,
  noise_floor_db: -40,
  master_gain: 0.7,
  per_harmonic_gains: Array(32).fill(1.0),
  wave_shapes: Array(32).fill('sine')
};
let state = JSON.parse(JSON.stringify(DEFAULT_STATE));
let lastBlobUrl = null;
let debounceTimer = null;
let originalAudioEl = null;
let synthAudioEl = null;
let samplesList = [];

function setStatus(msg, type) {
  const el = document.getElementById('status');
  el.textContent = msg || '';
  el.className = 'status' + (type === 'err' ? ' err' : type === 'ok' ? ' ok' : '');
}

function makeHarmonics() {
  const root = document.getElementById('harmonics');
  root.innerHTML = '';
  const waveOpts = ['sine','square','saw','triangle'];
  const nv = state.max_voices;
  for (let i=0; i<nv; i++) {
    const n = i+1;
    const freq = state.f0_mean ? Math.round(state.f0_mean * n) : 0;
    const freqText = freq ? '<div class="hfreq">' + freq + ' Hz</div>' : '<div class="hfreq">—</div>';
    const d = document.createElement('div');
    d.className = 'hcol';
    d.innerHTML = `
      <div class="hname">H${n}</div>
      ${freqText}
      <input type="range" class="vslider" id="gain${i}" min="0" max="2" step="0.01" value="${state.per_harmonic_gains[i] || 1}">
      <div class="hval" id="val${i}">${(state.per_harmonic_gains[i] || 1).toFixed(2)}</div>
      <select id="shape${i}">${
        waveOpts.map(w => `<option value="${w}"${w==='sine'?' selected':''}>${w}</option>`).join('')
      }</select>
    `;
    root.appendChild(d);
    const rng = d.querySelector('#gain'+i);
    const outv = d.querySelector('#val'+i);
    const sync = () => {
      outv.textContent = parseFloat(rng.value).toFixed(2);
      state.per_harmonic_gains[i] = parseFloat(rng.value);
    };
    rng.addEventListener('input', sync);
    const shapeSel = d.querySelector('#shape'+i);
    if (shapeSel) shapeSel.addEventListener('change', () => {
      state.wave_shapes[i] = shapeSel.value;
    });
    sync();
  }
}

function syncDOMToState() {
  const sel = document.getElementById('sample');
  state.currentSample = sel ? (sel.value || '') : '';
  const si = samplesList.find(s => s.id === state.currentSample);
  state.f0_mean = (si && si.f0_mean) ? si.f0_mean : 0;
  state.gain_curve = document.getElementById('gain_curve').value;
  state.thresh_db = parseFloat(document.getElementById('thresh_db').value) || -30;
  state.tilt_db = parseFloat(document.getElementById('tilt_db').value) || -12;
  state.max_voices = parseInt(document.getElementById('max_voices').value, 10) || 6;
  const mg = document.getElementById('master_gain');
  state.master_gain = mg ? parseFloat(mg.value) : 0.7;
  const nm = document.getElementById('noise_mix_db');
  const nb = document.getElementById('noise_bypass');
  state.noise_mix_db = (nb && nb.checked) ? -120.0 : (nm ? parseFloat(nm.value) : -12.0);
  state.noise_floor_db = parseFloat(document.getElementById('noise_floor_db').value) || -40;
  state.per_harmonic_gains = [];
  state.wave_shapes = [];
  const nv = state.max_voices;
  for (let i=0; i<nv; i++) {
    const g = document.getElementById('gain'+i);
    const s = document.getElementById('shape'+i);
    state.per_harmonic_gains.push( g ? parseFloat(g.value) : 1.0 );
    state.wave_shapes.push( s ? s.value : 'sine' );
  }
}

function syncStateToDOM() {
  const sel = document.getElementById('sample');
  if (sel && state.currentSample) {
    if (Array.from(sel.options).some(o => o.value === state.currentSample)) {
      sel.value = state.currentSample;
    }
  }
  const gc = document.getElementById('gain_curve');
  if (gc) gc.value = state.gain_curve;
  const th = document.getElementById('thresh_db');
  if (th) th.value = state.thresh_db;
  const ti = document.getElementById('tilt_db');
  if (ti) ti.value = state.tilt_db;
  const mv = document.getElementById('max_voices');
  if (mv) mv.value = state.max_voices;
  const mg = document.getElementById('master_gain');
  if (mg) mg.value = state.master_gain != null ? state.master_gain : 0.7;
  const mgv = document.getElementById('val-master-gain');
  if (mgv) mgv.textContent = parseFloat(mg ? mg.value : (state.master_gain || 0.7)).toFixed(2);
  const nm = document.getElementById('noise_mix_db');
  const nmv = document.getElementById('val-noise-mix');
  if (nm) nm.value = state.noise_mix_db != null ? state.noise_mix_db : -12.0;
  if (nmv) nmv.textContent = parseFloat(nm ? nm.value : (state.noise_mix_db || -12)).toFixed(1) + ' dB';
  const nb = document.getElementById('noise_bypass');
  if (nb) nb.checked = (state.noise_mix_db <= -120);
  const nf = document.getElementById('noise_floor_db');
  if (nf) nf.value = state.noise_floor_db != null ? state.noise_floor_db : -40;
  const nv = state.max_voices;
  for (let i=0; i<nv; i++) {
    const g = document.getElementById('gain'+i);
    const s = document.getElementById('shape'+i);
    const v = document.getElementById('val'+i);
    if (g) g.value = state.per_harmonic_gains[i] != null ? state.per_harmonic_gains[i] : 1.0;
    if (v) v.textContent = parseFloat(g ? g.value : (state.per_harmonic_gains[i] || 1)).toFixed(2);
    if (s) s.value = state.wave_shapes[i] || 'sine';
  }
  updateMeta();
}

function updateReadouts() {
  const nv = state.max_voices;
  for (let i=0; i<nv; i++) {
    const g = document.getElementById('gain'+i);
    const v = document.getElementById('val'+i);
    if (g && v) v.textContent = parseFloat(g.value).toFixed(2);
  }
  const mg = document.getElementById('master_gain');
  const mgv = document.getElementById('val-master-gain');
  if (mg && mgv) mgv.textContent = parseFloat(mg.value).toFixed(2);
  const nm = document.getElementById('noise_mix_db');
  const nmv = document.getElementById('val-noise-mix');
  if (nm && nmv) nmv.textContent = parseFloat(nm.value).toFixed(1) + ' dB';
}

function onParamChange() {
  syncDOMToState();
  updateReadouts();
  updateMeta();
  if (originalAudioEl && state.currentSample) {
    originalAudioEl.src = '/orig/' + encodeURIComponent(state.currentSample);
  }
  const auto = document.getElementById('auto-render');
  if (!auto || !auto.checked) {
    return;
  }
  if (debounceTimer) clearTimeout(debounceTimer);
  debounceTimer = setTimeout(() => {
    debounceTimer = null;
    doRender();
  }, 300);
}

function onRender() {
  if (debounceTimer) {
    clearTimeout(debounceTimer);
    debounceTimer = null;
  }
  doRender();
}

function synthPlay(url) {
  const synth = document.getElementById('synth');
  const dl = document.getElementById('download-link');
  if (synth) {
    synth.src = url;
    synth.play().catch(() => {});
  }
  if (dl) {
    dl.href = url;
    dl.download = (state.currentSample || 'render') + '_render.wav';
    dl.style.display = 'inline';
  }
}

async function doRender() {
  const btn = document.getElementById('render-btn');
  const sample_id = state.currentSample;
  if (!sample_id) {
    setStatus('Select a sample', 'err');
    return;
  }
  btn.disabled = true;
  const origLabel = btn.textContent;
  btn.textContent = 'Rendering…';
  setStatus('Sending POST /render...');
  const payload = {
    sample_id: sample_id,
    gain_curve: state.gain_curve,
    thresh_db: state.thresh_db,
    tilt_db: state.tilt_db,
    spectral_tilt_db: state.tilt_db,
    max_voices: state.max_voices,
    master_gain: state.master_gain,
    noise_mix_db: state.noise_mix_db,
    noise_floor_db: state.noise_floor_db,
    per_harmonic_gains: state.per_harmonic_gains,
    wave_shapes: state.wave_shapes,
    include_spec: true
  };
  try {
    const resp = await fetch('/render?include_spec=true', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload)
    });
    if (resp.ok) {
      const ct = resp.headers.get('content-type') || '';
      if (ct.includes('application/json')) {
        // include_spec response: {wav_b64, spec_b64, sample_rate, duration_s}
        const j = await resp.json();
        const wavBytes = Uint8Array.from(atob(j.wav_b64), c => c.charCodeAt(0));
        const blob = new Blob([wavBytes], {type: 'audio/wav'});
        if (lastBlobUrl) { try { URL.revokeObjectURL(lastBlobUrl); } catch(_){} }
        lastBlobUrl = URL.createObjectURL(blob);
        synthPlay(lastBlobUrl);
        // Show spectrograms
        if (j.spec_b64) {
          const sc = document.getElementById('spec-container');
          const so = document.getElementById('spec-orig');
          const ss = document.getElementById('spec-synth');
          if (sc && ss) {
            sc.style.display = 'block';
            ss.src = 'data:image/png;base64,' + j.spec_b64;
            so.style.display = 'none'; // single spec image has both
          }
        }
        setStatus('Render complete (' + (j.duration_s||0).toFixed(1) + 's). Ready.', 'ok');
      } else {
        // legacy blob response
        const blob = await resp.blob();
        if (lastBlobUrl) { try { URL.revokeObjectURL(lastBlobUrl); } catch(_){} }
        lastBlobUrl = URL.createObjectURL(blob);
        synthPlay(lastBlobUrl);
        setStatus('Render complete (200). Ready to play.', 'ok');
      }
    } else {
      let errMsg = 'HTTP ' + resp.status;
      try {
        const j = await resp.json();
        errMsg = j.error || j.detail || errMsg;
      } catch(_) {
        try { errMsg += ' ' + (await resp.text()).slice(0,120); } catch(e){}
      }
      setStatus('Render failed: ' + errMsg, 'err');
    }
  } catch(e) {
    setStatus('Fetch error: ' + e.message, 'err');
  } finally {
    btn.disabled = false;
    btn.textContent = origLabel;
  }
}

function updateMeta() {
  const id = state.currentSample || (document.getElementById('sample') ? document.getElementById('sample').value : '');
  const m = document.getElementById('meta');
  if (m) m.textContent = id ? id : '';
}

async function loadSamples() {
  const sel = document.getElementById('sample');
  try {
    const r = await fetch('/samples');
    if (!r.ok) throw new Error(r.status);
    const j = await r.json();
    const list = j.samples || [];
    samplesList = list;
    sel.innerHTML = '';
    if (!list.length) {
      sel.innerHTML = '<option value="">(no samples)</option>';
      setStatus('No samples discovered', 'err');
      return;
    }
    list.forEach(s => {
      const o = document.createElement('option');
      o.value = s.id;
      o.textContent = s.label + (s.duration_s != null ? ' ('+s.duration_s+'s)' : '');
      sel.appendChild(o);
    });
    if (state.currentSample && Array.from(sel.options).some(o => o.value === state.currentSample)) {
      sel.value = state.currentSample;
    } else {
      sel.selectedIndex = 0;
      state.currentSample = sel.value;
    }
    // Track F0 for frequency display
    const si = samplesList.find(s => s.id === state.currentSample);
    state.f0_mean = (si && si.f0_mean) ? si.f0_mean : 0;
    updateMeta();
    loadOriginalForCurrent();
    setStatus('Loaded ' + list.length + ' samples');
  } catch(e) {
    sel.innerHTML = '<option>(load failed)</option>';
    setStatus('Failed to fetch /samples: ' + e, 'err');
  }
}

function loadOriginalForCurrent() {
  if (!originalAudioEl) originalAudioEl = document.getElementById('original');
  const id = state.currentSample || (document.getElementById('sample') ? document.getElementById('sample').value : '');
  if (originalAudioEl && id) {
    originalAudioEl.src = '/orig/' + encodeURIComponent(id);
  }
}

function bindControls() {
  const changeIds = ['sample', 'gain_curve', 'noise_bypass'];
  changeIds.forEach(id => {
    const el = document.getElementById(id);
    if (el) el.addEventListener('change', onParamChange);
  });
  const inputIds = ['thresh_db', 'tilt_db', 'max_voices', 'master_gain', 'noise_mix_db', 'noise_floor_db'];
  inputIds.forEach(id => {
    const el = document.getElementById(id);
    if (el) el.addEventListener('input', onParamChange);
  });
  // Harmonic strip listeners are wired inside makeHarmonics();
  // the max_voices onchange handler rebuilds strips + listeners.
  const mv2 = document.getElementById('max_voices');
  if (mv2) mv2.addEventListener('change', () => {
    syncDOMToState();
    makeHarmonics();
    syncStateToDOM();
    updateReadouts();
  });
}

const PRESET_PREFIX = 'voice_shaper_preset:';
const LAST_KEY = 'voice_shaper_last_preset';

function savePreset() {
  syncDOMToState();
  const name = prompt('Preset name?');
  if (!name || !name.trim()) return;
  const key = PRESET_PREFIX + name.trim();
  try {
    localStorage.setItem(key, JSON.stringify(state));
    localStorage.setItem(LAST_KEY, name.trim());
    setStatus('Preset "' + name.trim() + '" saved', 'ok');
  } catch(e) {
    setStatus('Save failed: ' + e, 'err');
  }
}

function loadPreset() {
  const keys = [];
  for (let i = 0; i < localStorage.length; i++) {
    const k = localStorage.key(i);
    if (k && k.startsWith(PRESET_PREFIX)) keys.push(k);
  }
  if (!keys.length) {
    setStatus('No saved presets', 'err');
    return;
  }
  const names = keys.map(k => k.slice(PRESET_PREFIX.length));
  const choice = prompt('Load preset name:' + String.fromCharCode(10) + names.join(String.fromCharCode(10)));
  if (!choice) return;
  const key = PRESET_PREFIX + choice.trim();
  const raw = localStorage.getItem(key);
  if (!raw) {
    setStatus('Preset not found', 'err');
    return;
  }
  try {
    const loaded = JSON.parse(raw);
    state = {
      ...JSON.parse(JSON.stringify(DEFAULT_STATE)),
      ...loaded
    };
    if (!state.currentSample) state.currentSample = '';
    syncStateToDOM();
    loadOriginalForCurrent();
    setStatus('Preset "' + choice.trim() + '" loaded', 'ok');
    localStorage.setItem(LAST_KEY, choice.trim());
  } catch(e) {
    setStatus('Load failed: ' + e, 'err');
  }
}

function resetToDefaults() {
  state = JSON.parse(JSON.stringify(DEFAULT_STATE));
  syncStateToDOM();
  const ar = document.getElementById('auto-render');
  if (ar) ar.checked = false;
  loadOriginalForCurrent();
  setStatus('Defaults restored');
}

function autoLoadLastPreset() {
  try {
    const lastName = localStorage.getItem(LAST_KEY);
    if (!lastName) return false;
    const key = PRESET_PREFIX + lastName;
    const raw = localStorage.getItem(key);
    if (!raw) return false;
    const loaded = JSON.parse(raw);
    state = {
      ...JSON.parse(JSON.stringify(DEFAULT_STATE)),
      ...loaded
    };
    return true;
  } catch(e) { return false; }
}

async function doDownload() {
  const selId = state.currentSample || (document.getElementById('sample') ? document.getElementById('sample').value : '') || 'voice';
  if (lastBlobUrl) {
    const a = document.createElement('a');
    a.href = lastBlobUrl;
    a.download = selId + '_shaped.wav';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    return;
  }
  const payload = {
    sample_id: selId,
    gain_curve: state.gain_curve,
    thresh_db: state.thresh_db,
    tilt_db: state.tilt_db,
    spectral_tilt_db: state.tilt_db,
    max_voices: state.max_voices,
    master_gain: state.master_gain,
    noise_mix_db: state.noise_mix_db,
    noise_floor_db: state.noise_floor_db,
    per_harmonic_gains: state.per_harmonic_gains,
    wave_shapes: state.wave_shapes
  };
  setStatus('Re-rendering for download...');
  try {
    const resp = await fetch('/render', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload)
    });
    if (!resp.ok) throw new Error('render ' + resp.status);
    const blob = await resp.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = selId + '_shaped.wav';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setStatus('Downloaded ' + selId + '_shaped.wav', 'ok');
  } catch(e) {
    setStatus('Download render failed: ' + e.message, 'err');
  }
}

function initPlayback() {
  originalAudioEl = document.getElementById('original');
  synthAudioEl = document.getElementById('synth');

  document.getElementById('play-orig').addEventListener('click', () => {
    if (originalAudioEl && originalAudioEl.src) {
      originalAudioEl.play().catch(() => setStatus('Play original failed'));
    } else {
      setStatus('No original loaded');
    }
  });
  document.getElementById('play-synth').addEventListener('click', () => {
    if (synthAudioEl && synthAudioEl.src) {
      synthAudioEl.play().catch(() => setStatus('Play synth failed'));
    } else {
      setStatus('Render first for synth playback');
    }
  });
  document.getElementById('play-both').addEventListener('click', async () => {
    if (!originalAudioEl || !synthAudioEl) return;
    originalAudioEl.pause();
    synthAudioEl.pause();
    originalAudioEl.currentTime = 0;
    synthAudioEl.currentTime = 0;
    try {
      await Promise.all([
        originalAudioEl.play().catch(()=>{}),
        synthAudioEl.play().catch(()=>{})
      ]);
    } catch(_) {}
  });
}

function init() {
  makeHarmonics();
  state = JSON.parse(JSON.stringify(DEFAULT_STATE));
  syncStateToDOM();
  const didLoad = autoLoadLastPreset();
  if (didLoad) {
    syncStateToDOM();
  }
  bindControls();
  initPlayback();
  document.getElementById('render-btn').addEventListener('click', onRender);
  document.getElementById('reset-btn').addEventListener('click', resetToDefaults);
  document.getElementById('save-btn').addEventListener('click', savePreset);
  const loadBtn = document.getElementById('load-btn');
  if (loadBtn) loadBtn.addEventListener('click', loadPreset);
  document.getElementById('download-btn').addEventListener('click', doDownload);
  loadSamples();
}

document.addEventListener('DOMContentLoaded', init);
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class VoiceShaperHandler(BaseHTTPRequestHandler):
    """Routing layer on top of BaseHTTPRequestHandler.

    Routing is dispatched in `do_*` based on (method, path). Path patterns:
        GET  /                 → inline HTML
        GET  /samples          → JSON list
        GET  /orig/<id>        → WAV bytes
        POST /render           → JSON body, returns 501 stub for now
        GET  /viz/<path>       → file under <voice_dir>/viz/
        GET  /health           → JSON health check (handy for scripting)
    """

    server_version = "VoiceShaper/0.1"

    # Per-request state set by the server before serve_forever starts.
    voice_dir: Path  # injected by the factory below
    viz_dir: Path    # injected by the factory below

    # ------------------------------------------------------------------ helpers

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        # Route stdlib access logs through our logger so the format is
        # consistent with the rest of the project.
        log.info("%s - %s", self.address_string(), format % args)

    def _send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, status: int, body: bytes, content_type: str,
                    extra_headers: Optional[dict[str, str]] = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> tuple[Optional[dict], Optional[str]]:
        """Return (parsed_dict, error_message). error_message is non-None on failure."""
        length = self.headers.get("Content-Length")
        if length is None:
            return None, "missing Content-Length header"
        try:
            n = int(length)
        except ValueError:
            return None, f"invalid Content-Length: {length!r}"
        if n < 0 or n > 4 * 1024 * 1024:  # 4 MiB hard cap on render bodies
            return None, f"Content-Length out of range: {n}"
        raw = self.rfile.read(n)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return None, f"invalid JSON: {exc}"
        if not isinstance(data, dict):
            return None, "JSON body must be an object"
        return data, None

    # --------------------------------------------------------------- GET routes

    def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler API)
        path = self.path
        started = time.monotonic()

        try:
            if path == "/" or path == "/index.html":
                self._send_bytes(HTTPStatus.OK, PLACEHOLDER_HTML.encode("utf-8"),
                                 "text/html; charset=utf-8",
                                 extra_headers={"Cache-Control": "no-store"})
            elif path == "/samples":
                samples = _SAMPLES_CACHE if _SAMPLES_CACHE is not None else discover_samples(self.voice_dir)
                self._send_json(HTTPStatus.OK, {
                    "count": len(samples),
                    "voice_dir": str(self.voice_dir),
                    "samples": samples,
                })
            elif path.startswith("/orig/"):
                sample_id = path[len("/orig/"):].split("?", 1)[0]
                self._handle_get_orig(sample_id)
            elif path.startswith("/viz/"):
                rel = path[len("/viz/"):].split("?", 1)[0]
                self._handle_get_viz(rel)
            elif path == "/health":
                self._send_json(HTTPStatus.OK, {
                    "ok": True,
                    "voice_dir": str(self.voice_dir),
                    "viz_dir": str(self.viz_dir),
                })
            else:
                self._send_json(HTTPStatus.NOT_FOUND, {
                    "error": "not_found",
                    "method": "GET",
                    "path": path,
                })
        finally:
            log.info("GET %s -> %.0fms", path, (time.monotonic() - started) * 1000.0)

    # --------------------------------------------------------------- POST routes

    def do_POST(self) -> None:  # noqa: N802
        full_path = self.path
        try:
            path = urlparse(full_path).path
        except Exception:
            path = full_path
        started = time.monotonic()
        try:
            if path == "/render":
                self._handle_post_render()
            elif path == "/samples/refresh":
                self._handle_post_samples_refresh()
            else:
                self._send_json(HTTPStatus.NOT_FOUND, {
                    "error": "not_found",
                    "method": "POST",
                    "path": path,
                })
        finally:
            log.info("POST %s -> %.0fms", path, (time.monotonic() - started) * 1000.0)

    # ---------------------------------------------------------------- handlers

    def _handle_get_orig(self, sample_id: str) -> None:
        cache = _SAMPLES_CACHE or []
        sample = next((s for s in cache if s.get("id") == sample_id), None)
        if sample is None:
            self._send_json(HTTPStatus.NOT_FOUND, {
                "error": "sample not found",
            })
            return
        wav_path = Path(sample.get("path") or "")
        try:
            size = wav_path.stat().st_size
        except OSError as exc:
            log.error("read failed for %s: %s", wav_path, exc)
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {
                "error": "read_failed",
                "sample_id": sample_id,
            })
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            with wav_path.open("rb") as f:
                while True:
                    chunk = f.read(64 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except OSError as exc:
            log.error("stream failed for %s: %s", wav_path, exc)
            # headers already sent; no JSON 500 possible

    def _handle_get_viz(self, rel: str) -> None:
        # Strict allow-list: alnum + dot + dash + underscore + slash for
        # subpaths. No leading `/`, no backslashes. Traversal caught by resolve guard.
        if not rel or rel.startswith("/"):
            self._send_json(HTTPStatus.BAD_REQUEST, {
                "error": "invalid_viz_path",
                "path": rel,
            })
            return
        if not re.fullmatch(r"[A-Za-z0-9._/-]+", rel):
            self._send_json(HTTPStatus.BAD_REQUEST, {
                "error": "invalid_viz_path",
                "path": rel,
            })
            return
        target = (self.viz_dir / rel).resolve()
        viz_root = self.viz_dir.resolve()
        try:
            target.relative_to(viz_root)  # raises ValueError if escape
        except ValueError:
            self._send_json(HTTPStatus.FORBIDDEN, {
                "error": "viz_path_escape",
                "path": rel,
            })
            return
        if not target.is_file():
            self._send_json(HTTPStatus.NOT_FOUND, {
                "error": "viz_not_found",
                "path": rel,
            })
            return
        ctype, _ = mimetypes.guess_type(target.name)
        try:
            data = target.read_bytes()
        except OSError as exc:
            log.error("read failed for %s: %s", target, exc)
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {
                "error": "read_failed",
                "path": rel,
            })
            return
        self._send_bytes(HTTPStatus.OK, data, ctype or "application/octet-stream",
                         extra_headers={"Cache-Control": "public, max-age=60"})

    def _handle_post_render(self) -> None:
        """Wired to synth_pure + VoiceCache.

        Parses/validates JSON, loads sample WAV (via sf), prepares analysis
        (with VoiceCache), converts params, calls synthesize_prepared,
        post-processes and returns 16-bit stereo WAV bytes at SAMPLE_RATE.
        """
        body, err = self._read_json_body()
        if err is not None or body is None:
            self._send_json(HTTPStatus.BAD_REQUEST, {
                "error": "bad_request",
                "detail": err or "empty body",
            })
            return

        # check self.path for include_spec=true (parse with urllib.parse)
        include_spec = False
        try:
            parsed = urlparse(self.path)
            qs = parse_qs(parsed.query or "")
            val = (qs.get("include_spec") or [""])[0].lower()
            include_spec = val in ("true", "1", "yes")
        except Exception:
            include_spec = False

        sample_id = body.get("sample_id")
        if not isinstance(sample_id, str) or not SAFE_ID_RE.match(sample_id):
            self._send_json(HTTPStatus.BAD_REQUEST, {
                "error": "missing_sample_id",
                "detail": "sample_id (matching SAFE_ID_RE) is required",
            })
            return

        wav_path = self.voice_dir / f"{sample_id}.wav"
        if not wav_path.is_file():
            self._send_json(HTTPStatus.NOT_FOUND, {
                "error": "sample_not_found",
                "sample_id": sample_id,
            })
            return

        req_start = time.monotonic()

        # Wrap the synth_pure / VoiceCache imports (plus sf/np) so a missing
        # optional dep falls back cleanly to 501 stub behavior.
        try:
            import numpy as np
            import soundfile as sf
            sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
            from tools.synth_pure import prepare_analysis, synthesize_prepared, SAMPLE_RATE
            from tools.voice_cache import VoiceCache
        except Exception as imp_exc:
            log.warning("synth_pure or voice_cache imports failed: %s", imp_exc)
            self._send_json(HTTPStatus.NOT_IMPLEMENTED, {
                "error": "not_implemented",
                "detail": "synth_pure module not available",
            })
            return

        # LOAD AUDIO — detect the loud channel (R24 capture is stereo with
        # voice on Ch2; Ch1 is monitor bleed at -90dB). Mirror
        # build_voice_compare_v3.pick_mono_channel so the server agrees with
        # the dashboard builder on which channel has the voice signal.
        try:
            y, sr = sf.read(str(wav_path), always_2d=False)
            y = pick_loud_channel(y)
            y = y.astype(np.float32)
        except Exception as load_exc:
            log.error("failed to load %s: %s", wav_path, load_exc)
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {
                "error": "load_failed",
                "detail": str(load_exc),
            })
            return

        # PREPARE WITH CACHING (lazy module singleton)
        global _voice_cache
        if _voice_cache is None:
            _voice_cache = VoiceCache()
        anal_start = time.monotonic()
        prepared = _voice_cache.get(wav_path)
        if prepared is None:
            prepared = prepare_analysis(y, sr)
            _voice_cache.store(wav_path, prepared)
            anal_ms = (time.monotonic() - anal_start) * 1000.0
            cached = False
        else:
            anal_ms = 0.0
            cached = True

        # CONVERT params: lists -> 1-based dicts for the synthesize API
        try:
            gain_curve = str(body.get("gain_curve", "sqrt"))
            spectral_tilt_db = float(body.get("spectral_tilt_db", 0.0))
            thresh_db = float(body.get("thresh_db", -40.0))
            noise_floor_db = float(body.get("noise_floor_db", -60.0))
            max_voices = int(body.get("max_voices", 32))
            if not (1 <= max_voices <= 64):
                raise ValueError("max_voices must be in range 1..64")
            gh_list = body.get("per_harmonic_gains", [1.0] * max_voices)
            if not isinstance(gh_list, (list, tuple)) or len(gh_list) < 1 or len(gh_list) > 32:
                raise ValueError("per_harmonic_gains must be list of 1..32 floats")
            per_harmonic_gains = {i + 1: float(gh_list[i]) for i in range(len(gh_list))}
            ws_list = body.get("wave_shapes", ["sine"] * max_voices)
            if not isinstance(ws_list, (list, tuple)) or len(ws_list) < 1 or len(ws_list) > 32:
                raise ValueError("wave_shapes must be list of 1..32 strings")
            valid_shapes = {"sine", "square", "saw", "triangle"}
            for s in ws_list:
                if not isinstance(s, str) or s not in valid_shapes:
                    raise ValueError(f"invalid wave shape: {s}")
            wave_shapes = {i + 1: s for i, s in enumerate(ws_list)}
            noise_mix_db = float(body.get("noise_mix_db", -12.0))
            if not (-120.0 <= noise_mix_db <= 0.0):
                raise ValueError("noise_mix_db must be in range -120..0")
            master_gain = float(body.get("master_gain", 0.7))
            if not (0.0 <= master_gain <= 2.0):
                raise ValueError("master_gain must be in 0.0..2.0")
        except Exception as param_exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {
                "error": "invalid_parameters",
                "detail": str(param_exc),
            })
            return

        # RUN SYNTH
        synth_start = time.monotonic()
        try:
            out = synthesize_prepared(
                prepared,
                thresh_db=thresh_db,
                noise_floor_db=noise_floor_db,
                max_voices=max_voices,
                gain_curve=gain_curve,
                spectral_tilt_db=spectral_tilt_db,
                per_harmonic_gains=per_harmonic_gains,
                wave_shapes=wave_shapes,
                noise_mix_db=noise_mix_db,
            )
        except Exception as synth_exc:
            log.error("synthesize_prepared failed: %s", synth_exc)
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {
                "error": "synthesis_failed",
                "detail": str(synth_exc),
            })
            return
        synth_ms = (time.monotonic() - synth_start) * 1000.0

        # Apply master gain BEFORE peak normalization
        out = out * master_gain

        # TO WAV BYTES: softclip or norm, int16, duplicate to stereo interleaved
        try:
            peak = float(np.abs(out).max()) if len(out) else 0.0
            if peak > 0.95:
                out = np.tanh(out)
            elif peak > 0.0:
                out = out * (0.95 / peak)
            synth_y = np.asarray(out, dtype=np.float64).copy()
            duration_s = round(len(synth_y) / float(SAMPLE_RATE), 3) if SAMPLE_RATE > 0 else 0.0
            pcm = (np.clip(out, -1.0, 1.0) * 32767).astype(np.int16)
            pcm_stereo = np.column_stack([pcm, pcm]).reshape(-1)
            buf = io.BytesIO()
            with wave.open(buf, "wb") as w:
                w.setnchannels(2)
                w.setsampwidth(2)
                w.setframerate(SAMPLE_RATE)
                w.writeframes(pcm_stereo.tobytes())
            wav_bytes = buf.getvalue()
        except Exception as enc_exc:
            log.error("wav encoding failed: %s", enc_exc)
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {
                "error": "encode_failed",
                "detail": str(enc_exc),
            })
            return

        total_ms = (time.monotonic() - req_start) * 1000.0
        log.info(
            "POST /render id=%s total=%.1fms analysis%s=%.1fms synth=%.1fms peak=%.3f",
            sample_id, total_ms, "(cached)" if cached else "", anal_ms, synth_ms, peak
        )

        if include_spec:
            spec_b64 = ""
            spec_ms = 0.0
            try:
                spec_start = time.monotonic()
                import base64
                import matplotlib
                matplotlib.use('Agg')
                import matplotlib.pyplot as plt
                import librosa
                import librosa.display
                # orig from loaded y (float), synth from float64 out (pre-int16)
                y_spec = np.asarray(y, dtype=np.float64)
                fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 8), facecolor='#0e1116')
                for ax, yy, title, srate in [
                    (ax1, y_spec, "Original", sr),
                    (ax2, synth_y, "Synth", SAMPLE_RATE),
                ]:
                    ax.set_facecolor('#000')
                    S = np.abs(librosa.stft(yy, n_fft=2048, hop_length=512))
                    S_db = librosa.amplitude_to_db(S, ref=np.max(S))
                    img = librosa.display.specshow(
                        S_db, sr=srate, hop_length=512, x_axis='time', y_axis='hz',
                        cmap='magma', ax=ax, fmax=4000
                    )
                    fig.colorbar(img, ax=ax, format='%+2.0f dB', label='dB')
                    ax.set_title(title, color='#c9d1d9', fontsize=12)
                buf = io.BytesIO()
                fig.savefig(buf, format='png', dpi=100, bbox_inches='tight', facecolor='#0e1116')
                plt.close(fig)
                buf.seek(0)
                spec_b64 = base64.b64encode(buf.read()).decode('ascii')
                spec_ms = (time.monotonic() - spec_start) * 1000.0
                log.info("POST /render spec generation: %.1fms (include_spec=true)", spec_ms)
            except Exception as spec_exc:
                log.warning("include_spec spectrogram generation failed: %s", spec_exc)
            wav_b64 = base64.b64encode(wav_bytes).decode('ascii')
            self._send_json(HTTPStatus.OK, {
                "wav_b64": wav_b64,
                "spec_b64": spec_b64,
                "peak": round(peak, 4),
                "duration_s": duration_s,
            })
            return

        self._send_bytes(HTTPStatus.OK, wav_bytes, "audio/wav",
                         extra_headers={"Cache-Control": "no-store"})

    def _handle_post_samples_refresh(self) -> None:
        """Force a cache refresh by re-running discovery.

        Returns the fresh count and voice_dir. The refresh is logged by
        refresh_samples_cache().
        """
        count = refresh_samples_cache(self.voice_dir)
        self._send_json(HTTPStatus.OK, {
            "ok": True,
            "count": count,
            "voice_dir": str(self.voice_dir),
        })


# ---------------------------------------------------------------------------
# Server lifecycle
# ---------------------------------------------------------------------------

def make_handler(voice_dir: Path, viz_dir: Path):
    """Closure factory that injects paths into the handler class.

    BaseHTTPRequestHandler instances are created by the HTTPServer per
    request; class attributes are the simplest way to share config without
    globals.
    """

    class _Bound(VoiceShaperHandler):
        pass

    _Bound.voice_dir = voice_dir
    _Bound.viz_dir = viz_dir
    return _Bound


def install_signal_handlers(server: ThreadingHTTPServer) -> None:
    """Wire SIGINT/SIGTERM to a graceful shutdown.

    HTTPServer.shutdown() must be called from a thread other than the one
    blocked in serve_forever(), so the signal handler schedules shutdown on
    a dedicated thread.
    """
    shutdown_started = threading.Event()

    def _trigger_shutdown(signum, _frame):
        if shutdown_started.is_set():
            log.warning("second signal %d received; ignoring", signum)
            return
        shutdown_started.set()
        log.info("received signal %d, shutting down...", signum)
        # server.shutdown() returns immediately and unblocks serve_forever()
        threading.Thread(target=server.shutdown, name="vs-shutdown", daemon=True).start()

    signal.signal(signal.SIGINT, _trigger_shutdown)
    signal.signal(signal.SIGTERM, _trigger_shutdown)


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Voice → Shaper HTTP server (interactive mixer skeleton).",
    )
    p.add_argument("--host", default=DEFAULT_HOST,
                   help=f"bind host (default: {DEFAULT_HOST})")
    p.add_argument("--port", type=int, default=DEFAULT_PORT,
                   help=f"bind port (default: {DEFAULT_PORT})")
    p.add_argument("--voice-dir", type=Path, default=DEFAULT_VOICE_DIR,
                   help=f"voice samples directory (default: {DEFAULT_VOICE_DIR})")
    p.add_argument("--log-level", default="INFO",
                   choices=("DEBUG", "INFO", "WARNING", "ERROR"),
                   help="logging level (default: INFO)")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )

    voice_dir: Path = args.voice_dir.expanduser().resolve()
    viz_dir: Path = (voice_dir / "viz").resolve()

    if not voice_dir.is_dir():
        log.error("voice dir does not exist: %s", voice_dir)
        return 2

    refresh_samples_cache(voice_dir)

    handler_cls = make_handler(voice_dir, viz_dir)
    # ThreadingHTTPServer so /render (when wired) does not block /samples etc.
    server = ThreadingHTTPServer((args.host, args.port), handler_cls)
    server.daemon_threads = True

    install_signal_handlers(server)

    log.info("voice_shaper_server listening on http://%s:%d", args.host, args.port)
    log.info("  voice_dir: %s", voice_dir)
    log.info("  viz_dir:   %s", viz_dir)
    log.info("Ctrl-C to stop.")

    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        log.info("server closed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
