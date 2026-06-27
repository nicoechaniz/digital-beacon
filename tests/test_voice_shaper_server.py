"""Tests for the in-memory samples cache in voice_shaper_server.

Run with:
    python -m pytest tests/test_voice_shaper_server.py -q
or
    python -m unittest tests.test_voice_shaper_server -v
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen

# Ensure we can import the module under test
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import voice_shaper_server as v  # type: ignore


class TestSamplesCache(unittest.TestCase):
    def setUp(self) -> None:
        self._orig_cache = v._SAMPLES_CACHE
        v._SAMPLES_CACHE = None

    def tearDown(self) -> None:
        v._SAMPLES_CACHE = self._orig_cache

    def test_refresh_populates_cache(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "voice1.wav").write_bytes(b"RIFF")
            (d / "voice1_synth.wav").write_bytes(b"")  # should be skipped

            count = v.refresh_samples_cache(d)
            self.assertEqual(count, 1)
            self.assertIsNotNone(v._SAMPLES_CACHE)
            self.assertEqual(len(v._SAMPLES_CACHE), 1)
            self.assertEqual(v._SAMPLES_CACHE[0]["id"], "voice1")

    def test_get_samples_uses_cache(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "a.wav").write_bytes(b"RIFF")
            (d / "b.wav").write_bytes(b"RIFF")

            v.refresh_samples_cache(d)
            samples = v._SAMPLES_CACHE if v._SAMPLES_CACHE is not None else v.discover_samples(d)
            self.assertEqual(len(samples), 2)

    def test_fallback_when_cache_none(self) -> None:
        v._SAMPLES_CACHE = None
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "only.wav").write_bytes(b"RIFF")
            samples = v._SAMPLES_CACHE if v._SAMPLES_CACHE is not None else v.discover_samples(d)
            self.assertEqual(len(samples), 1)


class TestHTTPServerEndpoints(unittest.TestCase):
    def test_get_samples_and_post_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            voice_dir = Path(td)
            (voice_dir / "keep1.wav").write_bytes(b"RIFF....")
            (voice_dir / "keep2.wav").write_bytes(b"RIFF....")
            (voice_dir / "skip_synth.wav").write_bytes(b"")

            # ensure clean
            v._SAMPLES_CACHE = None
            v.refresh_samples_cache(voice_dir)

            handler_cls = v.make_handler(voice_dir, voice_dir / "viz")
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
            port = server.server_address[1]
            thr = threading.Thread(target=server.serve_forever, daemon=True)
            thr.start()
            time.sleep(0.15)

            base = f"http://127.0.0.1:{port}"
            try:
                # GET /samples from cache
                with urlopen(f"{base}/samples", timeout=2.0) as resp:
                    payload = json.loads(resp.read())
                self.assertEqual(payload["count"], 2)
                ids = [s["id"] for s in payload["samples"]]
                self.assertIn("keep1", ids)
                self.assertIn("keep2", ids)

                # POST /samples/refresh
                req = Request(f"{base}/samples/refresh", method="POST")
                with urlopen(req, timeout=2.0) as resp:
                    ref = json.loads(resp.read())
                self.assertTrue(ref["ok"])
                self.assertEqual(ref["count"], 2)
                self.assertIn("voice_dir", ref)

                # delete and refresh
                (voice_dir / "keep2.wav").unlink()
                req = Request(f"{base}/samples/refresh", method="POST")
                with urlopen(req, timeout=2.0) as resp:
                    ref2 = json.loads(resp.read())
                self.assertEqual(ref2["count"], 1)
            finally:
                server.shutdown()
                server.server_close()

    def test_get_orig_success(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            voice_dir = Path(td)
            wav_bytes = b"RIFFxxxxWAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00\x44\xac\x00\x00\x88X\x01\x00\x02\x00\x10\x00data\x04\x00\x00\x00\x00\x00\x00\x00"
            (voice_dir / "test_sample.wav").write_bytes(wav_bytes)
            v._SAMPLES_CACHE = None
            v.refresh_samples_cache(voice_dir)
            handler_cls = v.make_handler(voice_dir, voice_dir / "viz")
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
            port = server.server_address[1]
            thr = threading.Thread(target=server.serve_forever, daemon=True)
            thr.start()
            time.sleep(0.15)
            base = f"http://127.0.0.1:{port}"
            try:
                with urlopen(f"{base}/orig/test_sample", timeout=2.0) as resp:
                    self.assertEqual(resp.status, 200)
                    self.assertEqual(resp.headers.get("Content-Type"), "audio/wav")
                    body = resp.read()
                    self.assertEqual(body, wav_bytes)
            finally:
                server.shutdown()
                server.server_close()

    def test_get_orig_not_found(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            voice_dir = Path(td)
            v._SAMPLES_CACHE = None
            v.refresh_samples_cache(voice_dir)
            handler_cls = v.make_handler(voice_dir, voice_dir / "viz")
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
            port = server.server_address[1]
            thr = threading.Thread(target=server.serve_forever, daemon=True)
            thr.start()
            time.sleep(0.15)
            base = f"http://127.0.0.1:{port}"
            try:
                import urllib.error
                try:
                    with urlopen(f"{base}/orig/nonexistent", timeout=2.0) as _:
                        pass
                except urllib.error.HTTPError as he:
                    self.assertEqual(he.code, 404)
                    payload = json.loads(he.read().decode("utf-8"))
                    self.assertEqual(payload, {"error": "sample not found"})
            finally:
                server.shutdown()
                server.server_close()

    def test_get_viz_success(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            viz_dir = Path(td)
            png_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\xfe\xdc\xdc\x00\x00\x00\x00IEND\xaeB`\x82"
            (viz_dir / "test.png").write_bytes(png_bytes)
            voice_dir = viz_dir  # dummy
            v._SAMPLES_CACHE = None
            handler_cls = v.make_handler(voice_dir, viz_dir)
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
            port = server.server_address[1]
            thr = threading.Thread(target=server.serve_forever, daemon=True)
            thr.start()
            time.sleep(0.15)
            base = f"http://127.0.0.1:{port}"
            try:
                with urlopen(f"{base}/viz/test.png", timeout=2.0) as resp:
                    self.assertEqual(resp.status, 200)
                    self.assertIn("image/png", resp.headers.get("Content-Type", ""))
            finally:
                server.shutdown()
                server.server_close()

    def test_get_viz_not_found(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            viz_dir = Path(td)
            voice_dir = viz_dir
            handler_cls = v.make_handler(voice_dir, viz_dir)
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
            port = server.server_address[1]
            thr = threading.Thread(target=server.serve_forever, daemon=True)
            thr.start()
            time.sleep(0.15)
            base = f"http://127.0.0.1:{port}"
            try:
                import urllib.error
                try:
                    with urlopen(f"{base}/viz/missing.png", timeout=2.0) as _:
                        pass
                except urllib.error.HTTPError as he:
                    self.assertEqual(he.code, 404)
            finally:
                server.shutdown()
                server.server_close()

    def test_get_viz_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            viz_dir = Path(td)
            voice_dir = viz_dir
            handler_cls = v.make_handler(voice_dir, viz_dir)
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
            port = server.server_address[1]
            thr = threading.Thread(target=server.serve_forever, daemon=True)
            thr.start()
            time.sleep(0.15)
            base = f"http://127.0.0.1:{port}"
            try:
                import urllib.error
                try:
                    with urlopen(f"{base}/viz/../escape.png", timeout=2.0) as _:
                        pass
                except urllib.error.HTTPError as he:
                    self.assertEqual(he.code, 403)
            finally:
                server.shutdown()
                server.server_close()


class TestPickLoudChannel(unittest.TestCase):
    """Regression tests for the stereo-channel detector.

    The bug we are locking down: the original render handler hardcoded
    ``y[:, 0]`` (left channel), which is silent for R24 captures because
    the actual mic signal lives on Ch2. Result: librosa extracts zero F0
    frames and the synthesizer emits silence even though the response is
    HTTP 200 with a valid WAV header. pick_loud_channel() must detect
    which channel has the voice signal.
    """

    def test_mono_passthrough(self) -> None:
        import numpy as np
        y = np.array([0.1, -0.2, 0.3, -0.4], dtype=np.float32)
        out = v.pick_loud_channel(y)
        self.assertEqual(out.shape, (4,))
        np.testing.assert_array_equal(out, y)

    def test_right_channel_is_loud(self) -> None:
        """R24 pattern: Ch2 (right) has the voice, Ch1 (left) is silent bleed."""
        import numpy as np
        silent = np.zeros(1000, dtype=np.float32)
        signal = np.sin(np.linspace(0, 8 * np.pi, 1000)).astype(np.float32) * 0.5
        stereo = np.stack([silent, signal], axis=1)
        out = v.pick_loud_channel(stereo)
        self.assertEqual(out.shape, (1000,))
        np.testing.assert_allclose(out, signal, atol=1e-6)

    def test_left_channel_is_loud(self) -> None:
        """Symmetric: if Ch1 is the loud one, use it."""
        import numpy as np
        silent = np.zeros(1000, dtype=np.float32)
        signal = np.sin(np.linspace(0, 8 * np.pi, 1000)).astype(np.float32) * 0.5
        stereo = np.stack([signal, silent], axis=1)
        out = v.pick_loud_channel(stereo)
        self.assertEqual(out.shape, (1000,))
        np.testing.assert_allclose(out, signal, atol=1e-6)

    def test_balanced_channels_are_averaged(self) -> None:
        """When both channels are equally loud (e.g. true stereo mix), average."""
        import numpy as np
        t = np.linspace(0, 8 * np.pi, 1000)
        l = np.sin(t).astype(np.float32) * 0.3
        r = np.cos(t).astype(np.float32) * 0.3
        stereo = np.stack([l, r], axis=1)
        out = v.pick_loud_channel(stereo)
        self.assertEqual(out.shape, (1000,))
        expected = (l + r) / 2
        np.testing.assert_allclose(out, expected, atol=1e-6)


if __name__ == "__main__":
    unittest.main()
