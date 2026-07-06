import time
from typing import Callable, Dict, Optional


class PhoneIMUAdapter:
    """Adapter for phone DeviceOrientation/DeviceMotion events.

    Replicates beacon-spatial sensor interpreter behavior:
    - yaw maps to azimuth
    - pitch/roll/scale available as sensor events
    - accel magnitude is sqrt(ax^2+ay^2+az^2) - 1, scaled
    """

    def __init__(self, callback: Optional[Callable[[Dict[str, object]], None]] = None):
        self.callback = callback
        self._center_offset = {"yaw": 0.0, "pitch": 0.0, "roll": 0.0}
        self._yaw_last_alpha: Optional[float] = None
        self._yaw_unwrapped = 0.0

    def recenter(self, yaw: float, pitch: float, roll: float) -> None:
        self._center_offset = {"yaw": yaw, "pitch": pitch, "roll": roll}

    def _unwrap_yaw(self, alpha: Optional[float]) -> float:
        if alpha is None or alpha != alpha:  # NaN check
            return self._yaw_unwrapped
        if self._yaw_last_alpha is None:
            self._yaw_last_alpha = alpha
            self._yaw_unwrapped = alpha
            return self._yaw_unwrapped
        delta = alpha - self._yaw_last_alpha
        if delta > 180:
            delta -= 360
        elif delta < -180:
            delta += 360
        self._yaw_unwrapped += delta
        self._yaw_last_alpha = alpha
        return self._yaw_unwrapped

    def on_device_orientation(self, alpha: Optional[float], beta: Optional[float],
                              gamma: Optional[float], timestamp: Optional[float] = None) -> None:
        """Process a DeviceOrientation event (yaw/alpha, pitch/beta, roll/gamma)."""
        yaw = self._unwrap_yaw(alpha)
        centered_yaw = yaw - self._center_offset["yaw"]
        centered_pitch = (beta or 0.0) - self._center_offset["pitch"]
        centered_roll = (gamma or 0.0) - self._center_offset["roll"]
        ts = timestamp if timestamp is not None else time.time()
        for name, value in [("imu.orientation.yaw", centered_yaw),
                            ("imu.orientation.pitch", centered_pitch),
                            ("imu.orientation.roll", centered_roll)]:
            ev = {
                "timestamp": ts,
                "type": name,
                "value": value,
                "confidence": 1.0,
                "rate": 60.0,
                "units": "degrees",
            }
            if self.callback:
                self.callback(ev)

    def on_device_motion(self, ax: float, ay: float, az: float,
                         rx: float = 0.0, ry: float = 0.0, rz: float = 0.0,
                         timestamp: Optional[float] = None) -> None:
        """Process a DeviceMotion event (acceleration and rotation rate)."""
        accel = max(0.0, (ax * ax + ay * ay + az * az) ** 0.5 - 1.0) * 0.5
        rotrate = (rx * rx + ry * ry + rz * rz) ** 0.5
        ts = timestamp if timestamp is not None else time.time()
        for name, value, units in [
            ("imu.acceleration.magnitude", accel, "g"),
            ("imu.rotation_rate.magnitude", rotrate, "rad/s"),
        ]:
            ev = {
                "timestamp": ts,
                "type": name,
                "value": value,
                "confidence": 1.0,
                "rate": 60.0,
                "units": units,
            }
            if self.callback:
                self.callback(ev)
