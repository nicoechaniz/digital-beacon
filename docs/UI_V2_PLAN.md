# NaturalHarmony v2 UI Plan

This is the plan for the replacement UI. It deliberately scraps the old v1-style dashboard surface.

## Goal

Build a playable, scene-aware user interface for NaturalHarmony as a live instrument.

The UI must expose the available v2 functionality without asking the performer to write Python or think in internal data structures.

Primary uses:

1. Load and keep sound presets.
2. Perform with a continuous Beacon drone and independent Shaper voices.
3. See and control all scene sources: Beacon, Shaper, Samples, Voice.
4. Edit the 32-band spatial field directly.
5. Use the Launchpad/Shaper voice grid from the browser as well as hardware.
6. Upload/analyze field recordings and turn analysis into musical controls: proposed f1, sample sources, Phideus descriptors.
7. See processors, LFOs, and modulation routing as part of the instrument.
8. Save the resulting scene as a preset.

## Non-goals

- No old v1 dashboard as the primary user surface.
- No user-facing renderer selector.
- No "Python vs WebAudio" choice in the UI.
- No code-first workflow for the performer.
- No retrocompatibility UI. Sound preset migration matters; old UI semantics do not.

## Architecture

The browser is a single-page scene workspace.

Backend source of truth:

- `SceneState` in `nh-model`
- `HarmonicScene` in `nh-core`
- `PresetV2` in `nh-presets`
- `AnalysisResult` in `nh-analysis`

Frontend source of truth:

- One immutable-ish client state object derived from `scene_snapshot`.
- All visible panels render from that object.
- All controls send path-addressed v2 control events.

Initial HTTP fetch:

- `GET /nh/v2/scene`

Control path:

- `POST /nh/v2/scene/control`

Preset path:

- `GET /nh/v2/presets`
- `GET /nh/v2/presets/{preset_id}`
- future: `POST /nh/v2/presets/{preset_id}/load`
- future: `POST /nh/v2/presets`

Source mixer path:

- `POST /nh/v2/scene/sources/{source_id}/mute`
- `POST /nh/v2/scene/sources/{source_id}/solo`

Analysis path:

- `GET /nh/v2/analysis/{sample_id}`
- future: `POST /nh/v2/analyze`

Live updates:

- Preferred: WebSocket event type `scene_snapshot` carrying `SceneState.scene_snapshot()`.
- Transitional acceptable for first usable UI: polling `GET /nh/v2/scene` at a low rate while interactions are being implemented.

## App shell

Required visible layout:

```text
┌──────────────────────────────────────────────────────────────────────────┐
│ NaturalHarmony v2                 Preset ▾  Save  Panic  Status         │
├───────────────┬──────────────────────┬───────────────────────────────────┤
│ Sources       │ Shaper / Launchpad   │ Spatial Field                     │
│ - Beacon      │ 8x8 harmonic pads    │ 32 band list + radar view          │
│ - Shaper      │ active voices        │ az / dist / q / on                 │
│ - Samples     │ panic                │                                   │
├───────────────┼──────────────────────┼───────────────────────────────────┤
│ Processing    │ LFO / Modulation     │ Analysis / Field Recordings        │
│ comb/spatial  │ routes by path       │ upload -> analyze -> propose f1    │
│ dynamics      │ sensor routing       │ Phideus / F0 / spectral display    │
└───────────────┴──────────────────────┴───────────────────────────────────┘
```

Required stable DOM ids:

- `app-shell`
- `preset-bar`
- `sources-panel`
- `source-card-beacon`
- `source-card-shaper`
- `samples-panel`
- `shaper-panel`
- `launchpad-grid`
- `active-voices`
- `panic-button`
- `spatial-panel`
- `spatial-band-list`
- `spatial-radar`
- `processing-panel`
- `lfo-panel`
- `analysis-panel`
- `analysis-f0`
- `analysis-phideus`
- `analysis-proposed-f1`
- `event-log`

These ids are part of the E2E test contract. If the UI changes visually, the ids should remain until tests are updated intentionally.

## Panels

### 1. Preset bar

Purpose: keep sound presets accessible.

Shows:

- Current preset name.
- Preset dropdown from `/nh/v2/presets`.
- Load button.
- Save scene button.
- Panic button.
- Connection/state indicator.

Acceptance:

- Dropdown populates from v2 presets.
- Loading a preset changes `scene_snapshot`.
- Saving creates a v2 scene preset.

### 2. Sources panel

Purpose: control all audio/material sources.

Source cards:

- Beacon: f1, master gain, vsrate, active band count, mute/solo.
- Shaper: master gain, active voice count, max voices, mute/solo.
- SampleSource: audio path/name, loop, gain, proposed f1, mute/solo.
- VoiceSource: input state / analysis state when available.

Control events:

- `sources.<source_id>.f1`
- `sources.<source_id>.f1_offset`
- `sources.<source_id>.master_gain`
- `sources.<source_id>.gain`
- `sources.<source_id>.loop`

### 3. Shaper / Launchpad panel

Purpose: playable harmonic voice surface.

Shows:

- 8x8 or 8x4 pad grid with harmonic number labels.
- Active voices from `runtime.active_voices`.
- Momentary/toggle visual states.
- Voice gain/envelope if present.
- Panic button.

Contract:

- Pad controls affect ShaperSource only.
- Pad controls never mutate BeaconSource bands.

### 4. Spatial editor

Purpose: edit the 32-band binaural field.

Shows:

- 32-band list/table.
- For each band: harmonic n, on/off, azimuth, distance, q, gain.
- Radar/top-down visualization of band azimuth/distance.

Control paths:

- `sources.beacon.bands.<n>.az`
- `sources.beacon.bands.<n>.dist`
- `sources.beacon.bands.<n>.q`
- `sources.beacon.bands.<n>.on`
- `sources.beacon.bands.<n>.gain`

Contract:

- Spatial data remains spatial-only.
- Metadata never goes into `Partial.spatial`.

### 5. Processing panel

Purpose: expose sound-shaping processors.

Shows:

- Harmonic comb processor: bandwidth, q_factor, wet_dry, residual, num_harmonics.
- Binaural spatializer: 13/32-band mode, HRTF profile, rotation, head radius.
- Filter processor: type, cutoff, q, gain, order.
- Dynamics processor: mode, threshold, ratio, attack, release, knee, makeup.

Control paths:

- `processing_chain.processors.<processor_id>.params.<name>`
- `processing_chain.routing.<source_id>`

### 6. LFO / modulation panel

Purpose: visible path-targeted modulation.

Shows:

- LFO list: waveform, rate, strum divisor, amount, target path.
- Sensor routes: source, target path, scale, offset, enabled, clamped range.

Control paths:

- `lfos.<lfo_id>.*`
- `modulation_routes.<route_id>.*`

### 7. Analysis / field recording panel

Purpose: make recordings musically actionable.

Flow:

1. Upload WAV.
2. Analyze.
3. Display `AnalysisResult`:
   - F0 summary / track.
   - Spectral metrics.
   - Phideus H-series / V4 / A4-16k descriptors.
   - Emotion/speaker if available.
   - Proposed f1.
4. Add result as SampleSource.
5. Apply proposed f1 to Beacon.
6. Save scene preset.

## Frontend implementation plan

Use the existing Vite/TypeScript package, but replace the user-facing app:

- `packages/nh-ui/web/src/main.ts` becomes scene-first.
- `packages/nh-ui/web/src/ui.ts` becomes pure render functions for the panels above.
- `packages/nh-ui/web/src/style.css` becomes the new workspace visual system.
- `packages/nh-ui/web/src/ws.ts` should support `scene_snapshot` if WebSocket is added.

No new framework unless there is a concrete reason. Vanilla TypeScript is enough.

## E2E testing contract

A UI card is not complete until these pass:

1. TypeScript/build:

```bash
cd packages/nh-ui/web
npm run build
```

2. Python tests:

```bash
cd /home/nicolas/Projects/digital-beacon
.venv/bin/python -m pytest packages/nh-ui -q
```

3. Browser smoke test:

- Start the v2 server.
- Open `http://127.0.0.1:8080` with headless Chrome or Playwright system Chrome.
- Assert required DOM ids exist.
- Assert no user-facing renderer selector exists.
- Assert source cards render from `/nh/v2/scene`.
- Click one shaper pad.
- Verify `/nh/v2/scene` shows an active shaper voice.
- Click panic.
- Verify active voices clear.
- Save screenshot to `/tmp/nh-ui-v2-smoke.png`.

Preferred command once harness exists:

```bash
cd /home/nicolas/Projects/digital-beacon
.venv/bin/python -m pytest packages/nh-ui/tests/test_ui_v2_e2e.py -q
```

## Definition of done for the UI round

The round is done only when:

- The old v1 dashboard is no longer the primary interface.
- The page visibly exposes all v2 functional areas.
- The user can operate Beacon, Shaper, spatial bands, presets, LFO/modulation, and analysis from the browser.
- Browser E2E tests prove rendering and interaction.
- The implementation is committed phase-by-phase and pushed only after tests pass.
