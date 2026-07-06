from __future__ import annotations

import json
from typing import Any, Dict

from nh_core import HarmonicField, Partial, RendererCapabilities, Residual, Transport


def _read_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def migrate_digital_beacon_v1(path: str) -> HarmonicField:
    """Migrate digital-beacon v1 format: {bands, master, migrated_from}."""
    data = _read_json(path)
    bands = data.get("bands", [])
    if not bands:
        return HarmonicField(f1=65.0)

    f1 = 65.0
    first_band = bands[0]
    if "freq" in first_band and first_band["freq"]:
        f1 = first_band["freq"] / first_band.get("n", 1)

    field = HarmonicField(f1=f1)
    for b in bands:
        n = b.get("n")
        if n is None:
            continue
        field.partials[n] = Partial(
            n=n,
            gain=b.get("gain", 1.0),
            spatial={
                "az": b.get("az", 0.0),
                "dist": b.get("dist", 1.0),
                "q": b.get("q", 0.5),
                "on": bool(b.get("on", 1)),
            },
        )
    return field


def migrate_digital_beacon_v2(path: str) -> HarmonicField:
    """Migrate digital-beacon v2 format: {version, saved_at, beacon, shaper}."""
    data = _read_json(path)
    beacon = data.get("beacon", {})
    f1 = beacon.get("f1") or 65.0
    vsrate = beacon.get("vsrate", 1)
    if f1 and vsrate:
        effective_f1 = f1 * vsrate
    else:
        effective_f1 = f1

    field = HarmonicField(f1=effective_f1)
    for b in beacon.get("bands", []):
        n = b.get("n")
        if n is None:
            continue
        field.partials[n] = Partial(
            n=n,
            gain=b.get("gain", 1.0),
            spatial={
                "az": b.get("az", 0.0),
                "dist": b.get("dist", 1.0),
                "q": b.get("q", 0.5),
                "on": bool(b.get("on", 1)),
            },
        )

    shaper = data.get("shaper", {})
    voices = shaper.get("voices", {})
    for key, voice in voices.items():
        if not voice.get("active"):
            continue
        n = int(key)
        if n in field.partials:
            existing = field.partials[n]
            # Shaper is the active additive voice; preserve beacon band gain as metadata.
            existing.spatial = existing.spatial or {}
            existing.spatial["beacon_gain"] = existing.gain
            existing.gain = voice.get("gain", 0.6)
            existing.pan = voice.get("pan", 0.0)
            existing.phase = voice.get("phase_deg", 0.0)
            existing.spatial["active"] = True
            existing.envelope = {
                "attack_s": voice.get("attack_s", 0.01),
                "release_s": voice.get("release_s", 0.15),
                "shape": voice.get("shape", 0.0),
            }
        else:
            field.partials[n] = Partial(
                n=n,
                freq=voice.get("freq") or None,
                gain=voice.get("gain", 0.6),
                pan=voice.get("pan", 0.0),
                phase=voice.get("phase_deg", 0.0),
                envelope={
                    "attack_s": voice.get("attack_s", 0.01),
                    "release_s": voice.get("release_s", 0.15),
                    "shape": voice.get("shape", 0.0),
                },
            )

    return field


def migrate_beacon_spatial(path: str) -> HarmonicField:
    """Migrate beacon-spatial 13-band format: {bands, mix, master, sensor_mappings?}."""
    data = _read_json(path)
    bands = data.get("bands", [])
    if not bands:
        return HarmonicField(f1=65.0)

    f1 = 65.0
    if "freq" in bands[0] and bands[0]["freq"]:
        f1 = bands[0]["freq"]

    field = HarmonicField(f1=f1)
    for i, b in enumerate(bands, start=1):
        n = b.get("n", i)
        field.partials[n] = Partial(
            n=n,
            gain=b.get("gain", 1.0),
            spatial={
                "az": b.get("az", 0.0),
                "dist": b.get("dist", 1.0),
                "q": b.get("q", 0.5),
                "solo": bool(b.get("solo", 0)),
            },
        )
    return field


def infer_capabilities_for_preset(field: HarmonicField, source_name: str = "") -> RendererCapabilities:
    """Infer a reasonable capability profile from a field."""
    max_n = max(field.partials.keys()) if field.partials else 32
    return RendererCapabilities(
        max_partials=max_n,
        supports_phase=True,
        supports_spatial=True,
        spatial_mode="ambisonic",
        supports_residual=False,
    )
