# Sample-to-Beacon/Shaper Mapping Presets

This document describes the built-in mapping presets in `digital_beacon`. A
mapping connects descriptors extracted from a looped sample to control targets
in the beacon (SuperCollider) and the shaper (additive synth).

## Descriptors

Each analyzed chunk produces these descriptors:

| Descriptor | Meaning |
|---|---|
| `rms` | Total energy of the chunk |
| `f0_hz` | Estimated fundamental frequency (Hz) |
| `f0_ratio` | `f0_hz / beacon_f1` |
| `centroid` | Spectral centroid (brightness) |
| `bandwidth` | Spectral bandwidth |
| `flatness` | Spectral flatness (noise vs tonal) |
| `rms_delta` | Change in RMS from previous chunk |
| `rms_smooth` | Moving average of RMS |
| `f0_stability` | Stability of recent f0 estimates (0..1) |
| `centroid_delta` | Change in centroid from previous chunk |
| `inharmonicity` | Fraction of energy NOT on integer multiples of f0 |
| `band_0` .. `band_31` | Energy in 32 octave-scaled frequency bands |

## Targets

### Beacon
- `master` — overall beacon gain
- `f1` — fundamental frequency
- `vsrate` — varispeed factor
- per-band (`gain`, `az`, `dist`, `q`, `on`) — requires `band` 1..32

### Shaper
- `master` — overall shaper gain
- `sidechain` — side-chain amount
- `lfo_amount` — LFO depth
- per-voice (`gain`, `pan`, `shape`, `lfo_gain`, `lfo_pan`, `lfo_phase`) — requires `voice` 1..32

## Built-in presets

### `default`
- `rms` → `beacon.master` (scale 2.0, offset 0.2, max 1.5)
- `f0_ratio` → `beacon.vsrate` (scale 0.2, offset 1.0, range 0.25..2.0)
- `band_0` → `shaper.voice_1.gain` (scale 0.05, max 1.0)
- `rms` → `shaper.master` (scale 1.0, offset 0.2, max 1.0)

**Effect:** The sample's energy pumps the beacon and shaper volume; its pitch ratio
speeds or slows the beacon; low-frequency energy excites the shaper's fundamental.

### `tune-to-sample`
- `f0_hz` → `beacon.f1` (scale 1.0, smooth 0.9, range 20..200 Hz)
- `rms` → `beacon.master` (scale 1.0, offset 0.2, max 1.5)
- `rms` → `shaper.master` (scale 1.0, offset 0.2, max 1.0)

**Effect:** The beacon and shaper retune to the sample's fundamental pitch. The
field becomes "sintonized" to the voice or place.

### `spectrum-projection`
- `band_0` → `beacon.band_1.gain` (smooth 0.8, max 1.5)
- `band_1` → `beacon.band_7.gain` (smooth 0.8, max 1.5)
- `band_2` → `beacon.band_14.gain` (smooth 0.8, max 1.5)
- `rms` → `shaper.master` (scale 1.0, offset 0.2, max 1.0)

**Effect:** The sample's spectral energy illuminates corresponding bands of the
beacon, projecting its harmonic structure onto the field.

### `timbre-filter`
- `centroid` → `shaper.voice_1.shape` (scale 0.001, max 1.0, smooth 0.9)
- `flatness` → `beacon.band_1.q` (scale 2.0, offset 0.5, max 2.0, smooth 0.9)
- `rms` → `beacon.band_1.dist` (scale 5.0, max 10.0, smooth 0.8)

**Effect:** Brightness of the sample enriches the shaper timbre; noisiness
widens the beacon filter; energy pushes the beacon spatial distance.

### `rhythmic-pump`
- `rms` → `shaper.lfo_amount` (scale 2.0, max 1.0, smooth 0.7)
- `rms` → `beacon.master` (scale 1.5, offset 0.2, max 1.5, smooth 0.7)
- `rms_delta` → `shaper.voice_7.gain` (scale 0.5, max 1.0, threshold 0.01)

**Effect:** The envelope of the sample drives an LFO pump and rhythmic accents
on a shaper voice.

### `consonance-gate`
- `harmonicity` → `beacon.master` (scale 1.0, offset 0.2, max 1.2, smooth 0.9)
- `residual_rms` → `beacon.band_1.q` (scale 2.0, offset 0.5, max 3.0, smooth 0.9)
- `residual_ratio` → `shaper.voice_1.shape` (scale 1.0, max 1.0, smooth 0.9)
- `rms` → `shaper.master` (scale 0.8, offset 0.2, max 1.0, smooth 0.8)

**Effect:** Harmonic content opens the beacon; residual/noisy content widens the
base filter and enriches the shaper timbre. Inspired by the ResonantNeuralNet
consonance/dissonance detector.

### `harmonic-projection`
- `harm_i` → `beacon.band_(i+1).gain` for i = 0..31
- `harm_i` → `shaper.voice_(i+1).gain` for i = 0..31
- `rms` → `beacon.master` and `shaper.master`

**Effect:** The sample's energy at each harmonic of the beacon's `f1` is
projected onto the corresponding beacon band and shaper voice. This is the
RNN "phase-manifold" mapping in audible form.

## User presets

Custom mappings can be saved from the dashboard UI under "Mapping Editor". They
are stored in:

```
~/Music/digital-beacon-mapping-presets/<name>.json
```

## How to use from code

```python
from digital_beacon.sample_manager import SampleManager
from digital_beacon.state import VoiceParameterStore

store = VoiceParameterStore()
sm = SampleManager(store)
sm.load("/path/to/sample.wav")
sm.apply_preset("tune-to-sample")
```

## How to use from the UI

1. Start the system: `./start.sh --file`
2. Open `http://127.0.0.1:8080`
3. Load or upload a sample in the "Sample Modulation" panel.
4. Open the "Mapping Editor" panel.
5. Choose a preset from the dropdown and click LOAD, or add/edit rows manually
   and click APPLY.
6. Save your custom mapping with a name.
