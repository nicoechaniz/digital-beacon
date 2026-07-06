# Sensor Event Specification

Version: 1.0  
Scope: normalized sensor input stream for the NaturalHarmony toolkit.

## Goals

- Decouple sensor hardware (Muse EEG, phone IMU, etc.) from the harmonic model.
- Provide enough metadata for the mapping layer to scale, calibrate and smooth signals safely.

## Event schema

```json
{
  "timestamp": 1234567890.0,
  "type": "eeg_focus",
  "value": 0.75,
  "confidence": 0.92,
  "rate": 10.0,
  "units": "normalized_0_1",
  "calibration": {
    "baseline": 0.5,
    "scale": 1.0,
    "offset": 0.0
  }
}
```

## Fields

| Field | Type | Description |
|---|---|---|
| `timestamp` | float | Unix seconds, monotonic source clock. |
| `type` | string | Event type, namespaced by source. |
| `value` | number / object | Scalar, vector or struct. |
| `confidence` | float [0,1] | Signal quality or confidence. |
| `rate` | float | Nominal sample rate in Hz. |
| `units` | string | Semantic unit: `normalized_0_1`, `degrees`, `g`, `uv_rms`, etc. |
| `calibration` | object | Baseline, scale, offset applied to raw sensor value. |

## Standard event types

### EEG

- `eeg.band_power.delta`
- `eeg.band_power.theta`
- `eeg.band_power.alpha`
- `eeg.band_power.smr`
- `eeg.band_power.beta`
- `eeg.band_power.gamma`
- `eeg.focus` (composite 0-100 or 0-1)
- `eeg.signal_quality`

### IMU / phone

- `imu.orientation.yaw`
- `imu.orientation.pitch`
- `imu.orientation.roll`
- `imu.acceleration.x`
- `imu.acceleration.y`
- `imu.acceleration.z`
- `imu.motion_energy`

### Simulator

- `simulator.focus`
- `simulator.orientation`

## Mapping to model parameters

The `MappingLayer` (nh-control) binds event types to model parameters declaratively:

```json
{
  "eeg_focus": {"param": "master_gain", "scale": 0.2, "offset": 0.0, "smooth_s": 0.5}
}
```

## Source adapters

- `MuseOSCAdapter` (nh-sensors) emits EEG events from Mind Monitor OSC.
- `PhoneIMUAdapter` (nh-sensors) emits orientation/acceleration events from DeviceMotion.
- Simulators produce deterministic events for testing.
