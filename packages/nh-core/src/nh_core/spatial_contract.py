"""Spatial contract enforcement for Partial.spatial.

CONTRACT: ``Partial.spatial`` is a spatial parameter dictionary, NEVER a
metadata bag. Only spatial-localisation keys are permitted. Any key outside
the allowlist is a contract violation.

Transitional keys (``beacon_gain``, ``active``) are permitted during migration
but must be removed before the preset reaches v2 (Phase 10).

ALLOWED KEYS
    az        float   azimuth (degrees, 0–360)
    dist      float   distance (normalized, 0.0–1.0)
    q         float   filter resonance / bandwidth
    on        bool    per-band enable
    solo      bool    per-band solo

TRANSITIONAL (deprecated, removed in Phase 10)
    beacon_gain  float  legacy v2 migration artifact
    active       bool   legacy shaper voice state
"""

from typing import Any, Dict, List, Optional

# Canonical spatial parameter names — invariant contract, NEVER shrink this set.
SPATIAL_KEYS = frozenset({"az", "dist", "q", "on", "solo"})

# Legacy transitional keys tolerated during migration phases only.
TRANSITIONAL_KEYS = frozenset({"beacon_gain", "active"})

ALLOWED_KEYS = SPATIAL_KEYS | TRANSITIONAL_KEYS


def validate_spatial(spatial: Optional[Dict[str, Any]]) -> List[str]:
    """Validate that a spatial dict only contains allowed keys.

    Returns a list of error strings (empty if valid).
    """
    if spatial is None:
        return []
    errors: List[str] = []
    for key in spatial:
        if key not in ALLOWED_KEYS:
            errors.append(
                f"Partial.spatial contains non-spatial key '{key}'; "
                f"allowed: {sorted(ALLOWED_KEYS)}"
            )
    return errors


def is_spatial_valid(spatial: Optional[Dict[str, Any]]) -> bool:
    """Return True if the spatial dict satisfies the contract."""
    return len(validate_spatial(spatial)) == 0
