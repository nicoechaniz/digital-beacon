# digital-beacon / NaturalHarmony Toolkit

**The Harmonic Beacon as a modular software toolkit.** Natural harmonics, end-to-end.

This repo is the new home of a unified toolkit built from:

- `digital-beacon` (32-band SC binaural spatializer + Shaper additive synth)
- `NaturalHarmony` (MIDI middleware, key mapping, harmonic math)
- `beacon-spatial` (13-band ATK spatializer + phone sensor UI)
- `EEG-Game` (Muse neurofeedback + concentration estimator)
- `Phideus` (H-series, V4-lin, A4-16k harmonic descriptors)

> **Scope:** Software-only. Physical tines / hardware are out of scope for this foundational milestone. Backward compatibility is not preserved when it complicates the architecture.

## Packages

| Package | Purpose |
|---|---|
| `nh-core` | Renderer-neutral `HarmonicField`, `Partial`, `Residual`, `RendererCapabilities`, math helpers |
| `nh-presets` | Versioned preset schema + migrations from legacy 13/32-band JSON |
| `nh-model` | Portable runtime state with snapshots and modulation |
| `nh-control` | Normalized `ControlEvent`, mapping graph, Launchpad adapter |
| `nh-analysis` | F0, harmonicity, harmonic mask, Phideus descriptors (NumPy-first) |
| `nh-sensors` | EEG processor / Muse OSC adapter, phone IMU adapter, simulators |
| `nh-renderers` | Python/sounddevice (reference), WebAudio AudioWorklet, SuperCollider OSC |
| `nh-runtime` | WebSocket base-field server + local model client |

## Protocols

- `docs/protocols/websocket-messages.md` — canonical internal control plane
- `docs/protocols/sensor-event.md` — normalized sensor event schema
- `docs/protocols/osc-bridge.md` — OSC edge/gateway compatibility layer

## Roadmap

See `docs/ROADMAP_foundational_toolkit_2026-07-06.md`.

## How to run tests

```bash
cd ~/Projects/digital-beacon
python3 -m venv .venv
source .venv/bin/activate
pip install -e packages/nh-core -e packages/nh-presets -e packages/nh-model \
    -e packages/nh-control -e packages/nh-analysis -e packages/nh-sensors \
    -e packages/nh-renderers -e packages/nh-runtime
pytest packages/
```

## Legacy architecture

The old 32-band instrument stack (SuperCollider + ATK + Shaper + Launchpad) is still in `digital_beacon/` and `beacon.scd`. It will be migrated to the new renderer architecture incrementally rather than replaced in one step.

## License

GPL-3.0-or-later (matches NaturalHarmony).
