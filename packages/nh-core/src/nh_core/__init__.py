from nh_core.field import HarmonicField, Partial, Residual, Transport
from nh_core.capabilities import RendererCapabilities
from nh_core.math_utils import (
    cents_difference,
    freq_for_harmonic,
    octave_reduce,
    playable_frequency,
)
from nh_core.spatial_contract import (
    SPATIAL_KEYS,
    TRANSITIONAL_KEYS,
    ALLOWED_KEYS,
    validate_spatial,
    is_spatial_valid,
)
from nh_core.scene import (
    HarmonicScene,
    BeaconSource,
    ShaperSource,
    ShaperVoice,
    SampleSource,
    VoiceSource,
    SpatialBand,
    ProcessorState,
    ProcessingChain,
    LFOState,
    ModulationRoute,
)
from nh_core.processors import (
    HarmonicCombParams,
    BinauralSpatializerParams,
    FilterParams,
    DynamicsParams,
)

__all__ = [
    # Field (v1)
    "HarmonicField",
    "Partial",
    "Residual",
    "Transport",
    "RendererCapabilities",
    # Math
    "cents_difference",
    "freq_for_harmonic",
    "octave_reduce",
    "playable_frequency",
    # Spatial contract
    "SPATIAL_KEYS",
    "TRANSITIONAL_KEYS",
    "ALLOWED_KEYS",
    "validate_spatial",
    "is_spatial_valid",
    # Scene (v2)
    "HarmonicScene",
    "BeaconSource",
    "ShaperSource",
    "ShaperVoice",
    "SampleSource",
    "VoiceSource",
    "SpatialBand",
    "ProcessorState",
    "ProcessingChain",
    "LFOState",
    "ModulationRoute",
]
