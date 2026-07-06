import numpy as np
from typing import Any, Dict, Optional

from nh_core import HarmonicField, RendererCapabilities
from nh_renderers.renderer import Renderer


class PythonSounddeviceRenderer(Renderer):
    """Reference additive renderer using sounddevice.

    This renderer is the correctness oracle for other renderers.
    It synthesizes each partial as a sine wave with pan and phase.
    """

    def __init__(self, sr: int = 48000, block_size: int = 512, device: Optional[int] = None):
        self.sr = sr
        self.block_size = block_size
        self.device = device
        self._stream = None
        self._running = False
        self._last_field: Optional[HarmonicField] = None
        self._phase = 0.0

    def start(self) -> None:
        import sounddevice as sd
        self._stream = sd.OutputStream(
            samplerate=self.sr,
            blocksize=self.block_size,
            channels=2,
            dtype=np.float32,
            device=self.device,
            callback=self._callback,
        )
        self._stream.start()
        self._running = True

    def stop(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        self._running = False

    def render(self, field: HarmonicField, transport: Dict[str, Any] = None) -> None:
        self._last_field = field

    def _callback(self, outdata: np.ndarray, frames: int, time_info, status) -> None:
        if self._last_field is None:
            outdata[:] = 0.0
            return

        field = self._last_field
        t = np.arange(frames) / self.sr
        base_freq = field.f1

        out = np.zeros((frames, 2), dtype=np.float32)
        for n, partial in field.partials.items():
            if partial.gain <= 0.0:
                continue
            freq = base_freq * n
            phase_rad = np.deg2rad(partial.phase)
            pan = partial.pan
            left_gain = 0.5 * (1.0 - pan)
            right_gain = 0.5 * (1.0 + pan)
            samples = partial.gain * np.sin(2.0 * np.pi * freq * t + phase_rad + self._phase)
            out[:, 0] += left_gain * samples
            out[:, 1] += right_gain * samples

        self._phase += 2.0 * np.pi * base_freq * frames / self.sr
        self._phase %= 2.0 * np.pi

        # Soft clip
        peak = np.max(np.abs(out))
        if peak > 1.0:
            out = np.tanh(out)
        outdata[:] = out

    @property
    def is_running(self) -> bool:
        return self._running

    def get_capabilities(self) -> RendererCapabilities:
        return RendererCapabilities(
            max_partials=32,
            supports_phase=True,
            supports_spatial=True,
            spatial_mode="none",
            supports_residual=False,
            sample_rate=self.sr,
            block_size=self.block_size,
        )
