"""Tests for processor parameter schemas (Phase 5)."""
import pytest

from nh_core import (
    HarmonicCombParams,
    BinauralSpatializerParams,
    FilterParams,
    DynamicsParams,
)


def test_harmonic_comb_defaults():
    p = HarmonicCombParams()
    assert p.bandwidth == 0.5
    assert p.wet_dry == 1.0
    assert p.num_harmonics == 32


def test_harmonic_comb_round_trip():
    p = HarmonicCombParams(bandwidth=0.3, q_factor=2.0, wet_dry=0.5, residual=True)
    d = p.to_dict()
    r = HarmonicCombParams.from_dict(d)
    assert r.bandwidth == 0.3
    assert r.q_factor == 2.0
    assert r.residual is True


def test_binaural_spatializer_round_trip():
    from nh_core.processors import SpatialBand as ProcSpatialBand

    p = BinauralSpatializerParams(
        bands=[
            ProcSpatialBand(band_index=1, azimuth=0.0, distance=1.0, q=0.5, gain=1.0),
            ProcSpatialBand(band_index=2, azimuth=30.0, distance=1.5, q=0.3, gain=0.8),
        ],
        hrtf_profile="kemar",
        rotation=45.0,
    )
    d = p.to_dict()
    r = BinauralSpatializerParams.from_dict(d)
    assert r.hrtf_profile == "kemar"
    assert r.rotation == 45.0
    assert len(r.bands) == 2
    assert r.bands[1].azimuth == 30.0


def test_binaural_spatializer_13_bands():
    """Canonical 13-band spatializer."""
    from nh_core.processors import SpatialBand as ProcSpatialBand

    bands = []
    for i in range(1, 14):
        bands.append(ProcSpatialBand(
            band_index=i,
            azimuth=(i - 1) * 30.0,
            distance=1.0,
            q=0.042,
            gain=1.0 / i,
        ))
    p = BinauralSpatializerParams(bands=bands)
    assert len(p.bands) == 13
    assert p.bands[12].band_index == 13


def test_filter_params():
    p = FilterParams(filter_type="bandpass", cutoff_hz=440.0, q=2.0, order=4)
    d = p.to_dict()
    r = FilterParams.from_dict(d)
    assert r.filter_type == "bandpass"
    assert r.cutoff_hz == 440.0


def test_dynamics_params():
    p = DynamicsParams(mode="limiter", threshold_db=-6.0, ratio=20.0,
                       attack_ms=0.1, release_ms=10.0)
    d = p.to_dict()
    r = DynamicsParams.from_dict(d)
    assert r.mode == "limiter"
    assert r.threshold_db == -6.0
