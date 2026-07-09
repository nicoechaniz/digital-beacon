# Next-iteration brief for ia-bridge forum

## Context

We are working on `digital-beacon`, a live harmonic instrument with two audio
systems:

- **Beacon**: 32-band harmonic spatializer running in SuperCollider (sclang on
  port 57120). Controls: master, f1, vsrate, and per-band gain/az/dist/q/on.
- **Shaper**: 32-voice additive synthesizer running in Python (digital_beacon
  AudioEngine). Controls: master, sidechain, lfo_amount, and per-voice
  gain/pan/shape/lfo_gain/lfo_pan/lfo_phase.

We just finished a **declarative sample-to-modulation system** that uses a
looped audio sample as a control source for both the beacon and the shaper.

## What is implemented now (2026-07-08)

- `digital_beacon/sample_layer.py`: loads a WAV, loops it, analyzes chunks, and
  extracts descriptors: rms, f0_hz, f0_ratio, centroid, bandwidth, flatness,
  rms_delta, rms_smooth, f0_stability, centroid_delta, inharmonicity, and
  energy in 32 octave-scaled bands.
- `digital_beacon/sample_modulator.py`: declarative mapping schema with
  descriptor, target_type, target_param, voice/band, scale, offset, min, max,
  smooth, threshold, invert, active. Validates targets per system.
- `digital_beacon/sample_manager.py`: wraps layer + modulator, supports
  built-in and user-saved presets.
- `static/index.html`: UI for sample upload/selection, and an editable Mapping
  Editor with rows, add/remove, preset load/save, apply.
- API endpoints: `/api/sample/{load,stop,state,mapping,list,upload,presets,preset,save-preset}`.
- Built-in presets: `default`, `tune-to-sample`, `spectrum-projection`,
  `timbre-filter`, `rhythmic-pump`.
- E2E test: `scripts/test_field_recording_e2e.py` loads a Costa Rica field
  recording and verifies f0 detection and shaper retuning.
- Documentation: `docs/mapping-presets.md`, `docs/rnn-sound-notes.md`,
  `docs/analysis-inventory.md`, `MEMORY.md`.

## Research output (RNN applied to mappings and filters)

Source: subagent reading local documents (`ResonantNeuralNet A Cymatic.md`,
`rnn-sound-notes.md`, `analysis-inventory.md`, `mapping-presets.md`, `MEMORY.md`,
current code).

### Proposed mappings (5)

1. **phase-manifold-tune** — `f0_hz` + `f0_stability` + `inharmonicity` retune
   the beacon to the sample's fundamental; unstable/harmonic content widens
   the base band Q. Maps all 32 sample bands to beacon band gains.
2. **jpsh-excitation** — `rms_delta` and `centroid_delta` drive transient hits
   on beacon master and shaper voice; sustained energy moves beacon distance.
3. **hierarchical-harmonics** — sample bands project onto beacon bands preserving
   low=global, high=local harmonic hierarchy.
4. **consonance-dissonance** — `inharmonicity` and `flatness` modulate beacon Q
   and shaper shape; `f0_stability` fine-tunes varispeed.
5. **nodal-topology-spatial** — each sample band moves the corresponding beacon
   band in azimuth and distance, bending the spatial field to match the sample's
   spectral topology.

### Proposed resonant filters (2)

A. **consonance-mask** — STFT-based filter that passes energy near integer
   ratios of f1 and attenuates near φ (golden ratio), implementing the
   RNN "golden-ratio perturbation detector" as a harmonic filter.

B. **adaptive-harmonic-mask** — reuse `nh_analysis.mask.harmonic_mask`, but adapt
   its bandwidth in real time from `flatness`, `inharmonicity`, and
   `f0_stability`; outputs harmonic + residual components.

### PoC suggestion

1. Extend `VALID_DESCRIPTORS` to `band_0..31`.
2. Implement `adaptive-harmonic-mask` in Python using `nh_analysis.mask`.
3. Add `phase-manifold-tune` preset.
4. Test A/B with a Costa Rica field recording and a voice sample.

### Open questions

- Where should the filter run: Python (flexible) or SuperCollider (low latency)?
- Should the mask output be a new audio source mixed into the beacon, or a control signal?
- How do we visualize the phase-manifold state in the UI?


## What we want from the forum

Given the current code and the RNN research output, propose a **plan for the
next iteration** that answers:

1. Which new mappings should we add and why? Prioritize the ones that have a
   clear HIT/RNN justification, not just more features.
2. Should we add a resonant filter module? If yes, where should it live
   (SuperCollider, Python, or both) and what is its interface?
3. How should we represent and process the "phase-manifold" of the beacon? Is
   it a new descriptor, a new state, or a visualization?
4. What would be the minimal PoC to verify the RNN-inspired ideas in sound?
5. Are there existing components in `nh-analysis` or `harmonic-explorer` that
   we should reuse rather than reimplement?
6. What would the UI changes look like?

Please produce:
- A ranked list of proposed features/mappings.
- For each, a one-paragraph justification from HIT/RNN.
- A suggested order of implementation (what to build first to test the core idea).
- A list of open questions or experiments needed before coding.

## Constraints to respect

- Keep using the working audio path: `start.sh --file` (SuperCollider + Python
  AudioEngine).
- Do not replace the renderer with a new sounddevice-based one.
- UI must stay in the current HTML/CSS/JS dashboard, no new frontend
  frameworks.
- All code lives in `/home/nicolas/Projects/digital-beacon`.

## Artifacts to consult

- `digital_beacon/sample_layer.py`
- `digital_beacon/sample_modulator.py`
- `digital_beacon/sample_manager.py`
- `static/index.html`
- `docs/mapping-presets.md`
- `docs/rnn-sound-notes.md` (initial notes only; RNN research output pending)
- `MEMORY.md`
