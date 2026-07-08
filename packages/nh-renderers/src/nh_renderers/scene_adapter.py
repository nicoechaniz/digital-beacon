"""SC/ATK OSC adapter — maps a HarmonicScene to SuperCollider OSC commands.

Legacy renderer parity (Phase 10): this adapter provides backwards
compatibility so existing SC engines can consume v2 scene snapshots
by projecting them to the flat /beacon/* and /shaper/* OSC namespace.
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

from nh_core import HarmonicScene, BeaconSource, ShaperSource, SampleSource


def scene_to_beacon_osc(scene: HarmonicScene) -> List[Tuple[str, List[Any]]]:
    """Generate OSC commands for the beacon engine from a scene.

    Returns list of (address, args) tuples for OSC dispatch.
    """
    commands: List[Tuple[str, List[Any]]] = []
    beacons = [s for s in scene.sources.values() if isinstance(s, BeaconSource)]

    for beacon in beacons:
        sid = beacon.source_id

        # Fundamental frequency + varispeed.
        commands.append((f"/beacon/{sid}/f1", [beacon.f1]))
        commands.append((f"/beacon/{sid}/vsrate", [beacon.vsrate]))
        commands.append((f"/beacon/{sid}/master", [beacon.master_gain]))

        # Per-band parameters.
        for n, band in beacon.bands.items():
            base = f"/beacon/{sid}/band/{n}"
            commands.append((f"{base}/gain", [band.on * beacon.master_gain if band.on else 0.0]))
            commands.append((f"{base}/az", [band.az]))
            commands.append((f"{base}/dist", [band.dist]))
            commands.append((f"{base}/q", [band.q]))
            commands.append((f"{base}/on", [int(band.on)]))

    return commands


def scene_to_shaper_osc(scene: HarmonicScene) -> List[Tuple[str, List[Any]]]:
    """Generate OSC commands for the shaper engine from a scene.

    Only sends active voices; inactive voices get /voice/N/off.
    """
    commands: List[Tuple[str, List[Any]]] = []
    shapers = [s for s in scene.sources.values() if isinstance(s, ShaperSource)]

    for shaper in shapers:
        sid = shaper.source_id
        commands.append((f"/shaper/{sid}/master_gain", [shaper.master_gain]))

        for n, voice in shaper.voices.items():
            base = f"/shaper/{sid}/voice/{n}"
            if voice.active:
                commands.append((f"{base}/gain", [voice.gain]))
                commands.append((f"{base}/pan", [voice.pan]))
                commands.append((f"{base}/phase", [voice.phase]))
                if voice.envelope:
                    commands.append((f"{base}/attack", [voice.envelope.get("attack_s", 0.01)]))
                    commands.append((f"{base}/release", [voice.envelope.get("release_s", 0.15)]))
                commands.append((f"{base}/on", [1]))
            else:
                commands.append((f"{base}/off", [1]))

    return commands


def scene_to_sample_osc(scene: HarmonicScene) -> List[Tuple[str, List[Any]]]:
    """Generate OSC commands for sample/buffer playback."""
    commands: List[Tuple[str, List[Any]]] = []
    samples = [s for s in scene.sources.values() if isinstance(s, SampleSource)]

    for sample in samples:
        sid = sample.source_id
        commands.append((f"/sample/{sid}/path", [sample.audio_path]))
        commands.append((f"/sample/{sid}/gain", [sample.gain]))
        commands.append((f"/sample/{sid}/loop", [int(sample.loop)]))
        commands.append((f"/sample/{sid}/oneshot", [int(sample.one_shot)]))

    return commands


def scene_to_all_osc(scene: HarmonicScene) -> List[Tuple[str, List[Any]]]:
    """Generate ALL OSC commands for the full scene.

    Suitable for initial preset load or full state reset.
    """
    commands: List[Tuple[str, List[Any]]] = []
    commands.extend(scene_to_beacon_osc(scene))
    commands.extend(scene_to_shaper_osc(scene))
    commands.extend(scene_to_sample_osc(scene))

    # Global panic/transport.
    commands.append(("/global/panic", [0]))  # 0 = reset, 1 = trigger
    return commands
