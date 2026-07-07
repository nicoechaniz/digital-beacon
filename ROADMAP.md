# NaturalHarmony Next Phase Roadmap

Date: 2026-07-07

This roadmap is based on the current `main` branch at commit `92a1a9d0078c`.
The goal is to restore the lost instrument architecture without adding
short-term patches to the current flat `HarmonicField` model.

## Current Architecture Snapshot

- `nh-core` defines a single renderer-neutral `HarmonicField` with flat
  `partials`, optional `Residual`, descriptors, transport, and loose
  `Partial.spatial` dictionaries.
- `nh-model` owns one `ModelState`. Control events mutate modulation fields on
  that single field. Launchpad `pad_on`, `pad_off`, and `pad_toggle` currently
  become `partial_gain_offsets`.
- `nh-runtime` broadcasts modulated `base_field` snapshots over WebSocket.
  `LocalModelClient` renders those snapshots directly.
- `nh-renderers` Python and WebAudio renderers synthesize every positive-gain
  partial continuously. They do not render `spatial`, residual audio, comb
  filtering, envelopes, or multiple sources.
- `nh-ui` is a FastAPI app with seven tabs. It exposes F1, master, partial
  gains, spatial rotation, residual mix, sensors, Launchpad mirror, renderer
  selection, and a small WAV F0 analysis result.
- `nh-analysis` already contains useful primitives: pYIN F0, harmonicity,
  harmonic mask, spectral metrics, and Phideus descriptor functions.
- `nh-sensors` already has Muse/EEG, phone IMU, and simulator adapters.
- Legacy `digital_beacon/`, `beacon.scd`, and `tools/` still contain important
  behavior to migrate: Shaper voice lifecycle, envelopes, LFO, sidechain,
  Launchpad momentary/toggle semantics, voice-to-shaper analysis, field
  recording analysis, source normalization, and the SC/ATK beacon spatializer.

## Target Architecture

The canonical runtime object should become a multi-source harmonic scene, not a
single flattened field:

```text
HarmonicScene
  session: clock, f1, tempo/strum timing, transport
  sources:
    beacon: continuous harmonic field from file, live input, analysis, or manual bands
    shaper: triggered harmonic voice bank driven by Launchpad and other controls
    samples: nature/sample players with analysis metadata
    voice: live or file voice analysis/playback/synthesis source
  processors:
    harmonic_comb
    spatializer
    filters
    dynamics/limiter
  modulation:
    lfos
    sensors
    control mappings
  mix:
    source gains, bus routing, master gain
```

`HarmonicField` remains the reusable description of one harmonic field, but it
is no longer the whole application state. `base_field` can remain as a
temporary projected compatibility snapshot while renderers and the UI migrate,
but the source of truth should be a versioned `scene_snapshot`.

## Complexity Scale

- S: small, mostly data/API/test changes.
- M: moderate, a package-level feature with focused tests.
- L: large, cross-package contract and UI/runtime work.
- XL: high-risk audio/DSP work or broad end-to-end migration.

## Phase 0 - Contract Baseline And Fixtures

### What To Build

- Document current behavioral contracts as fixtures before changing them:
  - A flat-field preset that currently drones all partials.
  - A legacy digital-beacon v2 preset with separate `beacon` and `shaper`
    sections.
  - A beacon-spatial 13-band preset with `az`, `dist`, and `q`.
  - A short voice WAV and a short nature/field WAV from `data/uploads` or
    generated fixtures.
- Add a design note in the roadmap follow-up work that `Partial.spatial` must
  stop carrying non-spatial metadata such as `beacon_gain` or `active`.
- Decide the canonical JSON names for the new scene types before code work:
  `harmonic_scene`, `sources`, `processors`, `modulation`, `mix`.

No new package is needed in this phase.

### How To Verify

- `git status` stays clean except documentation/fixtures when implemented.
- Existing 97 tests remain green before any contract migration begins.
- Fixtures can be loaded by current `nh-presets` or intentionally marked as
  legacy input.

### Dependencies

- None.

### Estimated Complexity

S. This is primarily alignment and fixture preparation.

## Phase 1 - Scene Schema And Preset V2

### What To Build

- Extend `nh-core` with pure dataclasses for the new canonical state:
  - `HarmonicScene`
  - `SourceState`
  - `BeaconSource`
  - `ShaperSource`
  - `SampleSource`
  - `VoiceSource`
  - `ProcessingChain`
  - `ProcessorState`
  - `SpatialBand`
  - `LFOState`
  - `ModulationRoute`
- Keep `HarmonicField` as the per-source harmonic representation.
- Move all source identity into source objects. Do not encode source identity
  in `Partial.spatial` or `Partial.envelope`.
- Add `nh-presets` v2 schema:
  - `version: "2"`
  - `harmonic_scene`
  - optional `legacy_projection` metadata for migration diagnostics
- Migrate legacy digital-beacon v2 presets into separate scene sources:
  - `beacon` source gets the continuous field bands and beacon master.
  - `shaper` source gets voice templates, shaper master, envelopes, LFO,
    sidechain, phase, pan, shape, and inactive/active defaults.
- Migrate beacon-spatial presets into a `beacon` source with 13 `SpatialBand`
  entries.
- Add projection helpers:
  - scene -> flat `HarmonicField` for old renderers
  - scene -> digital-beacon v2 compatibility JSON if useful for comparison
- Update validation to check source IDs, source kinds, positive F1, valid
  harmonic indices, valid spatial ranges, and processor references.

No new package should be added. Put pure data contracts in `nh-core`; put
versioning, validation, migration, and projection in `nh-presets`.

### How To Verify

- Unit tests for scene round-trip JSON serialization.
- Migration tests prove a digital-beacon v2 preset preserves both beacon bands
  and shaper voice templates without overwriting one with the other.
- Migration tests prove a beacon-spatial preset preserves `az`, `dist`, and
  `q` per band.
- Projection tests prove `scene -> flat HarmonicField` is explicitly lossy and
  does not become the stored source of truth.
- Existing v1 preset tests continue passing through the v1 loader.

### Dependencies

- Phase 0 fixtures.

### Estimated Complexity

L. This changes core contracts but can stay pure and testable without touching
audio.

## Phase 2 - Runtime Model: Multi-Source Scene State

### What To Build

- Replace `ModelState` as the top-level source of truth with a scene-aware
  state object, for example `SceneState`.
- Keep focused sub-state objects:
  - `BeaconState`: continuous field, source gain, source transport, file/live
    source reference, per-band spatial values.
  - `ShaperState`: voice templates, active voices, envelopes, note-stealing
    state, master, sidechain, LFO params.
  - `SampleState`: loaded buffers/references, loop points, gain, playback
    status, analysis metadata.
  - `VoiceAnalysisState`: current file/stream analysis result, descriptor
    tracks, optional synthesis mapping.
- Add path-addressed controls:
  - `source_gain`
  - `source_enable`
  - `processor_param`
  - `spatial_param`
  - `shaper_voice_on`
  - `shaper_voice_off`
  - `shaper_voice_toggle`
  - `panic`
- Change Launchpad pad handling in the model:
  - `pad_on` creates or refreshes an active shaper voice for harmonic `n`.
  - `pad_off` releases that shaper voice.
  - `pad_toggle` latches or releases that shaper voice.
  - Beacon partial gains are not changed by pads.
- Add `scene_snapshot` WebSocket messages in `nh-runtime`.
- Keep `base_field` as a projected read-only compatibility broadcast only
  until renderers and UI are scene-native.
- Add source-aware sensor mapping in `nh-control`:
  - mappings target paths such as `sources.beacon.spatial.3.azimuth_deg` or
    `processors.harmonic_comb.wet`.
  - mappings can be scoped by `source_id`.

No new package is needed.

### How To Verify

- Unit tests prove loading a preset creates independent beacon and shaper
  sources.
- Unit tests prove pad events mutate only `ShaperState.active_voices`.
- Unit tests prove beacon partial gains remain unchanged after pad press,
  release, toggle, and panic.
- Runtime tests prove a client receives `renderer_capabilities`,
  `scene_snapshot`, and temporary `base_field` projection.
- Sensor mapping tests prove path-targeted routes update the intended source or
  processor and ignore disabled sources.

### Dependencies

- Phase 1 scene schema.

### Estimated Complexity

L. This is the main state-management migration and invalidates several current
flat-field assumptions.

## Phase 3 - Shaper Voice Lifecycle And Launchpad Semantics

### What To Build

- Port the useful behavior from legacy `digital_beacon.state` and
  `digital_beacon.audio_engine` into the new packages:
  - active voice tracking by harmonic `n`
  - per-voice frequency derived from current scene F1
  - attack/release envelopes
  - phase continuity
  - velocity-to-gain mapping
  - momentary and toggle pad behavior
  - note stealing when polyphony is exceeded
  - panic clearing active voices and Launchpad LED state
- Keep `nh-control.LaunchpadAdapter` responsible only for MIDI-to-control
  normalization and LED feedback metadata.
- Keep `nh-model` responsible for semantic voice state.
- Add shaper-specific control events rather than overloading
  `partial_gain`.
- Store shaper defaults in preset v2 voice templates, not in currently active
  voice state.

### How To Verify

- Unit tests:
  - momentary press creates one active voice.
  - release enters/reaches inactive state after envelope release.
  - toggle press latches voice and second press releases it.
  - velocity affects voice gain but not beacon gain.
  - note stealing releases the oldest toggled voice and records LED feedback.
- Runtime integration test with virtual MIDI:
  - load a scene with beacon and shaper.
  - send a Launchpad note.
  - assert `scene_snapshot.sources.shaper.active_voices[n]` exists.
  - assert `scene_snapshot.sources.beacon.field.partials[n].gain` is unchanged.
- UI mirror test proves Launchpad visual state follows hardware and web-origin
  controls.

### Dependencies

- Phase 2 scene-aware runtime model.

### Estimated Complexity

M. The behavior already exists in legacy code, but must be moved into clean
package boundaries.

## Phase 4 - Scene-Native Renderers And Multi-Source Mixdown

### What To Build

- Extend `nh-renderers.Renderer` with a scene-native render path:
  - `render_scene(scene: HarmonicScene, transport=None)`
  - keep `render(field)` as a compatibility adapter during migration.
- Python renderer becomes the correctness oracle for scene rendering:
  - continuous beacon source
  - triggered shaper source with envelopes
  - sample and voice sources as buffer players when buffers are available
  - source gains and master gain
  - peak-safe or bus-aware normalization that does not hide source routing bugs
- WebAudio worklet receives `scene_snapshot` payloads and implements the same
  source graph subset as Python.
- Add a buffer-loading interface for file-backed sources:
  - server stores/serves audio files.
  - WebAudio fetches/decodes buffers.
  - Python loads buffers through `soundfile`/`librosa` as appropriate.
- Define renderer capabilities for each feature:
  - `supports_scene`
  - `supports_sources`
  - `supports_envelopes`
  - `supports_buffers`
  - `supports_processors`
  - existing partial/spatial/residual flags remain for projection.

### How To Verify

- Python renderer tests:
  - beacon-only scene emits continuous audio.
  - shaper-only scene is silent with no active voices.
  - shaper voice press emits audio and release decays to silence.
  - beacon and shaper mixed together produce both spectral components.
  - nature/sample source plays expected fixture audio.
- WebAudio tests:
  - worklet accepts `scene_snapshot`.
  - no active shaper voice renders silence for shaper source.
  - active voice renders non-zero samples.
- End-to-end audio loopback test:
  - load a scene.
  - start Python renderer.
  - press Launchpad pad.
  - detect added shaper harmonic without losing beacon energy.
- Existing flat-field renderer tests remain through compatibility projection
  until removed in the final release phase.

### Dependencies

- Phase 2 scene snapshots.
- Phase 3 shaper active voice semantics.

### Estimated Complexity

XL. This is the first broad audio change and touches Python, WebAudio,
runtime, and tests.

## Phase 5 - Harmonic Comb, Spatialization, And Shared Processing

### What To Build

- Add processor contracts in `nh-core` and implementations in `nh-renderers`:
  - `HarmonicCombProcessor`
  - `BinauralSpatializer`
  - `FilterProcessor`
  - `DynamicsProcessor`
- Harmonic comb:
  - tuned by scene/session F1.
  - supports `n_harmonics`, per-band gain, bandwidth/Q, wet/dry, residual mix.
  - works on beacon, samples, and voice sources.
  - exposes deterministic offline processing helpers for tests.
- Spatializer:
  - restore the 13-band binaural spatializer semantics from beacon-spatial.
  - preserve `azimuth`, `distance`, and `q` per band.
  - support projection between 13-band and 32-band scenes.
  - define behavior for non-harmonic sources: source-level spatialization or
    analysis-derived harmonic-band spatialization.
- Shared routing:
  - sources render into a processor graph.
  - harmonic sources can be spatialized per partial before summing.
  - sample/voice sources can pass through comb and spatial processors.
- SC/ATK adapter:
  - update `nh_renderers.sc_osc` to map scene beacon parameters to existing
    `/beacon/*` OSC where possible.
  - keep SC/ATK as a high-quality compatibility renderer, not the canonical
    model.

### How To Verify

- Comb unit tests:
  - impulse or swept-sine response shows peaks at `n * f1`.
  - off-harmonic frequencies are attenuated according to Q/bandwidth.
  - `residual_mix` controls masked/unmasked energy.
- Spatial tests:
  - scene spatial params survive serialization and preset load.
  - azimuth changes alter left/right energy or HRTF output deterministically in
    Python reference tests.
  - distance changes reduce/directly transforms level according to the chosen
    distance law.
  - Q changes affect comb/filter bandwidth.
- SC adapter tests:
  - scene spatial values produce expected OSC messages for gain, azimuth,
    distance, Q, and on/off where supported.
- End-to-end test:
  - load a 13-band beacon-spatial preset.
  - verify UI values and renderer output respond to `az`, `dist`, and `q`
    changes.

### Dependencies

- Phase 4 scene-native renderer path.

### Estimated Complexity

XL. This is DSP-heavy and needs careful parity tests.

## Phase 6 - Full Audio And Voice Analysis Pipeline

### What To Build

- Promote the existing analysis primitives into a single `AnalysisResult`
  schema in `nh-analysis`:
  - file metadata: path/id, sample rate, channels, duration, loudness/peak.
  - F0 track and summary statistics.
  - voiced/unvoiced mask and confidence.
  - harmonicity and harmonic energy per partial.
  - spectral metrics: centroid, bandwidth, rolloff, flatness, flux, peak
    frequencies, band energy.
  - harmonic mask outputs and residual energy summaries.
  - Phideus descriptors: H-series, V4-linear/log, A4-16k.
  - optional emotion and speaker recognition outputs.
- Add provider interfaces for model-backed analysis:
  - `EmotionDetector`
  - `SpeakerRecognizer`
  - `VoiceEmbeddingExtractor`
  - deterministic fixture/stub implementations for CI.
  - optional local model implementations only when dependencies are installed.
- Convert `tools/voice_to_shaper.py`, `tools/voice_cache.py`, and relevant
  voice server logic into package-level analysis and cache utilities.
- Add `analyze_audio_file()` as the main orchestration API.
- Update `/nh/v1/analyze` to return the full `AnalysisResult`, not only mean
  F0.
- Store analysis sidecars next to uploaded files under `NH_UPLOAD_DIR` or a
  configured analysis cache.

No new package is required for analysis. Use `nh-analysis`.

### How To Verify

- Unit tests with synthetic sine, harmonic stack, noise, and short speech-like
  fixture:
  - F0 is near expected value on voiced synthetic audio.
  - harmonic energy peaks at expected partials.
  - harmonicity is high for harmonic tones and lower for noise.
  - spectral metrics are finite and stable.
  - Phideus descriptor shapes match expected dimensions.
- API tests:
  - `/nh/v1/analyze` returns a stable `AnalysisResult` schema.
  - missing optional emotion/speaker dependencies return
    `status: unavailable`, not a failed request.
- Cache tests:
  - repeated analysis of an unchanged file hits cache.
  - changed file invalidates cache by mtime and size.
- UI test:
  - analysis tab displays F0, harmonicity, spectral metrics, descriptor
    availability, emotion status, and speaker status.

### Dependencies

- Phase 1 schema for attaching analysis to sources.
- Can run partly in parallel with Phases 2-5 because most work is offline.

### Estimated Complexity

L. Most primitives already exist, but orchestration, schema, cache, and UI/API
surface are new.

## Phase 7 - Nature Samples And Field Recording Sources

### What To Build

- Add sample/media source support using existing packages first:
  - `nh-core`: `SampleSource` and source metadata contracts.
  - `nh-runtime`: upload, catalog, sidecar storage, and buffer references.
  - `nh-renderers`: buffer playback and loop/one-shot transport.
  - `nh-analysis`: field-recording analysis promoted from
    `tools/analyze_field_recordings.py`.
- Add field recording to beacon workflow:
  - analyze a field/nature WAV.
  - propose F1 and spectral landmarks.
  - generate a `BeaconSource` from harmonic peaks and residual/audio reference.
  - optionally generate `SpatialBand` defaults from detected harmonic bands.
- Add nature sample playback:
  - source gain, mute, loop points, playback rate where appropriate.
  - route through harmonic comb, spatializer, filters, and master bus.
- Add an optional `nh-media` package only if media catalog and cache code grows
  too large for `nh-runtime` plus `nh-analysis`. Do not create it before that
  pressure exists.

### How To Verify

- Unit tests:
  - sample source serializes and validates.
  - buffer player loops correctly and stops correctly.
  - analysis proposes a finite F1 for synthetic field recordings.
  - generated beacon source has expected partials and residual reference.
- Renderer tests:
  - sample source contributes audio to the mix.
  - changing sample source gain changes output level without changing beacon or
    shaper state.
  - sample source through comb changes spectrum as expected.
- API/UI tests:
  - upload nature WAV.
  - analyze it.
  - create beacon source from analysis.
  - load scene and see source values in the UI.

### Dependencies

- Phase 4 buffer-capable renderers.
- Phase 5 processing for comb/spatial routing.
- Phase 6 analysis schema for sidecars.

### Estimated Complexity

L. Playback is moderate; reliable analysis-to-source workflow is larger.

## Phase 8 - UI: Scene Inspector And Source Controls

### What To Build

- Move the UI from flat-field controls to scene-aware panels:
  - Sources: beacon, shaper, samples, voice.
  - Processing: harmonic comb, spatializer, filters, dynamics.
  - Spatial: per-band azimuth, distance, Q, on/off, projection mode.
  - Launchpad: active shaper voices, toggles, momentaries, note stealing.
  - Analysis: full `AnalysisResult` tables and summaries.
  - Presets: v1/v2 load, migration preview, source list, validation errors.
- When loading a preset, show all components and values:
  - beacon F1, source type, partial/band gains, spatial params, residual.
  - shaper voice templates, active voices, envelope defaults, shape, pan,
    phase, LFO amounts, sidechain, master.
  - sample/voice source transport, gain, analysis summary.
  - processors and modulation routes.
- Change performance controls:
  - master/source gains are source-aware.
  - partial sliders target selected source, not the global field.
  - Launchpad controls target shaper voice gates.
- Update WebAudio client to render from `scene_snapshot`.
- Keep seven current tabs only if they remain ergonomic; otherwise introduce a
  clear scene-first tab layout while preserving existing workflows.

### How To Verify

- Playwright tests:
  - v2 preset load displays beacon and shaper as separate sources.
  - shaper inactive templates are visible but do not sound.
  - pressing Launchpad pad marks a shaper voice active.
  - beacon gain/spatial values do not change after shaper pad events.
  - spatial values from a beacon-spatial preset are visible and editable.
  - analysis upload displays full metrics.
- API tests:
  - preset details endpoint returns v2 scene structure.
  - UI can save a scene snapshot and reload it without losing sources.
- Manual smoke test:
  - WebAudio start, load scene, press pad, adjust comb/spatial values, panic.

### Dependencies

- Phase 2 scene snapshots for data.
- Phase 3 Launchpad semantics for voice state.
- Phase 5 spatial/processor params for meaningful controls.
- Phase 6 analysis result for analysis display.
- Can begin as read-only scene inspector after Phase 2 before all audio
  features are complete.

### Estimated Complexity

L. The UI is a broad surface but can be shipped incrementally as read-only
inspection first, then controls.

## Phase 9 - LFOs, Field Modulation, And Sensor Routing

### What To Build

- Port legacy LFO behavior into `nh-model`:
  - waveforms: sine, triangle, saw, square, samplehold.
  - rate by Hz and by strum-period divisor.
  - global LFO amount and per-route depth.
  - per-voice routes for gain, pan, and phase.
- Add source/processor modulation routing:
  - path-targeted routes from LFO, EEG, IMU, Launchpad controls, and analysis
    envelopes.
  - smoothing and clamping at route level.
  - source enable/kill switches.
  - modulation values included in `scene_snapshot`.
- Integrate sensor safety with scene routing:
  - global influence.
  - per-source/per-sensor enable.
  - confidence thresholds.
- Add field modulation:
  - F1 offsets.
  - spatial rotation.
  - comb Q/wet modulation.
  - source gain modulation.
  - shaper voice parameter modulation.

### How To Verify

- Deterministic unit tests for every LFO waveform.
- Route tests:
  - one sensor event updates only the configured path.
  - disabled source receives no modulation.
  - smoothing converges over expected frames.
  - route clamping prevents invalid spatial/Q/gain values.
- Integration tests:
  - simulated IMU yaw rotates beacon spatial bands.
  - simulated EEG focus changes a configured source or master gain.
  - LFO gain modulation changes shaper voice level without mutating stored
    preset templates.
- UI tests:
  - modulation values appear in source/processor panels and sliders stay in
    sync with server broadcasts.

### Dependencies

- Phase 2 scene state.
- Phase 5 processor params.
- Phase 8 UI controls for full visibility, though model tests can start
  earlier.

### Estimated Complexity

M to L. The logic is moderate, but it touches many parameter paths.

## Phase 10 - Legacy Renderer Parity And Release Hardening

### What To Build

- Bring SC/ATK compatibility up to the new scene contracts:
  - scene-to-OSC mapping for beacon bands.
  - shaper scene mapping where SC is not responsible for voices.
  - explicit unsupported-feature reporting through capabilities.
- Add migration/removal plan for temporary flat-field compatibility:
  - mark `base_field` projection as legacy.
  - update tests to assert `scene_snapshot` first.
  - keep flat projection only for old clients if needed.
- Add cross-renderer parity checks:
  - Python reference.
  - WebAudio.
  - SC/ATK where available.
- Add performance targets:
  - update rate.
  - render CPU.
  - WebSocket payload size.
  - audio callback underrun checks.
- Update docs:
  - preset v2 schema.
  - WebSocket v2 messages.
  - source graph.
  - renderer capabilities.
  - migration notes from legacy digital-beacon and beacon-spatial.

### How To Verify

- Full test suite passes.
- End-to-end tests:
  - load v2 scene with beacon, shaper, sample, and voice source.
  - run Python renderer.
  - run WebAudio renderer.
  - trigger Launchpad shaper harmonics.
  - modify spatial and comb parameters.
  - run analysis and attach results to a source.
  - save and reload scene.
- Optional hardware smoke:
  - physical Launchpad controls active shaper voices and LEDs.
  - SC/ATK receives expected beacon OSC and remains binaural.
- Regression checks:
  - loading a preset does not make shaper partials drone.
  - beacon can drone continuously while shaper voices are silent.
  - beacon, shaper, nature samples, and voice can play together.

### Dependencies

- Phases 1-9.

### Estimated Complexity

M to L. Most feature work should be done; this phase is integration,
compatibility, documentation, and cleanup.

## Recommended Build Order

1. Phase 1: scene schema and preset v2.
2. Phase 2: scene-aware runtime state and `scene_snapshot`.
3. Phase 3: shaper voice lifecycle and Launchpad semantics.
4. Phase 4: scene-native Python renderer, then WebAudio.
5. Phase 8 read-only UI scene inspector can begin immediately after Phase 2
   and become editable as Phases 3-6 land.
6. Phase 5 spatial/comb processors.
7. Phase 6 full analysis pipeline.
8. Phase 7 nature/field sources.
9. Phase 9 modulation and sensors.
10. Phase 10 release hardening.

The critical path is Phases 1-4. Until those are complete, Launchpad pads will
continue to fight the beacon drone because the app has no clean representation
for "continuous source plus triggered source."

## Immediate Acceptance Criteria For The Next Implementation Slice

The first implementation slice should be considered complete only when all of
the following are true:

- A v2 preset can represent beacon and shaper independently.
- Loading a migrated digital-beacon v2 preset does not collapse shaper voices
  into beacon partials.
- `scene_snapshot` shows separate `beacon` and `shaper` sources.
- Pressing a Launchpad pad creates an active shaper voice.
- Releasing that pad releases the shaper voice.
- Beacon partial gains and spatial values are unchanged by the pad events.
- The temporary flat `base_field` projection is clearly marked as lossy
  compatibility, not the model source of truth.
