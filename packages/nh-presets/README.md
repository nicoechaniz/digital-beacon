# NaturalHarmony Presets

Versioned, renderer-neutral preset/session management for the Harmonic Beacon toolkit.

## Install

```bash
pip install -e packages/nh-presets
```

## Scope

- Define a canonical `v1` preset schema based on `nh_core.HarmonicField`.
- Load/save presets with validation.
- Migrate from legacy formats:
  - `digital-beacon` v1 (migrated 13→32 band JSON)
  - `digital-beacon` v2 (`{version, beacon, shaper}`)
  - `beacon-spatial` 13-band JSON
- Project canonical presets to renderer capability profiles.

## Example

```python
from nh_presets import load
from nh_presets.migrations import migrate_digital_beacon_v2

field = migrate_digital_beacon_v2("configs/Amora.json")
canonical = field.to_dict()
```
