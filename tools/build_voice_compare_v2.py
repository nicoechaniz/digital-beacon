"""Build a static spectrogram comparison page for voice → shaper.

Generates high-quality PNG spectrograms with matplotlib + librosa.display.specshow
(the canonical pattern), saves them, and emits a small HTML page that loads them
side-by-side + overlaid. Audio playback uses native <audio> tags.
"""
import json
import base64
from pathlib import Path

import librosa
import librosa.display
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import soundfile as sf

OUT_DIR = Path("/home/nicolas/Projects/digital-beacon/tools/viz")
OUT_DIR.mkdir(parents=True, exist_ok=True)
HTML_PATH = OUT_DIR / "index.html"

ORIG_PATH = Path("/home/nicolas/Music/voice-analysis/nico_voz_v2_mono.wav")
SYNTH_PATH = Path("/home/nicolas/Music/voice-analysis/nico_voz_v2_synth.wav")

# ─── Load audio ────────────────────────────────────────────────────────────
y_orig, sr_o = sf.read(str(ORIG_PATH))
y_synth, sr_s = sf.read(str(SYNTH_PATH))
y_orig = np.asarray(y_orig, dtype=np.float32)
y_synth = np.asarray(y_synth, dtype=np.float32)

# Ensure both are (N, 1) mono (the original WAV is mono, the synth WAV is
# stereo because it was written as [L,R,L,R,...] — collapse to channel 0
# for analysis purposes).
if y_orig.ndim == 2:
    y_orig = y_orig[:, 0]
if y_synth.ndim == 2:
    y_synth = y_synth[:, 0]

if sr_s != sr_o:
    y_synth = librosa.resample(y_synth, orig_sr=sr_s, target_sr=sr_o)
    sr_s = sr_o

duration = len(y_orig) / sr_o
print(f"orig: {len(y_orig)/sr_o:.3f}s, synth: {len(y_synth)/sr_s:.3f}s @ {sr_o} Hz")

# ─── F0 analysis (pYIN) ────────────────────────────────────────────────────
hop_pyin = int(0.0464 * sr_o)   # 46.4 ms → ~22 fps
f0, voiced, _ = librosa.pyin(
    y_orig, fmin=70, fmax=400, sr=sr_o,
    hop_length=hop_pyin, frame_length=4096, fill_na=0.0,
)
f0_times = librosa.frames_to_time(np.arange(len(f0)), sr=sr_o, hop_length=hop_pyin)
print(f"pYIN: {len(f0)} frames, voiced={sum(voiced)}/{len(voiced)}")

# ─── Spectrogram computation ───────────────────────────────────────────────
# Standard STFT settings for voice: n_fft=2048, hop=512 at 48kHz.
# This gives ~23 Hz frequency resolution (48000/2048) and ~10.7ms time resolution.
n_fft = 2048
hop = 512

S_orig = np.abs(librosa.stft(y_orig, n_fft=n_fft, hop_length=hop))
S_synth = np.abs(librosa.stft(y_synth, n_fft=n_fft, hop_length=hop))

S_orig_db = librosa.amplitude_to_db(S_orig, ref=np.max(S_orig))
S_synth_db = librosa.amplitude_to_db(S_synth, ref=np.max(S_synth))
print(f"spec shapes: {S_orig_db.shape}")

# ─── Save PNG: side-by-side ────────────────────────────────────────────────
def plot_spec(ax, S_db, sr, hop, title, cmap):
    img = librosa.display.specshow(
        S_db,
        sr=sr,
        hop_length=hop,
        x_axis="time",
        y_axis="hz",
        cmap=cmap,
        ax=ax,
    )
    ax.set_ylim(0, 800)   # Zoom to relevant band for adult voice
    ax.set_title(title, color="white", fontsize=12)
    ax.set_ylabel("Hz", color="#aaa")
    ax.tick_params(colors="#888", labelsize=9)
    ax.set_xlabel("Time (s)", color="#aaa")
    return img

# 1) Side-by-side (orig vs synth)
fig, axes = plt.subplots(2, 1, figsize=(20, 10), sharex=True,
                          facecolor="#0e1116")
img_o = plot_spec(axes[0], S_orig_db, sr_o, hop, "Original voice", "magma")
img_s = plot_spec(axes[1], S_synth_db, sr_s, hop, "Shaper synthesis (additive sines)", "viridis")
for ax in axes:
    ax.set_facecolor("#000")
fig.colorbar(img_o, ax=axes[0], format="%+2.0f dB", label="dB")
fig.colorbar(img_s, ax=axes[1], format="%+2.0f dB", label="dB")
plt.tight_layout()
side_by_side = OUT_DIR / "side_by_side.png"
plt.savefig(side_by_side, dpi=200, facecolor=fig.get_facecolor())
plt.close()
print(f"saved {side_by_side}")

# 2) Overlay: synthesized with F0 contour
fig, ax = plt.subplots(figsize=(20, 6), facecolor="#0e1116")
img = librosa.display.specshow(
    S_synth_db,
    sr=sr_o, hop_length=hop,
    x_axis="time", y_axis="hz",
    cmap="magma", ax=ax,
)
ax.set_ylim(0, 800)
ax.set_facecolor("#000")

# Overlay F0 contour on top of the synthesized spectrogram
times_synth = librosa.frames_to_time(np.arange(S_synth.shape[1]),
                                     sr=sr_o, hop_length=hop)
f0_on_synth = np.interp(times_synth, f0_times, f0,
                        left=0.0, right=0.0)
voiced_on_synth = np.interp(times_synth, f0_times,
                            voiced.astype(float),
                            left=0.0, right=0.0) > 0.5
f0_plot = np.where(voiced_on_synth, f0_on_synth, np.nan)
ax.plot(times_synth, f0_plot, color="#00ffd0", lw=2.0,
        label="F0 (pYIN)")
ax.set_title("Shaper synthesis + F0 trajectory",
             color="white", fontsize=12)
ax.set_ylabel("Hz", color="#aaa")
ax.set_xlabel("Time (s)", color="#aaa")
ax.tick_params(colors="#888", labelsize=9)
ax.legend(loc="upper right", facecolor="#161b22", edgecolor="#444",
          labelcolor="white")
fig.colorbar(img, ax=ax, format="%+2.0f dB", label="dB")
plt.tight_layout()
overlay_png = OUT_DIR / "overlay_synth_f0.png"
plt.savefig(overlay_png, dpi=200, facecolor=fig.get_facecolor())
plt.close()
print(f"saved {overlay_png}")

# 3) Overlay: original with F0 contour
fig, ax = plt.subplots(figsize=(20, 6), facecolor="#0e1116")
img = librosa.display.specshow(
    S_orig_db,
    sr=sr_o, hop_length=hop,
    x_axis="time", y_axis="hz",
    cmap="magma", ax=ax,
)
ax.set_ylim(0, 800)
ax.set_facecolor("#000")
ax.plot(times_synth, f0_plot, color="#00ffd0", lw=2.0, label="F0 (pYIN)")
ax.set_title("Original voice + F0 trajectory",
             color="white", fontsize=12)
ax.set_ylabel("Hz", color="#aaa")
ax.set_xlabel("Time (s)", color="#aaa")
ax.tick_params(colors="#888", labelsize=9)
ax.legend(loc="upper right", facecolor="#161b22", edgecolor="#444",
          labelcolor="white")
fig.colorbar(img, ax=ax, format="%+2.0f dB", label="dB")
plt.tight_layout()
overlay_orig_png = OUT_DIR / "overlay_orig_f0.png"
plt.savefig(overlay_orig_png, dpi=200, facecolor=fig.get_facecolor())
plt.close()
print(f"saved {overlay_orig_png}")

# ─── Encode WAVs for native <audio> playback ───────────────────────────────
def encode_wav_b64(y, sr):
    pcm = (np.clip(y, -1, 1) * 32767).astype(np.int16).tobytes()
    # Build a minimal WAV header around the PCM (44 bytes)
    import io, wave
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm)
    return base64.b64encode(buf.getvalue()).decode()


orig_wav_b64 = encode_wav_b64(y_orig, sr_o)
synth_wav_b64 = encode_wav_b64(y_synth, sr_s)

# ─── Build HTML ────────────────────────────────────────────────────────────
f0_min, f0_max = float(f0[voiced].min()), float(f0[voiced].max())
f0_mean = float(f0[voiced].mean())

html = f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<title>Voice → Shaper comparison</title>
<style>
  body {{
    margin: 0; padding: 24px;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
    background: #0e1116; color: #c9d1d9;
  }}
  h1 {{ margin: 0 0 8px; font-size: 20px; }}
  h2 {{ margin: 24px 0 8px; font-size: 13px;
        color: #8b949e; text-transform: uppercase; letter-spacing: 0.05em; }}
  .sub {{ color: #8b949e; font-size: 13px; margin-bottom: 16px; max-width: 900px; }}
  .panel {{ background: #161b22; border: 1px solid #30363d;
            border-radius: 8px; padding: 16px; margin-bottom: 16px; }}
  img {{ display: block; max-width: 100%; height: auto;
         border-radius: 4px; }}
  audio {{ width: 100%; margin-top: 8px; }}
  .audio-row {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px;
                 margin-top: 8px; }}
  .audio-label {{ font-size: 12px; color: #58a6ff; margin-bottom: 4px;
                  font-weight: 600; }}
  .play-both, .stop-both {{
    background: #1f6feb; color: white; border: none; padding: 10px 18px;
    border-radius: 6px; cursor: pointer; font-size: 13px; font-weight: 600;
    margin-right: 8px; margin-top: 12px;
  }}
  .play-both:hover, .stop-both:hover {{ background: #388bfd; }}
  .stop-both {{ background: #21262d; color: #c9d1d9; }}
  .stop-both:hover {{ background: #30363d; }}
  .legend {{ color: #8b949e; font-size: 12px; margin-top: 4px; }}
  .stats {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; }}
  .stat {{ background: #0e1116; padding: 12px; border-radius: 6px;
           border: 1px solid #21262d; }}
  .stat .v {{ font-size: 22px; font-weight: 600; color: #58a6ff; }}
  .stat .l {{ font-size: 11px; color: #8b949e;
              text-transform: uppercase; letter-spacing: 0.05em; }}
</style>
</head>
<body>
  <h1>Voice → Shaper comparison</h1>
  <div class="sub">
    Original voice recording vs additive synthesis through the Harmonic
    Beacon Shaper (32 sine voices tuned to f<sub>1</sub>·N, F0 estimated by
    pYIN, per-harmonic gain from STFT magnitude at f<sub>1</sub>·N). Both
    audio files normalized to peak −3 dBFS. Spectrograms use STFT n_fft=2048,
    hop=512, zoomed to 0-800 Hz (the relevant range for adult voice F0 and
    harmonics).
  </div>

  <div class="panel">
    <div class="stats">
      <div class="stat">
        <div class="l">Voiced frames</div>
        <div class="v">{int(voiced.sum())} / {len(voiced)}</div>
      </div>
      <div class="stat">
        <div class="l">F0 mean</div>
        <div class="v">{f0_mean:.1f} Hz</div>
      </div>
      <div class="stat">
        <div class="l">F0 range</div>
        <div class="v">{f0_min:.0f}–{f0_max:.0f} Hz</div>
      </div>
      <div class="stat">
        <div class="l">Duration</div>
        <div class="v">{duration:.2f} s</div>
      </div>
    </div>
  </div>

  <h2>Side by side</h2>
  <div class="panel">
    <img src="side_by_side.png" alt="Side by side spectrograms">
  </div>

  <div class="panel">
    <h2 style="margin-top:0">Listen</h2>
    <div class="legend">Both audio files normalized to peak −3 dBFS. RMS ratio
    (synth/orig) ≈ 0.9 — perceptually balanced. Click play on each player
    independently or start both at the same time to A/B compare.</div>
    <div class="audio-row">
      <div>
        <div class="audio-label">Original</div>
        <audio id="orig-audio" controls preload="metadata" src="data:audio/wav;base64,{orig_wav_b64}"></audio>
      </div>
      <div>
        <div class="audio-label">Shaper synthesis</div>
        <audio id="synth-audio" controls preload="metadata" src="data:audio/wav;base64,{synth_wav_b64}"></audio>
      </div>
    </div>
    <button id="play-both" class="play-both">▶ Play both synced</button>
    <button id="stop-both" class="stop-both">⏹ Stop both</button>
  </div>

  <h2>Synthesized + F0 trajectory</h2>
  <div class="panel">
    <img src="overlay_synth_f0.png" alt="Synthesis with F0 overlay">
    <div class="legend">Cyan line: F0 trajectory estimated by pYIN
    (librosa.pyin, f0 ∈ [70, 400] Hz).</div>
  </div>

  <h2>Original + F0 trajectory</h2>
  <div class="panel">
    <img src="overlay_orig_f0.png" alt="Original with F0 overlay">
  </div>

  <script>
  document.getElementById('play-both').onclick = () => {{
    const o = document.getElementById('orig-audio');
    const s = document.getElementById('synth-audio');
    o.currentTime = 0; s.currentTime = 0;
    o.play(); s.play();
  }};
  document.getElementById('stop-both').onclick = () => {{
    document.getElementById('orig-audio').pause();
    document.getElementById('synth-audio').pause();
  }};
  </script>
</body>
</html>
"""

HTML_PATH.write_text(html)
print(f"wrote {HTML_PATH}")