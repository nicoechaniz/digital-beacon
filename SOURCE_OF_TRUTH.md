# digital-beacon — Source of Truth

## Locked decisions (2026-06-22, pre-Kai demo)

1. **f1 default = 40.0 Hz**, NOT fixed. Modulation slot reserved
   (`F1_MOD_POINTS = []`, 12 discrete points TBD).
2. **32 bands**, one BPF per natural harmonic. Band N center =
   `f1 * N` for N = 1..32. No wide-band grouping.
3. **Wet/dry mix → on/off per band**. Each band has independent
   on/off toggle (`/beacon/on/N`).
4. **No MPE**, no Surge XT. The Shaper additive synth (pure sines via
   `sounddevice`) IS the synth.
5. **No EEG, no mobile sensors today**. Slots reserved.
6. **Single audio path**: SC binaural + Shaper sines → same default
   sink via PipeWire. Headphones required (HRTF).
7. **Launchpad Mini as primary surface**. Minilab3 as auxiliary.
8. **Repo**: `~/Projects/digital-beacon/`, local git for now, no
   remote.

## What may evolve

- Band count (32 → 64 is trivial, just change `N_BANDS` constant)
- f1 modulation curve (12 points TBD)
- Audio routing (separate sound cards if needed in Costa Rica)
- Visualization layer (port from NaturalHarmony visualizer)
- Web UI surface

## What is non-negotiable

- Identity of the bands = natural harmonics of f1 (this IS the
  Harmonic Beacon, not a generic spatializer)
- Binaural rendering via ATK Listen (HRTF is core to the experience)
- No "fake" pitch shift (granular, phase vocoder) — only varispeed
  (sample rate change) preserves the original harmonic identity of
  the recorded source
