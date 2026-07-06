# OSC Bridge Mapping

Version: 1.0  
Scope: compatibility layer for legacy OSC devices and renderers.

## Principle

OSC is an **edge/gateway** protocol only. The canonical internal control plane is WebSocket/JSON. `nh-osc-bridge` translates between legacy OSC address spaces and the canonical message types defined in `websocket-messages.md`.

## Legacy namespaces

| Source | OSC prefix | Notes |
|---|---|---|
| NaturalHarmony | `/fnote`, `/fnote/rel`, `/allnotesoff`, `/ne/pitch` | Surge XT / MPE output. |
| beacon-spatial | `/beacon/gain/<n>`, `/beacon/azimuth/<n>`, `/beacon/distance/<n>`, `/beacon/q/<n>`, `/beacon/solo/<n>`, `/beacon/mix`, `/beacon/master`, `/beacon/record/*`, `/beacon/reset` | 13-band spatializer. |
| digital-beacon | `/beacon/f1`, `/beacon/vsource`, `/beacon/gain/<n>`, `/beacon/az/<n>`, `/beacon/on/<n>`, `/beacon/master`, `/beacon/panic`, `/beacon/reset`, `/beacon/record/*`, `/digital/harmonic/<n>/gain`, `/digital/harmonic/<n>/pan`, `/digital/harmonic/<n>/phase`, `/digital/master`, `/digital/panic` | 32-band + Shaper. |
| harmonic-beacon-tines | `/beacon/play`, `/beacon/stop`, `/beacon/stopall`, `/beacon/phase`, `/beacon/duty`, `/beacon/freq`, `/beacon/fundamental`, `/beacon/drift` | Physical tines (out of scope but mapped). |
| EEG-Game | `/muse/eeg`, `/muse/elements/*` | Mind Monitor EEG stream. |

## Mapping table (legacy -> canonical)

### Note / voice on

- `/fnote freq velocity [noteID]` -> `control_event` {source: "naturalharmony", type: "note_on", value: {freq, velocity, noteID}}
- `/beacon/voice/on idx freq` -> `control_event` {source: "digital-beacon", type: "voice_on", value: {idx, freq}}
- `/beacon/play idx vel dur` -> `control_event` {source: "tines", type: "voice_on", value: {idx, vel, dur}}

### F1 / fundamental

- `/beacon/f1 f` -> `session_state` {f1: f}
- `/beacon/fundamental f` -> `session_state` {f1: f}
- `/beacon/vsource vsrate` -> `session_state` {vsrate: vsrate}

### Gain / level

- `/beacon/gain/<n> g` -> `base_field` update partial n gain
- `/beacon/master m` -> `control_event` {type: "master", value: m}
- `/digital/master m` -> `control_event` {type: "master", value: m}
- `/digital/harmonic/<n>/gain g` -> `base_field` update partial n gain

### Spatial

- `/beacon/az/<n> a` -> `base_field` update partial n spatial.az
- `/beacon/azimuth/<n> a` -> `base_field` update partial n spatial.az
- `/beacon/distance/<n> d` -> `base_field` update partial n spatial.dist
- `/beacon/q/<n> q` -> `base_field` update partial n spatial.q

### Pan / phase

- `/digital/harmonic/<n>/pan p` -> `base_field` update partial n pan
- `/digital/harmonic/<n>/phase p` -> `base_field` update partial n phase

### Panic / reset

- `/beacon/panic`, `/allnotesoff`, `/digital/panic` -> `control_event` {type: "panic"}
- `/beacon/reset` -> `control_event` {type: "reset"}

### Sensors

- `/muse/eeg tp9 af7 af8 tp10` -> `sensor_event` {type: "eeg.raw", value: {tp9, af7, af8, tp10}}
- `/muse/elements/alpha_absolute a b c d` -> `sensor_event` {type: "eeg.band_power.alpha", value: {a, b, c, d}}

## Directionality

- Incoming legacy OSC is parsed into canonical WebSocket messages and forwarded to the server runtime.
- Outgoing canonical messages are translated to the appropriate OSC dialect when driving legacy hardware (SC, tines, MIDI bridges).
- No component other than the bridge should speak raw legacy OSC.
