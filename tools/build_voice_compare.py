"""Build a standalone HTML visualizer comparing original voice vs Shaper synthesis.

Writes the data payload as JSON next to the HTML. HTML references it via fetch().
"""
import base64
import json
from pathlib import Path

import librosa
import numpy as np
import soundfile as sf

OUT_HTML = Path("/home/nicolas/Projects/digital-beacon/tools/voice_compare.html")
JSON_PATH = Path("/home/nicolas/Projects/digital-beacon/tools/voice_compare_data.json")

ORIG_PATH = Path("/tmp/viz_orig.wav")
SYNTH_PATH = Path("/tmp/viz_synth.wav")

y_orig, sr_o = sf.read(str(ORIG_PATH))
y_synth, sr_s = sf.read(str(SYNTH_PATH))
y_orig = np.asarray(y_orig, dtype=np.float32).ravel()
y_synth = np.asarray(y_synth, dtype=np.float32).ravel()
print(f"orig: {len(y_orig)/sr_o:.3f}s @ {sr_o} Hz  peak={np.abs(y_orig).max():.3f}")
print(f"synth: {len(y_synth)/sr_s:.3f}s @ {sr_s} Hz  peak={np.abs(y_synth).max():.3f}")

# Match sample rate for spectrograms. Resample synth if needed.
if sr_s != sr_o:
    y_synth = librosa.resample(y_synth, orig_sr=sr_s, target_sr=sr_o)
    sr_s = sr_o

# Use a higher-resolution spec for the visualization (n_fft=8192 gives ~2.1 Hz
# bin width, enough to resolve harmonics at 140 Hz fundamental). We then
# crop to 0-400 Hz — the relevant range for adult speech F0 + first ~3
# harmonics at 140 Hz. With n_fft=8192 we get ~188 frequency bins in that
# range, which avoids the "stairstep" you get from a too-coarse FFT.
ZOOM_MAX_HZ = 400.0
N_FFT = 8192
HOP = 512

hop = HOP
n_fft = N_FFT
f0, voiced, _ = librosa.pyin(
    y_orig, fmin=70, fmax=400, sr=sr_o,
    hop_length=int(0.0464 * sr_o), frame_length=4096, fill_na=0.0,
)
times = librosa.frames_to_time(np.arange(len(f0)), sr=sr_o,
                               hop_length=int(0.0464 * sr_o))


def spec_db(y, sr):
    S = np.abs(librosa.stft(y, n_fft=n_fft, hop_length=hop))
    S_db = librosa.amplitude_to_db(S, ref=np.max(S))  # GLOBAL ref (not per-frame)
    return S_db.astype(np.float32)


S_orig = spec_db(y_orig, sr_o)
S_synth = spec_db(y_synth, sr_s)
freqs_full = librosa.fft_frequencies(sr=sr_o, n_fft=n_fft)

# Crop to 0..ZOOM_MAX_HZ
max_bin = int(np.searchsorted(freqs_full, ZOOM_MAX_HZ))
freqs = freqs_full[:max_bin + 1]
S_orig = S_orig[:max_bin + 1, :]
S_synth = S_synth[:max_bin + 1, :]
print(f"spec shapes: orig={S_orig.shape} synth={S_synth.shape}  (freq range 0..{ZOOM_MAX_HZ} Hz)")


def quantize(arr, lo=-80.0, hi=0.0):
    out = np.clip(arr, lo, hi)
    return ((out - lo) / (hi - lo) * 255).astype(np.uint8)


q_orig = quantize(S_orig)
q_synth = quantize(S_synth)


def encode_wav(y, sr):
    """Encode float32 mono array as base64 WAV (16-bit PCM)."""
    pcm = (np.clip(y, -1, 1) * 32767).astype(np.int16).tobytes()
    return base64.b64encode(pcm).decode()


payload = {
    "sr_orig": sr_o,
    "sr_synth": sr_s,
    "n_orig": len(y_orig),
    "n_synth": len(y_synth),
    "orig_wav_b64": encode_wav(y_orig, sr_o),
    "synth_wav_b64": encode_wav(y_synth, sr_s),
    "spec_orig_q": q_orig.flatten().tolist(),
    "spec_synth_q": q_synth.flatten().tolist(),
    "spec_freqs": freqs.tolist(),
    "spec_hop": hop,
    "spec_sr": sr_o,
    "spec_n_fft": n_fft,
    "f0_times": times.tolist(),
    "f0_values": [float(v) if voiced[i] else 0.0 for i, v in enumerate(f0)],
    "f0_voiced": [bool(v) for v in voiced],
}

JSON_PATH.write_text(json.dumps(payload))
print(f"Wrote {JSON_PATH} ({JSON_PATH.stat().st_size / 1e6:.1f} MB)")
print(f"HTML will be: {OUT_HTML}")