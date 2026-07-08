# NaturalHarmony Migration — v1 to v2

## What changed

v1 (flat HarmonicField) → v2 (multi-source HarmonicScene)

| v1                      | v2                                  |
|-------------------------|--------------------------------------|
| HarmonicField           | HarmonicScene                       |
| ModelState              | SceneState                          |
| partials dict (flat)    | Sources (beacon, shaper, sample, voice) |
| partial.spatial = metadata bag | Partial.spatial = SPATIAL ONLY |
| Preset v1               | PresetV2                            |
| /nh/v1/* endpoints       | /nh/v2/* endpoints                  |
| base_field snapshot      | scene_snapshot WebSocket            |
| Type-based controls      | Path-addressed controls + types     |

## Key contracts

1. **Partial.spatial is NEVER metadata.** Only az, dist, q, on, solo allowed.
   Transitional keys (beacon_gain, active) tolerated during migration only.

2. **Pad events only affect ShaperSource, never BeaconSource.**
   Pads = additive synth voices. Beacon = continuous drone.

3. **Sources are independent.** Muting shaper does NOT silence beacon.

4. **SceneState.scene_snapshot()** is the canonical WebSocket payload for v2.

## How to migrate

### Code
- Replace `ModelState()` → `SceneState(scene=HarmonicScene(...))`
- Replace `state.base_field` → `state.scene` (access sources directly)
- Replace `state.to_snapshot()` → `state.scene_snapshot()`
- Replace `apply_control({"type": "pad_on", ...})` → same (backward compat built in)
- New: path-addressed controls: `apply_control({"path": "sources.beacon.f1", "value": 55.0})`

### Presets
- v1 JSON files continue to load via `Preset`
- v2 JSON files use `PresetV2` with `"version": "2"` and `"scene"` key
- `migrate_v1_to_v2(HarmonicField)` → `HarmonicScene`
- `scene.project_to_base_field()` → `HarmonicField` (lossy)

### Renderers
- `renderer.render(field)` still works for v1
- `renderer.render_scene(scene)` for v2 (default: project to field)
- SC/ATK: use `scene_to_beacon_osc()` / `scene_to_shaper_osc()` adapters

## Deprecation timeline
- **Phase 10 (now):** `base_field` marked LEGACY. `ModelState` documented as legacy.
- **v3 (future):** Remove `base_field`, `ModelState`, and v1 preset format.

## Regression invariants
- Preset load does NOT collapse shaper into beacon drone.
- Beacon drones while shaper is silent.
- All sources play together without interference.
- 205 regression tests passing as of Phase 10.
