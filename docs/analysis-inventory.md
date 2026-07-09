# Inventory of existing sample analysis experiments

This document preserves the analysis work that has been done across the
digital-beacon / NaturalHarmony / beacon-spatial projects so it can be reused
when we design the dedicated HIT analysis tool.

## 1. nh-analysis package (`packages/nh-analysis/src/nh_analysis/`)

A reusable analysis library built during the nh-toolkit refactor. It is still
usable even though the UI v2 refactor is being set aside.

| Module | What it does | Key functions |
|---|---|---|
| `f0.py` | F0 estimation | `F0Estimator`, `LibrosaPyinEstimator` (pyin) |
| `harmonicity.py` | Harmonic energy scoring | `harmonicity_score(audio, sr, f1, n_harmonics)`, `harmonic_f1_search(audio, sr)`, `spectral_metrics(audio, sr)` |
| `mask.py` | Harmonic / residual separation | `harmonic_mask(audio, sr, f1, n_harmonics, bandwidth_hz)` returns `harmonic_audio`, `residual_audio`, `mask` |
| `phideus.py` | Phideus-style descriptors | `compute_h_series`, `compute_v4_linear`, `compute_v4_log`, `compute_a4_16k` |
| `result.py` | Structured result types | `AnalysisResult`, `F0Track`, `SpectralMetrics`, `PhideusDescriptors`, `EmotionResult`, `SpeakerResult` |
| `catalog.py` | Sample catalog | `SampleCatalog`, `SampleEntry` for managing WAV collections |

### Example usage

```python
from nh_analysis import harmonic_f1_search, harmonicity_score, harmonic_mask
import librosa

y, sr = librosa.load("field.wav", sr=48000, mono=True)
result = harmonic_f1_search(y, sr, f1_min=20, f1_max=200, n_harmonics=32)
score = harmonicity_score(y, sr, f1=result["f1"], n_harmonics=32)
separated = harmonic_mask(y, sr, f1=result["f1"], bandwidth_hz=40, strict=False)
```

## 2. Harmonic Explorer components (`tools/harmonic_explorer_components.py`)

A standalone tool written to decouple the explorer from the main app. It
combines analysis + visualization + live OSC control.

| Component | Purpose |
|---|---|
| `AudioLoader` | Load mono WAVs, partial/centered windows, soundfile-based |
| `HarmonicAnalyzer` | `harmonicity()` and `candidates()` for f0 search |
| `SpectrogramRenderer` | PNG spectrum + spectrogram with harmonic grid overlay |
| `HarmonicController` | OSC control of beacon and shaper |
| `HarmonicPerformanceEngine` | Standalone shaper + Launchpad + beacon bridge |

### Key insight

The explorer already explored the idea of "field recording as a performance
interface": load a sample, analyze its f0, and retune the beacon + play the
shaper based on that analysis. This is the direct ancestor of the
"samples loopeables as ratio sources" work.

## 3. beacon-spatial analysis (`/home/nicolas/Projects/beacon-spatial/`)

The beacon-spatial project has its own analysis history in `research/` and its
web UI (`webui.py`) includes sensor-to-harmonic mappings. The sensor interpreter
treats phone orientation/motion as oscillatory processes that modulate the
beacon field — conceptually similar to treating a sample as an oscillatory
control source.

## 4. Three iterations of analysis

Based on the repository history, the three analysis experiments are:

1. **F0 + harmonicity + mask** (nh-analysis): find f0 and separate harmonic
   vs. residual.
2. **Phideus descriptors** (nh-analysis): H-series, V4-linear, V4-log, A4-16k
   descriptors for speaker/emotion-style characterization.
3. **Harmonic Explorer** (tools): end-to-end tool with spectrum + spectrogram +
   OSC control + performance engine.

## 5. What to reuse for the future HIT analyzer

- `nh_analysis.harmonicity` for f0 search and harmonicity score.
- `nh_analysis.mask` for harmonic/residual separation.
- `nh_analysis.phideus` for voice-like descriptors.
- `tools/harmonic_explorer_components.py` as the skeleton for the new tool:
  - `AudioLoader` for loading samples.
  - `HarmonicAnalyzer` for f0/harmonicity.
  - `SpectrogramRenderer` for visualization.
  - `HarmonicController` for OSC output.

## 6. Open questions

- Should the dedicated HIT analyzer produce a "phase vector" (f1, phases,
  gains) that can be loaded directly into the beacon as a preset?
- Should it output a descriptor file that the sample layer can consume as a
  static ratio source?
- How does ResonantNeuralNet's phase-manifold idea map to these descriptors?
  (See `docs/rnn-sound-notes.md` for that investigation.)
