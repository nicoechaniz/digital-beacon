# WebSocket Message Protocol

Version: 1.0  
Scope: internal typed control plane between NaturalHarmony server and clients.

## Transport

- Bidirectional WebSocket over `wss?://host:port/nh/v1/ws`.
- Messages are JSON objects with `type` and `payload`.
- Timestamps are ISO 8601 strings in UTC or Unix seconds as floats.

## Message types

### `base_field`

Server -> client. Periodic update of the shared harmonic field.

```json
{
  "type": "base_field",
  "payload": {
    "f1": 65.0,
    "partials": [
      {"n": 1, "gain": 1.0, "phase": 0.0, "pan": 0.0}
    ],
    "residual_kind": "none",
    "descriptors": {},
    "transport": {"clock": 0.0, "playing": true}
  },
  "ts": 1234567890.0
}
```

### `session_state`

Server -> client or client -> server. Preset/session state changes.

```json
{
  "type": "session_state",
  "payload": {
    "session_id": "uuid",
    "preset_id": "preset-name",
    "preset_version": "1",
    "transport": {"clock": 0.0, "playing": true}
  }
}
```

### `control_event`

Client -> server or server -> client. Normalized control input.

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

### `sensor_event`

Client -> server. Normalized sensor stream.

See `sensor-event.md`.

### `renderer_capabilities`

Client -> server during handshake.

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

### `ping` / `pong`

Keepalive.

```json
{"type": "ping", "payload": {}}
```

## Error handling

- On malformed message, server replies with `type: error` and `payload: {code, message}`.
- Client should reconnect on connection drop and re-send `renderer_capabilities`.
