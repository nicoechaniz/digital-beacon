# NaturalHarmony Core

Foundational data model for the NaturalHarmony / Harmonic Beacon toolkit.

This package defines the renderer-neutral canonical object (`HarmonicField`) and
capability profiles used by all other toolkit packages. It contains no audio I/O
and depends only on the Python standard library.

## Install

```bash
pip install -e packages/nh-core
```

## Core concepts

- `HarmonicField`: time-varying harmonic field with partials, residual, descriptors and transport.
- `Partial`: a single harmonic partial indexed by `n` (or explicit frequency).
- `RendererCapabilities`: what a renderer can do (partial count, spatialization, phase, residual, etc.).
- `Transport`: clock, play/seek state and loop bounds.

## Example

```python
from nh_core.field import HarmonicField, Partial
from nh_core.capabilities import RendererCapabilities
from nh_core.math_utils import freq_for_harmonic

field = HarmonicField(f1=65.0)
for n in range(1, 33):
    field.partials[n] = Partial(n=n, gain=1.0 / n)

caps = RendererCapabilities(max_partials=13)
projected = field.project_to_capabilities(caps)
```
