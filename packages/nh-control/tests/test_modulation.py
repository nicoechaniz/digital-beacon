"""Tests for LFO modulation and sensor routing (Phase 9)."""
import pytest

from nh_core import LFOState, ModulationRoute
from nh_control.modulation import LFOModulator, SensorRouter


def test_lfo_sine():
    lfo = LFOState(lfo_id="l1", waveform="sine", rate_hz=1.0)
    mod = LFOModulator(lfo=lfo)
    # At phase 0: sin(0) = 0.
    assert mod.advance(0.0) == 0.0
    # Quarter cycle: sin(pi/2) = 1.
    val = mod.advance(0.25)
    assert val == pytest.approx(1.0, abs=1e-6)


def test_lfo_triangle():
    lfo = LFOState(lfo_id="l1", waveform="triangle", rate_hz=1.0)
    mod = LFOModulator(lfo=lfo)
    # triangle(0.0) = 4*|0-0.5|-1 = 1.0
    assert mod.advance(0.0) == pytest.approx(1.0, abs=1e-6)
    # triangle(0.25) = 4*|0.25-0.5|-1 = 0.0
    mod2 = LFOModulator(lfo=LFOState(lfo_id="l2", waveform="triangle", rate_hz=1.0))
    mod2.advance(0.25)
    assert mod2.advance(0.0) == pytest.approx(0.0, abs=1e-6)
    # triangle(0.50) = 4*|0.5-0.5|-1 = -1.0
    mod3 = LFOModulator(lfo=LFOState(lfo_id="l3", waveform="triangle", rate_hz=1.0))
    mod3.advance(0.5)
    assert mod3.advance(0.0) == pytest.approx(-1.0, abs=1e-6)


def test_lfo_saw():
    lfo = LFOState(lfo_id="l1", waveform="saw", rate_hz=1.0)
    mod = LFOModulator(lfo=lfo)
    assert mod.advance(0.0) == pytest.approx(-1.0, abs=1e-6)
    assert mod.advance(0.5) == pytest.approx(0.0, abs=1e-6)


def test_lfo_square():
    lfo = LFOState(lfo_id="l1", waveform="square", rate_hz=1.0)
    mod = LFOModulator(lfo=lfo)
    assert mod.advance(0.0) == 1.0  # phase < 0.5
    assert mod.advance(0.5) == -1.0  # phase wraps to 0.5 → next cycle


def test_lfo_strum_divisor():
    lfo = LFOState(lfo_id="l1", waveform="sine", strum_divisor=2)
    mod = LFOModulator(lfo=lfo)
    # rate = 65 / 2 = 32.5 Hz. After 1/32.5s ≈ 30.7ms: one full cycle.
    val = mod.advance(1.0 / 32.5)
    assert val == pytest.approx(0.0, abs=1e-6)


def test_sensor_router():
    routes = {
        "m1": ModulationRoute(route_id="m1", source="imu_pitch",
                              target_path="sources.beacon.spatial_rotation",
                              scale=2.0, offset=0.0),
    }
    router = SensorRouter(routes)
    results = router.apply("imu_pitch", 0.5, influence=1.0)
    assert len(results) == 1
    assert results[0]["path"] == "sources.beacon.spatial_rotation"
    assert results[0]["value"] == 1.0  # 0.5 * 2.0


def test_sensor_router_disabled():
    routes = {
        "m1": ModulationRoute(route_id="m1", source="eeg",
                              target_path="sources.beacon.f1", scale=1.0),
    }
    router = SensorRouter(routes)
    router.set_enabled("eeg", False)
    results = router.apply("eeg", 0.5)
    assert results == []


def test_sensor_router_clamping():
    routes = {
        "m1": ModulationRoute(route_id="m1", source="imu",
                              target_path="scene.master_gain",
                              scale=1.0, range_min=0.0, range_max=1.0),
    }
    router = SensorRouter(routes)
    # Raw 2.0 → scaled 2.0 → clamped to 1.0.
    results = router.apply("imu", 2.0)
    assert results[0]["value"] == 1.0
