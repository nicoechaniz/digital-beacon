# NaturalHarmony UI/UX Roadmap

**Date:** 2026-07-06  
**Source:** Follow-up forum with Grok, Claude, Codex (`bridge-1783318824124-8f9d9351`).  
**Goal:** Turn the foundational toolkit into a live, playable instrument.

## Principles

- One web UI (TypeScript/Vite) as the single client.
- One Python host (WS + HTTP) speaking the existing `nh-runtime` contract.
- `nh-model` is the single source of truth; UI is view+controller only.
- Every renderer (WebAudio, sounddevice, SC/ATK, Shaper) is behind `RendererCapabilities`.
- Variable-partial-first; 32-band only at the SC/OSC boundary.
- Do not build on the legacy `digital_beacon` FastAPI; reuse it only as reference.

## Milestone 1 — Live Performance Console

Minimum usable instrument for live performance and exploration.

1. **nh-ui host process** — FastAPI/Starlette serving static UI + WebSocket + preset HTTP + WAV upload.
2. **Web client shell** — Vite/TS, renderer selector, connection status.
3. **Performance controls** — f1 coarse/fine, per-partial gain, mute/solo, master, PANIC.
4. **Preset load/save** — Browse 26 migrated presets; save current state as new snapshot.
5. **Launchpad Mini mirror** — On-screen pad grid + mode indicator.
6. **Basic sensor meters** — Muse focus, IMU yaw, phone tilt.
7. **Sensor-influence safety** — Global 0-100% master; per-source enable; hard kill at 0%.
8. **Renderer integration** — WebAudio (existing worklet) + sounddevice + SC/ATK via caps.
9. **Operational robustness** — WS auto-reconnect, audio permission handling, renderer/sensor/MIDI status.
10. **M1 e2e test** — Headless Chrome: load preset → change f1/partial → audio changes → PANIC silences.
11. **Verify WS contract** — Confirm full snapshot vs delta; update docs if needed.

## Milestone 2 — Mapping + Analysis + Expression

Advanced expressive and analytical features.

1. **Visual mapping matrix** — source → transform (curve/range/smoothing/deadzone/depth) → target.
2. **Sensor panel** — Focus meter, yaw→azimuth compass, tilt 2D pad, simulators.
3. **Retune from field recording** — Drag WAV → preview F0/harmonicity/mask → one-click retune.
4. **Preset editor** — Edit f1, partial params, metadata; capability warnings/projection.
5. **Preset morph/crossfade** — Between two snapshots via `nh-model`.
6. **Session replay/export** — Record control/sensor/model stream.

## Open questions to verify before build

- Does the WS server broadcast a full field snapshot or deltas?
- Does `sc_osc.py` cover all needed `Partial` fields for SC/ATK?
- What are the exact morph semantics in `nh-model`?
- Browser audio latency vs sounddevice for live performance.
- SC process lifecycle/health surfacing (start/status/kill).

## Acceptance tests

M1:
- `pytest` stays green (58 tests + new UI tests).
- E2E via headless Chrome: preset → f1/partial change → audio → record → PANIC silence.
- Sensor-influence 0% = hard kill.
- WS reconnect preserves state.
- Renderer capability change hides/disables controls without losing state.

M2:
- Sensor simulator → matrix → target parameter moves in UI + audio.
- Retune known-pitch WAV → F0 detected → field f1 matches.
- Renderer parity across WebAudio/sounddevice/SC.
