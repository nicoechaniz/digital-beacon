from nh_core import HarmonicField, RendererCapabilities
from nh_presets.schema import Preset


def project_to_capabilities(preset: Preset, capabilities: RendererCapabilities) -> Preset:
    """Return a new Preset projected to the given capabilities."""
    projected_field = preset.harmonic_field.project_to_capabilities(capabilities)
    return Preset(
        version=preset.version,
        harmonic_field=projected_field,
        renderer_capabilities_required=capabilities,
        metadata={**preset.metadata, "projected": True},
    )
