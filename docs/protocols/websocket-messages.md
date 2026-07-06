# WebSocket Message Protocol

Version: 1.1  
Scope: internal typed control plane between NaturalHarmony server and clients.

## Transport

- Bidirectional WebSocket over `wss?://host:port/nh/v1/ws`.
- Messages are JSON objects with `type` and `payload`.
- Timestamps are Unix seconds as floats.

## Server behavior

On client connect, the server sends:
1. `renderer_capabilities` (capabilities of the active renderer).
2. Then periodic `base_field` snapshots (default 10 Hz).

The server keeps a single `ModelState`. Clients send `control_event` and `sensor_event` messages; the server applies them to the model and broadcasts the resulting field snapshot as `base_field`.

## Message types

### `renderer_capabilities`

Server -> client. Sent once after handshake. Describes the active renderer.

```json
{
  "type": "renderer_capabilities",
  "payload": {
    "max_partials": 32,
    "supports_phase": true,
    "supports_spatial": true,
    "spatial_mode": "ambisonic",
    "supports_residual": false,
    "sample_rate": 48000,
    "block_size": 512
  }
}
```

### `base_field`

Server -> client. Periodic full snapshot of the modulated harmonic field. Contains all partials, not deltas.

```json
{
  "type": "base_field",
  "payload": {
    "f1": 65.0,
    "partials": {
      "1": {"n": 1, "gain": 1.0, "phase": 0.0, "pan": 0.0, "spatial": {"az": 0.0, "dist": 2.0, "on": true}}
    },
    "residual": {"kind": "none", "gain": 0.0, "mix": 1.0},
    "metadata": {},
    "transport": {"clock": 0.0, "playing": true}
  },
  "ts": 1234567890.0
}
```

### `control_event`

Client -> server. Normalized control input.

```json
{
  "type": "control_event",
  "payload": {
    "source": "launchpad",
    "type": "pad_on",
    "value": {"n": 5, "vel": 127},
    "timestamp": 1234567890.0
  }
}
```

Special control types consumed by the server model:
- `master`: value = float gain.
- `f1_offset`: value = float Hz offset.
- `partial_gain`: value = {"n": int, "gain": float}.
- `panic`: value ignored; resets all modulations to default.

### `sensor_event`

Client -> server. Normalized sensor stream.

See `sensor-event.md`. Applied to model via a mapping graph configured server-side.

### `ping` / `pong`

Keepalive.

```json
{"type": "ping", "payload": {}}
```

## Error handling

- On malformed message, server replies with `type: error` and `payload: {code, message}`.
- Client should reconnect on connection drop and wait for new `renderer_capabilities` + `base_field`.
- Server broadcasts full snapshots, so a reconnecting client does not need to request state.
