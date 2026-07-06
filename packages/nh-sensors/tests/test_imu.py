import pytest

from nh_sensors import PhoneIMUAdapter


def test_imu_orientation():
    events = []
    adapter = PhoneIMUAdapter(callback=lambda e: events.append(e))
    adapter.on_device_orientation(alpha=90.0, beta=10.0, gamma=5.0, timestamp=1.0)
    assert len(events) == 3
    types = {e["type"] for e in events}
    assert types == {"imu.orientation.yaw", "imu.orientation.pitch", "imu.orientation.roll"}


def test_imu_centering():
    events = []
    adapter = PhoneIMUAdapter(callback=lambda e: events.append(e))
    adapter.recenter(yaw=90.0, pitch=10.0, roll=5.0)
    adapter.on_device_orientation(alpha=90.0, beta=10.0, gamma=5.0, timestamp=1.0)
    yaw_event = [e for e in events if e["type"] == "imu.orientation.yaw"][0]
    assert yaw_event["value"] == pytest.approx(0.0)


def test_imu_wrap():
    events = []
    adapter = PhoneIMUAdapter(callback=lambda e: events.append(e))
    adapter.on_device_orientation(alpha=10.0, beta=0.0, gamma=0.0, timestamp=1.0)
    adapter.on_device_orientation(alpha=350.0, beta=0.0, gamma=0.0, timestamp=2.0)
    yaw_events = [e for e in events if e["type"] == "imu.orientation.yaw"]
    # 350 should unwrap to -10, so delta from 10 to -10 is -20
    assert yaw_events[-1]["value"] == pytest.approx(-10.0)


def test_device_motion():
    events = []
    adapter = PhoneIMUAdapter(callback=lambda e: events.append(e))
    adapter.on_device_motion(ax=1.0, ay=0.0, az=0.0, rx=0.1, ry=0.0, rz=0.0, timestamp=1.0)
    types = {e["type"] for e in events}
    assert "imu.acceleration.magnitude" in types
    assert "imu.rotation_rate.magnitude" in types
