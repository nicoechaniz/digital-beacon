# digital-beacon

**The Harmonic Beacon as a digital instrument.** Natural harmonics, end-to-end.

Sister / successor of `~/Projects/beacon-spatial` (13-band ATK binaural
spatializer) and `~/Projects/NaturalHarmony` (MIDI middleware +
visualizer + Shaper additive synth). `digital-beacon` is the **unified
instrument**: a 32-band binaural spatializer whose band centers are
exactly the natural harmonic series of `f1` (default 40 Hz), driven by
Launchpad Mini + a pure-sine additive synth played *on top* of the
spatialized field.

## Architecture

```
Launchpad Mini (MIDI)  ──────►  NaturalHarmony  ── /beacon/voice/* :9001 ──┐
   pad n (1..64)                   key_mapper                               │
   CC74 (f1)                       f1 = 40 Hz default                       │
   CC22 (stacking)                 chromatic prototypes                     │
   CC104 (split)                                                           │
                                                                           ▼
                                                          ┌────────────────────────────┐
                                                          │  digital-beacon (this repo)│
                                                          │                            │
                                                          │  beacon.scd (32 BPF 1:1)   │
                                                          │    FoaPanB                 │
                                                          │    FoaDecode(Listen HRTF)  │
                                                          │    BufRateScale ← vsrate   │
                                                          │                            │
                                                          │  digital_beacon/           │
                                                          │    state.py (32 voices)    │
                                                          │    audio_engine.py (sines) │
                                                          │    osc_receiver.py         │
                                                          │    midi_control.py         │
                                                          │    config.py               │
                                                          │                            │
                                                          │  f1_bridge.py              │
                                                          │    /beacon/f1 → vsrate     │
                                                          │    → /beacon/vsource       │
                                                          └────────────────────────────┘
                                                                           │
                                                                           ▼
                                                                headphones (HRTF)
                                                                + Shaper sines overlaid
```

## What this is NOT (yet)

- No EEG integration (planned, slot in `config.py`)
- No mobile sensor layer (planned)
- No Surge XT (Shaper IS the synth)
- No MPE (only OSC + WebSocket)
- No continuous f1 modulation (12 discrete points TODO)

## Stack

- `scsynth` + `sclang` + ATK (Ambisonic Toolkit) — binaural engine
- `pw-jack` (PipeWire) — audio backend
- `sounddevice` (PortAudio) — Shaper audio engine
- `python-osc` — control bus
- `mido` — Launchpad Mini + Minilab3 MIDI
- `fastapi` + WebSocket — optional web control surface

## How to run

```bash
cd ~/Projects/digital-beacon
./start.sh --file                  # beacon + shaper + bridge, source = WAV
./start.sh --live                  # beacon source = SoundIn(0) [R24 CH1]
```

## Default frequency

- `f1 = 40.0 Hz` (NOT fixed, see `config.F1_MOD_POINTS` for future
  12-point discrete modulation slot)
- Band N center frequency = `f1 * N` for N = 1..32
- Band 32 = 40 * 32 = 1280 Hz

## License

GPL-3.0-or-later (matches NaturalHarmony).
