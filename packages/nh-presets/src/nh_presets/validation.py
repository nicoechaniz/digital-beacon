from typing import Any, Dict, List

from nh_presets.schema import Preset


def validate(preset: Preset) -> List[str]:
    """Validate a preset. Returns a list of errors (empty if valid)."""
    errors: List[str] = []
    field = preset.harmonic_field

    if field.f1 <= 0:
        errors.append(f"f1 must be positive, got {field.f1}")

    for n, partial in field.partials.items():
        if n <= 0:
            errors.append(f"harmonic index must be positive, got {n}")
        if partial.n != n:
            errors.append(f"partial key {n} does not match partial.n {partial.n}")
        if partial.gain < 0:
            errors.append(f"partial {n} gain must be non-negative")

    if preset.renderer_capabilities_required:
        caps = preset.renderer_capabilities_required
        if caps.max_partials < len(field.partials):
            errors.append(
                f"field has {len(field.partials)} partials but capabilities require max {caps.max_partials}"
            )

    return errors


def validate_dict(d: Dict[str, Any]) -> List[str]:
    """Lightweight structural validation without instantiating full objects."""
    errors: List[str] = []
    if not isinstance(d, dict):
        errors.append("preset must be a dict")
        return errors
    if d.get("version") != "1":
        errors.append(f"unsupported preset version: {d.get('version')}")
    hf = d.get("harmonic_field")
    if not isinstance(hf, dict):
        errors.append("missing harmonic_field")
        return errors
    if hf.get("f1", 0) <= 0:
        errors.append("f1 must be positive")
    return errors
