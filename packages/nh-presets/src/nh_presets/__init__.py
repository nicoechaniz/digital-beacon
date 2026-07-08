from nh_presets.schema import Preset, load, save
from nh_presets.migrations import (
    migrate_beacon_spatial,
    migrate_digital_beacon_v1,
    migrate_digital_beacon_v2,
)
from nh_presets.projection import project_to_capabilities
from nh_presets.validation import validate
from nh_presets.scene_preset import (
    PresetV2,
    load_v2,
    save_v2,
    migrate_v1_to_v2,
    migrate_preset_v1_to_v2,
    validate_v2,
    SCENE_VERSION,
)

__all__ = [
    # v1
    "Preset",
    "load",
    "save",
    "migrate_beacon_spatial",
    "migrate_digital_beacon_v1",
    "migrate_digital_beacon_v2",
    "project_to_capabilities",
    "validate",
    # v2
    "PresetV2",
    "load_v2",
    "save_v2",
    "migrate_v1_to_v2",
    "migrate_preset_v1_to_v2",
    "validate_v2",
    "SCENE_VERSION",
]
