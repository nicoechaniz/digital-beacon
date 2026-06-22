#!/usr/bin/env python3
"""Migrate beacon-spatial presets (13 bands) to digital-beacon (32 bands).

Mapping strategy (best-effort, 1:1 where possible):
  - Bands 1-6 (40, 80, 120, 160, 200, 240 Hz) → digital-beacon bands 1-6 (same freq, Q=0.5)
  - Band 7 (480 Hz)    → bands 7-12    (480-720 Hz range, copy gain/az/dist; Q interpolated)
  - Band 8 (720 Hz)    → bands 13-18   (760-1080 Hz range)
  - Band 9 (960 Hz)    → bands 19-24   (1160-1360 Hz range)
  - Band 10 (1200 Hz)  → bands 25-30   (1400-1600 Hz range)
  - Band 11 (1440 Hz)  → band 31       (1640 Hz)
  - Band 12 (1680 Hz)  → band 32       (1680 Hz, but as HPF not BPF)
  - Band 13 (HPF 1800+) → band 32 (HPF flag)

Usage:
    python3 migrate_presets.py
"""

import json
import re
import sys
from pathlib import Path

SRC_DIR = Path.home() / "Projects" / "beacon-spatial" / "configs"
DST_DIR = Path(__file__).parent.parent / "configs"
DST_DIR.mkdir(parents=True, exist_ok=True)

# Mapping: source index (0-12) → list of (dst_n, weight) for expansion
# weight is used to interpolate the source value (e.g. Q, dist) when
# multiple destination bands come from one source.
EXPANSION = {
    0: [(1, 1.0)],                          # band 1 → dst 1
    1: [(2, 1.0)],                          # band 2 → dst 2
    2: [(3, 1.0)],                          # band 3 → dst 3
    3: [(4, 1.0)],                          # band 4 → dst 4
    4: [(5, 1.0)],                          # band 5 → dst 5
    5: [(6, 1.0)],                          # band 6 → dst 6
    6: [(7, 1.0), (8, 1.0), (9, 1.0), (10, 1.0), (11, 1.0), (12, 1.0)],  # 480Hz
    7: [(13, 1.0), (14, 1.0), (15, 1.0), (16, 1.0), (17, 1.0), (18, 1.0)], # 720Hz
    8: [(19, 1.0), (20, 1.0), (21, 1.0), (22, 1.0), (23, 1.0), (24, 1.0)], # 960Hz
    9: [(25, 1.0), (26, 1.0), (27, 1.0), (28, 1.0), (29, 1.0), (30, 1.0)], # 1200Hz
    10: [(31, 1.0)],                         # 1440Hz → dst 31
    11: [(32, 1.0)],                         # 1680Hz → dst 32
    12: [(32, 1.0)],                         # HPF 1800+ → dst 32 (HPF flag)
}

# Q for the destination bands (1 octava = 0.5 for digital-beacon).
# Source bands have a Q value too, but in digital-beacon all bands use
# Q=0.5 except band 32 which is HPF. We preserve the source Q as metadata
# in the preset but the actual engine uses its own per-band Q (only band
# 32 is special — q32 controls the HPF cutoff scaling).
DEFAULT_Q = 0.5


def migrate_preset(src_path: Path) -> dict:
    with open(src_path) as f:
        src = json.load(f)

    src_bands = src.get("bands", [])
    dst_bands = []

    for i, src_band in enumerate(src_bands):
        if i not in EXPANSION:
            continue
        for dst_n, _ in EXPANSION[i]:
            dst_band = {
                "n": dst_n,
                "gain": float(src_band.get("gain", 0.8)),
                "az": float(src_band.get("az", 0)),
                "dist": float(src_band.get("dist", 2.0)),
                "on": 1 if int(src_band.get("solo", 0)) == 0 else 0,
            }
            if i == 12:
                # Last source band was HPF — flag the destination band as HPF.
                dst_band["mode"] = "hpf"
                dst_band["q32"] = 1.0
            elif i < 6:
                dst_band["q"] = DEFAULT_Q
            else:
                # Mid/high bands — keep source Q as reference
                src_q = src_band.get("q", DEFAULT_Q)
                dst_band["q"] = float(src_q) if src_q is not None else DEFAULT_Q
            dst_bands.append(dst_band)

    return {
        "bands": dst_bands,
        "master": float(src.get("master", 0.9)),
        "migrated_from": {
            "source": str(src_path),
            "source_bands": len(src_bands),
            "dest_bands": len(dst_bands),
        },
    }


def main():
    if not SRC_DIR.exists():
        print(f"ERROR: source dir not found: {SRC_DIR}", file=sys.stderr)
        sys.exit(1)

    src_files = sorted(SRC_DIR.glob("*.json"))
    print(f"Migrating {len(src_files)} presets from {SRC_DIR}")
    print(f"  to {DST_DIR}")
    print()

    for src in src_files:
        # Preserve original name
        dst_name = src.stem
        # Skip non-preset files (test_sensor.json is not a real preset)
        if "sensor" in dst_name.lower():
            print(f"  skip {src.name} (test sensor file)")
            continue

        try:
            dst = migrate_preset(src)
        except Exception as exc:
            print(f"  ERROR migrating {src.name}: {exc}")
            continue

        dst_path = DST_DIR / f"{dst_name}.json"
        with open(dst_path, "w") as f:
            json.dump(dst, f, indent=2)
        print(f"  {src.name} ({len(dst['bands'])} dst bands) -> {dst_path.name}")

    print()
    print(f"Done. {len(list(DST_DIR.glob('*.json')))} presets in {DST_DIR}")


if __name__ == "__main__":
    main()
