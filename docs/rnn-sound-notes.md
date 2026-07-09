# ResonantNeuralNet ideas applied to digital-beacon

This document connects the concepts in
`https://hackmd.io/@nicoechaniz/ResonantNeuralNet` to our sound work.

## Core mapping

| ResonantNeuralNet concept | digital-beacon equivalent |
|---|---|
| Phase-manifold (N-harmonic torus) | The 32-band beacon field: `f1` + per-band `gain/az/dist/q/on` |
| Jpsh! impulse | A looped sample chunk that perturbs the field |
| Superposition as nonlinearity | Beacon + Shaper + Sample layers mixed in audio |
| Golden-ratio perturbation detector | A consonance/dissonance measure for filtering |
| Hierarchical representation | `f1` sets global geometry; higher bands add local detail |
| Nodal topology | Spatial positions of the 32 bands around the listener |

## Concrete applications

### 1. Filters

Instead of a traditional lowpass/highpass, build a **harmonic mask filter** that
passes energy near integer multiples of `f1` and attenuates the rest. The
existing `nh_analysis.mask.harmonic_mask()` already does this. A richer version
could use a soft mask shaped by the beacon's phase manifold: boost bins that are
consonant with the current field and suppress those that are not.

A "φ-perturbation detector" would flag any spectral energy whose ratio to `f1`
is close to the golden ratio (or any irrational), treating it as unstable and
attenuating it. This gives a natural consonance filter.

### 2. Analysis

Treat a sample as a **point on the phase manifold**. Extract:

- `f1` (fundamental) as the manifold's base frequency.
- Per-band gains, phases, and spatial positions that best reconstruct the
  sample's long-term spectrum.
- A residual component for everything outside the harmonic grid.

The result is a compact vector `(f1, [gains], [phases], [az], [dist])` that can
be loaded as a beacon preset. Any node can replay the field from this vector.
This matches RNN's "compact phase vector + local replay" idea.

### 3. Compression

Store field recordings not as waveforms but as:

```
(f1, gains[32], phases[32], az[32], dist[32], envelope, residual)
```

The harmonic part is reconstructed by the beacon's resonant synthesis. Only the
residual (noise, transients) needs to be stored as audio. For highly harmonic
field recordings this could be orders of magnitude smaller.

### 4. Modulation (the current experiment)

A looped sample is a continuous source of Jpsh! impulses. Its descriptors
(rms, f0_ratio, band energies) become control signals that push the beacon's
phase manifold around. This is already implemented in
`digital_beacon/sample_layer.py` and `digital_beacon/sample_modulator.py`.

The next step is to let a sample "tune" the beacon to a specific place or
person: extract that entity's characteristic phase vector, then apply it as a
modulation offset so the beacon resonates with that identity.

### 5. Synthesis

The Shaper is already a resonant synthesizer: each voice is a sine at an
integer multiple of `f1`. The RNN framing suggests we can think of the Shaper as
a small phase-manifold network where activating a voice is setting a phase
position. Sample ratios can modulate the network's weights (gain, pan, shape)
in real time.

## Open research questions

- Can we define a distance between two phase-manifold states (two presets or two
  samples)? This would let us "morph" from one place/person to another.
- Can we use persistent homology (nodal topology invariants) as a fingerprint
  for a field recording, as RNN suggests for its ledger state?
- How does the Kuramoto synchronization idea apply when multiple samples (or
  live input + samples) are simultaneously perturbing the field?

## Relation to the current roadmap

The sample layer work is the first practical implementation of the RNN
"Jpsh! impulse" idea. The analysis inventory (`docs/analysis-inventory.md`) is
the source of components for the dedicated HIT analyzer. The harmonic mask in
`nh_analysis` is the first resonant filter. We are building the sound tool
first; the full RNN substrate remains a research direction, not an immediate
product.
