# NaturalHarmony Foundational Toolkit — Roadmap

**Date:** 2026-07-06  
**Scope:** Foundational milestone for a unified Harmonic Beacon software toolkit.  
**Constraint:** No physical tines hardware dependency. Backward compatibility only when it does not complicate the architecture.

---

## 1. Foundational goal

Extract a reusable, renderer-neutral software toolkit from the current ecosystem so that the Harmonic Beacon can run as a shared base field on a server and a personalized local model on each client.

The toolkit is scoped to **software-only** targets first:
- `digital-beacon` (32-band SuperCollider + Python Shaper)
- `NaturalHarmony` (MIDI → harmonic mapping + Shaper)
- `beacon-spatial` (13-band prototype + phone sensor interpreter)
- `EEG-Game` (Muse EEG sensor input)
- `Phideus` (H-series, V4-lin, A4-16k descriptors)

Physical tines (`harmonic-beacon-tines`) are **out of scope** as a hardware target. We will reuse its clean state-store patterns if useful, but we will not design for or constrain the architecture around 5-element tines.

---

## 2. Core architectural decisions

### 2.1 Canonical model: `HarmonicField`

The canonical object is a time-varying harmonic field, not a fixed voice or band count.

```text
HarmonicField
  f1(t)                     float Hz
  partials: dict[n, Partial]
  residual: Residual        audio or parametric noise
  descriptors: dict[str, Descriptor]   optional
  modulations: list[Mapping]  sensor/control → param
  transport: Transport        clock, play/seek state

Partial
  n: int                    harmonic index
  freq: float               = n * f1(t) (or explicit override)
  gain: float
  phase: float
  width: float              optional
  pan: float or spatial     optional
  envelope: Envelope        optional

RendererCapabilities
  max_partials: int
  supports_phase: bool
  supports_spatial: bool
  spatial_mode: str         none | hrtf | ambisonic
  supports_residual: bool
```

13-band, 32-band, and any additive Shaper are **projections** of this field, not separate formats.

### 2.2 Server / client split

- **Server** owns the shared base field, session clock, preset/session store, source audio, descriptor streams, and device bridges.
- **Client** computes its own local model, applies sensor/control modulations, and renders locally.

This supports the long-term goal: "everyone receives the same Beacon signal, but each listener's local model is different."

### 2.3 Protocols

- **Internal control plane:** typed JSON over WebSocket.
- **Edge/legacy:** OSC gateway for SuperCollider, MIDI/Launchpad, and any hardware bridges.
- No unified OSC address space as the canonical model. OSC is a compatibility layer only.

### 2.4 Presets

- Renderer-neutral JSON schema.
- Versioned from day 1.
- Capabilities-based projection to any renderer.
- Bidirectional migrations only where they are simple and useful. Do not preserve legacy quirks that complicate the schema.

### 2.5 Descriptors

- Extract Phideus `H-series`, `V4-lin`, and `A4-16k` into a NumPy-first library with a pluggable F0 provider.
- PyTorch is optional for research, not required for the toolkit.

### 2.6 Sensors

- Normalized `SensorEvent` stream: `timestamp`, `type`, `value`, `confidence`, `rate`, `units`, `calibration`.
- Adapters for Muse EEG (from `EEG-Game`) and phone IMU (from `beacon-spatial` sensors branch).
- Declarative `MappingLayer` binds sensor streams to model parameters.

---

## 3. Module structure (foundational)

Keep packages small. Do not split into many packages before interfaces stabilize.

| Module | Responsibility | Reuses from existing code |
|---|---|---|
| `nh-core` | `HarmonicField`, `Partial`, `RendererCapabilities`, basic math (f1→freq, cents). Pure, minimal deps. | NaturalHarmony `harmonics.py` |
| `nh-presets` | Versioned schema, validation, migrations, projection to capabilities. | digital-beacon preset logic |
| `nh-model` | Portable model state, snapshots, modulation. | `VoiceParameterStore` patterns from digital-beacon |
| `nh-analysis` | F0, harmonicity, harmonic mask, Phideus descriptors. | digital-beacon analysis tools + Phideus `vocal_descriptors.py` |
| `nh-control` | MIDI/Launchpad events, normalized control events, mapping graph. | NaturalHarmony key mapping + digital-beacon Launchpad code |
| `nh-sensors` | Sensor adapters, `SensorEvent` stream, calibration. | EEG-Game Muse bridge + beacon-spatial IMU interpreter |
| `nh-renderers` | Adapters: Python/sounddevice, Web Audio/AudioWorklet, SuperCollider/ATK. | digital-beacon Shaper + SC bridge |
| `nh-protocol` | Typed WebSocket messages + OSC compatibility mappings. | digital-beacon `/beacon/*` OSC patterns |
| `nh-runtime` | Session server, clock, capability negotiation, base-field distribution. | FastAPI/WebSocket server from digital-beacon |

Optional / later:
- `nh-nature` — procedural bioacoustic models only if useful for the software synth.

Out of scope:
- Physical tines hardware support.
- 5-element fixed-tuning constraints.
- Deep retro-compatibility with broken/legacy formats.

---

## 4. What to keep, refactor, drop

### Keep
- NaturalHarmony `harmonics.py` + `key_mapper.py` (pure math, 59 tests).
- `digital-beacon` 32-band SuperCollider engine + `f1_bridge.py` + analysis tools.
- `digital-beacon` `VoiceParameterStore` / `AudioEngine` patterns (generalize).
- `digital-beacon` harmonic mask + recent Launchpad split-mode work.
- `EEG-Game` Muse OSC bridge + band-power + concentration estimator.
- `Phideus` `H-series`, `V4-lin`, `A4-16k` math.

### Refactor
- All state stores to support variable harmonic sets (not hardcoded 32 or 5).
- Preset handling into `nh-presets` with renderer-neutral schema.
- Web UIs into one shared client shell (FastAPI + WebSocket + static).
- OSC receivers into protocol adapters, not canonical model.

### Drop / freeze
- `beacon-spatial` 13-band engine as canonical; preserve only as a migration source.
- Ad-hoc Flask / stdlib http.server UIs.
- Hardcoded paths and home-directory assumptions.
- Duplicated additive Shaper implementations where they overlap.
- eeg-actuator (empty).

---

## 5. Phased roadmap (foundational)

### Phase 1 — Schema + core (highest leverage, lowest risk)
- `nh-core`: `HarmonicField`, `Partial`, `RendererCapabilities`, basic math.
- `nh-presets`: versioned schema + validation.
- Migrations from existing formats: digital-beacon 32-band, beacon-spatial 13-band, NaturalHarmony mappings.
- One-pagers: WebSocket/OSC protocol spec + `SensorEvent` spec.
- **Validation:** round-trip existing presets.

### Phase 2 — Model + control
- `nh-model`: portable state with snapshots and modulation.
- `nh-control`: MIDI/Launchpad input, normalized control events, mapping graph.
- `nh-protocol`: typed messages + OSC bridge table.
- **Validation:** digital-beacon can load/save `nh-presets` and control the Shaper via `nh-model`.

### Phase 3 — Analysis + sensors
- `nh-analysis`: F0, harmonicity, harmonic mask, Phideus descriptors (NumPy-first).
- `nh-sensors`: Muse EEG adapter + simulator; phone IMU adapter.
- **Validation:** analyze a voice sample and a field recording; stream simulated EEG focus.

### Phase 4 — Renderers
- Python/sounddevice reference renderer (the correctness oracle).
- Web Audio / AudioWorklet renderer (reach client).
- SuperCollider / ATK adapter.
- Capability negotiation between server and client.
- **Validation:** same preset sounds equivalent on Python and Web Audio renderers.

### Phase 5 — Base-field + local model prototype
- Server emits base field + clock + session.
- One client (Python or Web Audio) computes local model + applies sensor modulation.
- Measure latency, bandwidth, CPU.
- **Validation:** two clients with same base field but different sensor mappings produce different outputs.

### Phase 6 — Consolidation
- Retire duplicated UIs and engines.
- Cross-renderer tests.
- Documentation.
- **Validation:** end-to-end session from analysis → preset → playback → sensor modulation.

---

## 6. Open risks

1. **Client fidelity gap:** Web Audio may not match SuperCollider/ATK spatial quality. Mitigate by keeping Python as the oracle and validating perceptually.
2. **Sensor noise:** EEG focus is a low-bandwidth, noisy signal. Treat it as gentle modulation, not precise control.
3. **Base-field bandwidth:** parametric field is small; residual audio layer is not. Decide early whether residual travels as audio or stays server-side.
4. **Schema bloat:** capabilities must stay explicit or the universal schema becomes too big.
5. **Over-abstraction:** keep packages thin. Allow direct use of `nh-core` for performance paths.
6. **Research hypothesis:** "natural harmonic series as privileged structure" must remain testable, not baked in unfalsifiably. Keep residual paths and comparison modes.

---

## 7. First concrete deliverable

A Python package `nh-core` + `nh-presets` that can:
1. Load a digital-beacon 32-band preset.
2. Load a beacon-spatial 13-band preset.
3. Represent both as a `HarmonicField`.
4. Project each to the other's capability profile.
5. Save back to a versioned canonical JSON.

This is the milestone that unlocks everything else without touching audio engines.

---

*Generated from the IA-Bridge forum (Grok, Claude, Codex) on 2026-07-06. Claude round-1 was empty due to an ia-bridge MCP bug; the roadmap was synthesized from Grok and Codex round-1 plus all three critique rounds.*
