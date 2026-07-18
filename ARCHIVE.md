# ARCHIVE — digital-beacon

## Status: ARCHIVED (2026-07-18)

This repository is **retired from active development**. Its living subsystems
have been migrated to new, purpose-built repos as part of the beacon ecosystem
re-architecture (migration map:
`~/Projects/HarMoCAP/Biblioteca/rearquitectura-ecosistema-beacon/reportes_agentes/04-digital-beacon.md`
§6). Nothing here should receive new features. The repo is archived as-is and
remains **readable history**: git history preserves everything, including code
that was moved, split, or deliberately discarded.

## What the system was

The digital fork of the harmonic beacon: a 32-voice software instrument that
grew a NaturalHarmony Shaper fork (additive synth + waveshaper + LFO +
sidechain) together with a SuperCollider-based 32-band spatialized nature
layer (resonant filters, sample playback, field-recording modulation presets),
plus a field-recording analysis pipeline. The bicephalous modules
(`sample_modulator.py`, `sample_manager.py`, with `"beacon"` vs `"shaper"`
targets) were the exact seam where the two halves mixed — and the reason the
ecosystem was re-architected.

## Destination map (where each subsystem lives now)

**→ `harmonic-shaper`** (standalone, `pip install -e .`, contracts in
`contracts/shaper.contract.json`):

- `audio_engine.py`, `state.py`, Shaper/LFO/sidechain sections of `config.py`
- `midi_control.py`
- `osc_receiver.py` — the `/digital/*` branch
- `/api/shaper/*` endpoints of `api.py`
- `tools/synth_pure.py`

**→ `beacon-spatial`:**

- `resonant_filter.py`, `sample_layer.py` (+ vendorized nh mask) →
  `beacon-spatial/nature/`
- `sample_player.py` + the `\sample_player` SynthDef → `beacon-spatial`'s
  `beacon.scd` and `/beacon/nature/*` OSC namespace
- the beacon half of `sample_modulator.py` (the 4 presets
  `spectrum-projection`, `harmonic-projection`, `consonance-gate`,
  `timbre-filter`) → `beacon-spatial/nature/sample_modulator.py`
- nature samples → `beacon-spatial/assets/nature-samples/` (gitignored, with a
  SHA-256 MANIFEST)

**→ `harmonic-weaver`** (future home): routing / modulation.

**Deferred by design:** the shaper half of `sample_modulator.py` is explicitly
unimplemented pending F5 work.

## Archived / discarded here

- `packages/` (nh-toolkit v2) — refactor set aside ("apartado"); not carried
  forward. Note: `ROADMAP.md` and much of `docs/` describe that NaturalHarmony
  v2 architecture, not the migrated one.
- `normalized_analysis/`, `originals/` — broken one-off symlinks, removed in
  T0.2.
- `: RTK && ` — junk directory from a mis-pasted `mkdir -p`, removed in T0.2.
- duplicate `venv/` / `.venv/` — removed in T0.2.
- `data/migrated_presets/`, `data/sources/` — one-off data.
- The 32-band spatializer reference (`beacon.scd`) and `f1_bridge.py` are kept
  as historical reference; their living counterparts are in `beacon-spatial`.

## Successors

- **`harmonic-shaper`** — canonical synth (the 32-voice + waveshaper + LFO +
  sidechain evolution of this repo's Shaper).
- **`beacon-spatial`** — spatialized nature layer (filters, sample playback,
  modulation presets).
- **`harmonic-weaver`** — routing/modulation (future).

For orientation inside this archive, `MEMORY.md` and `docs/` remain in place.
Do not resurrect modules from here without checking the successor repos first.
