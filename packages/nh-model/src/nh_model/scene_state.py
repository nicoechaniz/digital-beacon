"""SceneState — multi-source runtime state for HarmonicScene v2.

Replaces the flat ModelState with per-source runtime modulations,
path-addressed controls, and scene-level snapshot.

The path grammar is:  sources.<source_id>.<param>
  or:                processors.<processor_id>.<param>
  or:                scene.<param>

Examples:
  sources.beacon.f1          → BeaconSource.f1
  sources.shaper.voice_1_on  → activate ShaperVoice n=1
  processors.comb_1.wet      → ProcessorState.params["wet"]
  scene.master_gain          → global master
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field as dc_field
from typing import Any, Dict, List, Optional, Tuple

from nh_core import (
    HarmonicField,
    HarmonicScene,
    BeaconSource,
    ShaperSource,
    ShaperVoice,
    SampleSource,
    VoiceSource,
    SpatialBand,
    ProcessorState,
    ProcessingChain,
    Partial,
)


@dataclass
class ActiveVoiceState:
    """Runtime state for a single active shaper voice."""
    n: int
    velocity: float = 1.0
    note_on: bool = False
    gate: bool = False
    envelope_phase: str = "idle"  # idle | attack | sustain | release
    envelope_value: float = 0.0
    phase_accum: float = 0.0
    started_at: float = 0.0  # clock time when note was triggered
    released_at: Optional[float] = None


@dataclass
class ShaperRuntime:
    """Runtime state for a ShaperSource — voice lifecycle, envelopes."""
    source_id: str
    active_voices: Dict[int, ActiveVoiceState] = dc_field(default_factory=dict)
    gain_offset: float = 1.0
    pan_offset: float = 0.0
    polyphony_mode: str = "steal"

    def voice_on(self, n: int, velocity: float = 1.0, clock: float = 0.0) -> ActiveVoiceState:
        """Activate a voice. Returns the voice state."""
        if n in self.active_voices:
            # Voice already active — re-attack.
            voice = self.active_voices[n]
            voice.velocity = velocity
            voice.note_on = True
            voice.gate = True
            voice.envelope_phase = "attack"
            voice.envelope_value = 0.0
            voice.phase_accum = 0.0
            voice.started_at = clock
            voice.released_at = None
            return voice

        # Note stealing if at max voices.
        if self.polyphony_mode == "steal" and len(self.active_voices) >= 32:
            # Steal the oldest voice.
            oldest_n = min(self.active_voices.keys(),
                          key=lambda k: self.active_voices[k].started_at)
            del self.active_voices[oldest_n]

        voice = ActiveVoiceState(
            n=n, velocity=velocity, note_on=True, gate=True,
            envelope_phase="attack", started_at=clock,
        )
        self.active_voices[n] = voice
        return voice

    def voice_off(self, n: int, clock: float = 0.0) -> None:
        """Release a voice (enter release phase)."""
        if n in self.active_voices:
            voice = self.active_voices[n]
            voice.note_on = False
            voice.gate = False
            voice.envelope_phase = "release"
            voice.released_at = clock

    def voice_toggle(self, n: int, velocity: float = 1.0, clock: float = 0.0) -> bool:
        """Toggle voice n. Returns True if voice is now active."""
        if n in self.active_voices and self.active_voices[n].gate:
            self.voice_off(n, clock)
            return False
        else:
            self.voice_on(n, velocity, clock)
            return True

    def panic(self) -> None:
        """Silence all voices immediately."""
        self.active_voices.clear()

    def cleanup_released(self, max_age_s: float = 5.0, clock: float = 0.0) -> None:
        """Remove voices that have been in release phase for too long."""
        to_remove = []
        for n, v in self.active_voices.items():
            if v.envelope_phase == "release" and v.released_at is not None:
                if clock - v.released_at > max_age_s:
                    to_remove.append(n)
        for n in to_remove:
            del self.active_voices[n]

    def voice_count(self) -> int:
        return len(self.active_voices)

    def advance_envelopes(self, dt: float, clock: float = 0.0) -> None:
        """Advance envelope phases for all active voices by dt seconds.

        Default envelope times (can be overridden per voice from the scene):
          attack: 0.01s, release: 0.15s, shape: 0.0 (linear)
        """
        for voice in list(self.active_voices.values()):
            # Get envelope params from the scene voice definition.
            env = self._voice_envelope(voice.n)
            attack_s = env.get("attack_s", 0.01)
            release_s = env.get("release_s", 0.15)
            shape = env.get("shape", 0.0)  # 0=linear, 1=exponential

            if voice.envelope_phase == "attack":
                elapsed = clock - voice.started_at
                if attack_s > 0:
                    t = min(elapsed / attack_s, 1.0)
                    if shape > 0:
                        voice.envelope_value = t ** (1.0 - shape)  # exponential
                    else:
                        voice.envelope_value = t  # linear
                else:
                    voice.envelope_value = 1.0
                if elapsed >= attack_s:
                    voice.envelope_phase = "sustain"
                    voice.envelope_value = 1.0

            elif voice.envelope_phase == "sustain":
                voice.envelope_value = 1.0

            elif voice.envelope_phase == "release":
                if voice.released_at is not None:
                    elapsed = clock - voice.released_at
                    if release_s > 0:
                        t = max(0.0, 1.0 - elapsed / release_s)
                        if shape > 0:
                            voice.envelope_value = t ** (1.0 / (1.0 - shape + 0.001))
                        else:
                            voice.envelope_value = t
                    else:
                        voice.envelope_value = 0.0
                if voice.envelope_value <= 0.001:
                    voice.envelope_value = 0.0

            # Advance phase accumulator for phase-continuous oscillators.
            if voice.gate:
                # Phase accumulation from source f1 * n.
                voice.phase_accum += dt * 0.0  # filled by renderer at sample rate

    def get_voice_gain(self, voice: ActiveVoiceState, base_gain: float = 1.0) -> float:
        """Compute effective gain: base_gain * velocity * envelope * source offset."""
        return base_gain * voice.velocity * voice.envelope_value * self.gain_offset

    def _voice_envelope(self, n: int) -> Dict[str, Any]:
        """Get envelope params for voice n from the scene, or defaults."""
        # The scene envelope is stored on ShaperVoice; this is accessed
        # through the SceneState's scene. We store a cached reference.
        if not hasattr(self, "_env_cache"):
            self._env_cache: Dict[int, Dict[str, Any]] = {}
        if n not in self._env_cache:
            self._env_cache[n] = {"attack_s": 0.01, "release_s": 0.15, "shape": 0.0}
        return self._env_cache[n]

    def sync_envelopes_from_scene(self, voices: Dict[int, Any]) -> None:
        """Sync envelope parameters from a ShaperSource's voice definitions."""
        self._env_cache = {}
        for n, voice in voices.items():
            if hasattr(voice, "envelope") and voice.envelope:
                self._env_cache[n] = dict(voice.envelope)
            else:
                self._env_cache[n] = {"attack_s": 0.01, "release_s": 0.15, "shape": 0.0}


@dataclass
class BeaconRuntime:
    """Runtime state for a BeaconSource."""
    source_id: str
    f1_offset: float = 0.0
    gain_offset: float = 1.0
    spatial_rotation: float = 0.0
    vsrate: float = 1.0


@dataclass
class SampleRuntime:
    """Runtime state for a SampleSource."""
    source_id: str
    playing: bool = False
    position: float = 0.0
    gain_offset: float = 1.0
    loop: bool = False


@dataclass
class ProcessorRuntime:
    """Runtime state for a ProcessorState."""
    processor_id: str
    param_overrides: Dict[str, float] = dc_field(default_factory=dict)


@dataclass
class SceneState:
    """Multi-source runtime state wrapping a HarmonicScene.

    Provides path-addressed control, per-source modulations, and a
    scene_snapshot that merges the scene model with runtime state.

    base_field is a v1 compatibility projection.
    """
    scene: HarmonicScene = dc_field(default_factory=HarmonicScene)

    # Per-source runtime.
    beacons: Dict[str, BeaconRuntime] = dc_field(default_factory=dict)
    shapers: Dict[str, ShaperRuntime] = dc_field(default_factory=dict)
    samples: Dict[str, SampleRuntime] = dc_field(default_factory=dict)

    # Per-processor runtime.
    processors: Dict[str, ProcessorRuntime] = dc_field(default_factory=dict)

    # Global.
    master_gain: float = 0.6
    sensor_influence: float = 1.0
    sensor_sources: Dict[str, bool] = dc_field(default_factory=dict)

    def __post_init__(self):
        """Ensure runtime dicts are populated from the scene."""
        for sid, source in self.scene.sources.items():
            if isinstance(source, BeaconSource):
                if sid not in self.beacons:
                    self.beacons[sid] = BeaconRuntime(source_id=sid, vsrate=source.vsrate)
            elif isinstance(source, ShaperSource):
                if sid not in self.shapers:
                    self.shapers[sid] = ShaperRuntime(source_id=sid)
            elif isinstance(source, SampleSource):
                if sid not in self.samples:
                    self.samples[sid] = SampleRuntime(source_id=sid, loop=source.loop)

        for proc in self.scene.processing_chain.processors:
            if proc.processor_id not in self.processors:
                self.processors[proc.processor_id] = ProcessorRuntime(
                    processor_id=proc.processor_id
                )

    # ── Path-addressed control ────────────────────────────────────────────

    def _parse_path(self, path: str) -> Optional[Tuple[str, str, str]]:
        """Parse 'section.target.param' into (section, target, param)."""
        parts = path.split(".")
        if len(parts) < 2:
            return None
        section = parts[0]
        if section == "sources" and len(parts) >= 3:
            return ("sources", parts[1], ".".join(parts[2:]))
        elif section == "processors" and len(parts) >= 3:
            return ("processors", parts[1], ".".join(parts[2:]))
        elif section == "scene" and len(parts) >= 2:
            return ("scene", "", ".".join(parts[1:]))
        return None

    def apply_control(self, event: Dict[str, Any]) -> None:
        """Apply a normalized control event.

        Events use path-addressed or type-based dispatch. Pad events
        (pad_on/pad_off/pad_toggle) are routed ONLY to ShaperSource,
        never to BeaconSource.

        Path-based events: {"path": "sources.shaper.gain", "value": 0.5}
        Type-based events: {"type": "master", "value": 0.5}
        """
        path = event.get("path")
        if path:
            self._apply_path_control(path, event.get("value", 0.0))
            return

        etype = event.get("type")
        value = event.get("value", 0.0)

        if etype == "master":
            self.master_gain = float(value)
        elif etype == "sensor_influence":
            self.sensor_influence = float(value)
        elif etype == "panic":
            self.panic()
        elif etype in ("pad_on", "pad_off", "pad_toggle"):
            self._apply_pad_event(etype, value)
        elif etype == "beacon_f1":
            # Route to all beacon sources.
            v = value if isinstance(value, dict) else {}
            sid = v.get("source_id", "beacon")
            if sid in self.beacons:
                self.beacons[sid].f1_offset = float(v.get("offset", 0.0))

    def _apply_path_control(self, path: str, value: Any) -> None:
        """Apply a path-addressed control."""
        parsed = self._parse_path(path)
        if parsed is None:
            return

        section, target, param = parsed
        val = float(value) if isinstance(value, (int, float)) else value

        if section == "sources":
            if target in self.beacons:
                br = self.beacons[target]
                if param == "f1_offset":
                    br.f1_offset = float(val)
                elif param == "gain":
                    br.gain_offset = float(val)
                elif param == "spatial_rotation":
                    br.spatial_rotation = float(val)
                elif param.startswith("bands."):
                    self._apply_beacon_band_path(target, param, val)

            elif target in self.shapers:
                sr = self.shapers[target]
                if param == "gain":
                    sr.gain_offset = float(val)
                elif param == "pan":
                    sr.pan_offset = float(val)
                elif param.startswith("voice_"):
                    self._apply_shaper_voice_path(sr, param, val)

            elif target in self.samples:
                sm = self.samples[target]
                if param == "gain":
                    sm.gain_offset = float(val)
                elif param == "play":
                    sm.playing = bool(val)
                elif param == "position":
                    sm.position = float(val)

        elif section == "processors":
            if target in self.processors:
                self.processors[target].param_overrides[param] = float(val)

        elif section == "scene":
            if param == "master_gain":
                self.master_gain = float(val)
            elif param == "sensor_influence":
                self.sensor_influence = float(val)

    def _apply_shaper_voice_path(self, sr: ShaperRuntime, param: str, val: Any) -> None:
        """Handle voice_N_on / voice_N_off / voice_N_toggle."""
        # param format: voice_<N>_<action>
        parts = param.split("_")
        if len(parts) >= 3 and parts[0] == "voice":
            try:
                n = int(parts[1])
            except ValueError:
                return
            action = "_".join(parts[2:])
            if action == "on":
                sr.voice_on(n, float(val) if isinstance(val, (int, float)) else 1.0)
            elif action == "off":
                sr.voice_off(n)
            elif action == "toggle":
                sr.voice_toggle(n)

    def _apply_beacon_band_path(self, source_id: str, param: str, val: Any) -> None:
        """Handle bands.<N>.az / dist / q / on for BeaconSource."""
        source = self.scene.sources.get(source_id)
        if not isinstance(source, BeaconSource):
            return
        parts = param.split(".")
        if len(parts) != 3 or parts[0] != "bands":
            return
        try:
            n = int(parts[1])
        except ValueError:
            return
        band = source.bands.get(n)
        if band is None:
            return
        field = parts[2]
        if field == "az":
            band.az = float(val)
        elif field == "dist":
            band.dist = float(val)
        elif field == "q":
            band.q = float(val)
        elif field == "on":
            band.on = bool(val)

    def _apply_pad_event(self, etype: str, value: Any) -> None:
        """Pad events only affect ShaperSource, never BeaconSource."""
        v = value if isinstance(value, dict) else {}
        n = int(v.get("n", 0) or 0)
        if n <= 0:
            return

        # Route to the default shaper source.
        for sr in self.shapers.values():
            if etype == "pad_toggle":
                sr.voice_toggle(n, float(v.get("vel", 127)) / 127.0)
            elif etype == "pad_on":
                sr.voice_on(n, float(v.get("vel", 127)) / 127.0)
            else:  # pad_off
                sr.voice_off(n)

    # ── Snapshot ──────────────────────────────────────────────────────────

    def scene_snapshot(self) -> Dict[str, Any]:
        """Return a renderer-ready snapshot merging scene model + runtime state.

        This is the WebSocket-friendly scene_snapshot payload.
        """
        sources_snap = {}

        for sid, source in self.scene.sources.items():
            src_dict = source.to_dict()

            if sid in self.beacons:
                br = self.beacons[sid]
                src_dict["runtime"] = {
                    "f1_offset": br.f1_offset,
                    "gain_offset": br.gain_offset,
                    "spatial_rotation": br.spatial_rotation,
                    "effective_f1": source.f1 + br.f1_offset
                    if isinstance(source, BeaconSource) else None,
                }
            elif sid in self.shapers:
                sr = self.shapers[sid]
                src_dict["runtime"] = {
                    "gain_offset": sr.gain_offset,
                    "pan_offset": sr.pan_offset,
                    "active_voices": {
                        str(n): {
                            "velocity": v.velocity,
                            "gate": v.gate,
                            "envelope_phase": v.envelope_phase,
                            "envelope_value": v.envelope_value,
                        }
                        for n, v in sr.active_voices.items()
                    },
                    "voice_count": sr.voice_count(),
                }
            elif sid in self.samples:
                sm = self.samples[sid]
                src_dict["runtime"] = {
                    "playing": sm.playing,
                    "position": sm.position,
                    "gain_offset": sm.gain_offset,
                }

            sources_snap[sid] = src_dict

        processors_snap = []
        for proc in self.scene.processing_chain.processors:
            pd = proc.to_dict()
            if proc.processor_id in self.processors:
                pr = self.processors[proc.processor_id]
                pd["param_overrides"] = pr.param_overrides
            processors_snap.append(pd)

        return {
            "version": self.scene.version,
            "sources": sources_snap,
            "processing_chain": {
                "processors": processors_snap,
                "routing": self.scene.processing_chain.routing,
            },
            "modulations": {
                rid: r.to_dict() for rid, r in self.scene.modulations.items()
            },
            "master_gain": self.master_gain,
            "sensor_influence": self.sensor_influence,
        }

    # ── base_field compat ─────────────────────────────────────────────────

    def to_base_field(self) -> HarmonicField:
        """Lossy projection to v1 HarmonicField for legacy renderers.

        Applies runtime offsets (f1, gain) so the legacy audio path reflects the
        current scene state controlled by the v2 UI.
        """
        field = self.scene.project_to_base_field()
        # Apply beacon runtime offsets. Multiple beacons would need per-partial
        # attribution; for the current single-beacon scenes this is correct.
        for br in self.beacons.values():
            field.f1 += br.f1_offset
            for partial in field.partials.values():
                partial.gain *= br.gain_offset
        return field

    # ── Global controls ───────────────────────────────────────────────────

    def panic(self) -> None:
        """Silence all sources."""
        for sr in self.shapers.values():
            sr.panic()
        for sm in self.samples.values():
            sm.playing = False
        for br in self.beacons.values():
            br.gain_offset = 0.0

    def reset_modulations(self) -> None:
        """Reset all runtime modulations to defaults."""
        self.master_gain = 0.6
        self.sensor_influence = 1.0
        for br in self.beacons.values():
            br.f1_offset = 0.0
            br.gain_offset = 1.0
            br.spatial_rotation = 0.0
        for sr in self.shapers.values():
            sr.gain_offset = 1.0
            sr.pan_offset = 0.0
        for sm in self.samples.values():
            sm.gain_offset = 1.0
            sm.playing = False
        for pr in self.processors.values():
            pr.param_overrides.clear()

    # ── Serialization ─────────────────────────────────────────────────────

    def to_dict(self) -> Dict[str, Any]:
        return {
            "scene": self.scene.to_dict(),
            "master_gain": self.master_gain,
            "sensor_influence": self.sensor_influence,
            "sensor_sources": self.sensor_sources,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "SceneState":
        scene = HarmonicScene.from_dict(d.get("scene", {}))
        return cls(
            scene=scene,
            master_gain=d.get("master_gain", 0.6),
            sensor_influence=d.get("sensor_influence", 1.0),
            sensor_sources=d.get("sensor_sources", {}),
        )

