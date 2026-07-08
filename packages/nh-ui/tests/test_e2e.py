"""End-to-end Playwright tests for the NaturalHarmony v2 UI shell.

These tests start a real FastAPI server with a SceneState and drive the built
web UI in system Chrome. They intentionally validate the v2-only surface: no
renderer selector, scene snapshot source cards, Launchpad grid, spatial list,
and path controls.
"""
from __future__ import annotations

import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Generator

import pytest
from playwright.sync_api import sync_playwright

PYTHON = sys.executable


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_for_port(port: int, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1.0):
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"server did not start on port {port} within {timeout}s")


@pytest.fixture
def server_url() -> Generator[str, None, None]:
    port = _free_port()
    script = f"""
import uvicorn
from nh_core import HarmonicScene, BeaconSource, ShaperSource, SpatialBand, HarmonicField, RendererCapabilities
from nh_model import SceneState
from nh_runtime import BaseFieldServer
from nh_ui import app, set_scene_state
from nh_ui.server import make_app

scene = HarmonicScene(sources={{
    'beacon': BeaconSource(source_id='beacon', f1=65.0, master_gain=0.8),
    'shaper': ShaperSource(source_id='shaper', master_gain=0.5),
}})
for n in range(1, 33):
    scene.sources['beacon'].bands[n] = SpatialBand(az=(n - 1) * 360.0 / 32.0, dist=1.0, q=0.5, on=True)
state = SceneState(scene=scene)
set_scene_state(state)
runtime = BaseFieldServer(
    base_field=HarmonicField(f1=65.0),
    renderer_capabilities=RendererCapabilities(max_partials=32, supports_phase=True, supports_spatial=True),
)
make_app(runtime)
uvicorn.run(app, host='127.0.0.1', port={port}, log_level='warning')
"""
    proc = subprocess.Popen(
        [PYTHON, "-c", script],
        cwd=Path(__file__).resolve().parents[2],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        _wait_for_port(port)
        yield f"http://127.0.0.1:{port}"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def _page(server_url: str):
    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path="/usr/bin/google-chrome",
            args=["--headless", "--no-sandbox", "--disable-gpu"],
        )
        page = browser.new_page(viewport={"width": 1440, "height": 1100})
        try:
            yield page
        finally:
            browser.close()


def test_v2_shell_loads_required_panels(server_url: str) -> None:
    for page in _page(server_url):
        page.goto(server_url)
        page.wait_for_selector("#app-shell", timeout=10_000)
        assert page.text_content("h1") == "NaturalHarmony v2"
        page.wait_for_selector("#connection-status.connected", timeout=10_000)

        required_ids = [
            "preset-bar", "sources-panel", "source-card-beacon", "source-card-shaper",
            "samples-panel", "shaper-panel", "launchpad-grid", "active-voices",
            "panic-button", "spatial-panel", "spatial-band-list", "spatial-radar",
            "processing-panel", "lfo-panel", "analysis-panel", "analysis-f0",
            "analysis-phideus", "analysis-proposed-f1", "event-log",
        ]
        missing = page.evaluate(
            "ids => ids.filter(id => !document.getElementById(id))",
            required_ids,
        )
        assert missing == []


def test_v2_shell_has_no_renderer_selector(server_url: str) -> None:
    for page in _page(server_url):
        page.goto(server_url)
        page.wait_for_selector("#app-shell", timeout=10_000)
        assert page.locator("#renderer-section, #renderer-select, [data-tab='renderer']").count() == 0
        assert "WebAudio" not in (page.text_content("body") or "")
        assert "Python (sounddevice)" not in (page.text_content("body") or "")


def test_v2_scene_data_renders(server_url: str) -> None:
    for page in _page(server_url):
        page.goto(server_url)
        page.wait_for_selector("#connection-status.connected", timeout=10_000)
        assert page.locator(".source-card").count() == 2
        assert page.locator(".pad-button").count() == 64
        assert page.locator(".spatial-row").count() == 32
        assert page.locator("#preset-select option").count() >= 1


def test_v2_path_control_updates_scene(server_url: str) -> None:
    for page in _page(server_url):
        page.goto(server_url)
        page.wait_for_selector("#connection-status.connected", timeout=10_000)
        value = page.evaluate(
            """async () => {
                await fetch('/nh/v2/scene/control', {
                  method: 'POST',
                  headers: {'Content-Type': 'application/json'},
                  body: JSON.stringify({path: 'sources.beacon.f1_offset', value: 7.5})
                });
                const r = await fetch('/nh/v2/scene');
                const data = await r.json();
                return data.sources.beacon.runtime.f1_offset;
            }"""
        )
        assert value == 7.5


def test_v2_sources_mixer_controls_update_runtime(server_url: str) -> None:
    for page in _page(server_url):
        page.goto(server_url)
        page.wait_for_selector("#connection-status.connected", timeout=10_000)
        page.wait_for_selector("#source-gain-beacon", timeout=10_000)

        gain = page.evaluate(
            """async () => {
                const slider = document.querySelector('#source-gain-beacon');
                slider.value = '0.42';
                slider.dispatchEvent(new Event('input', {bubbles: true}));
                await new Promise(resolve => setTimeout(resolve, 250));
                const r = await fetch('/nh/v2/scene');
                const data = await r.json();
                return data.sources.beacon.runtime.gain_offset;
            }"""
        )
        assert gain == 0.42

        muted = page.evaluate(
            """async () => {
                document.querySelector('#source-card-shaper [data-action=\"mute\"]').click();
                await new Promise(resolve => setTimeout(resolve, 250));
                const r = await fetch('/nh/v2/scene');
                const data = await r.json();
                return data.sources.shaper.runtime.gain_offset;
            }"""
        )
        assert muted == 0


def test_v2_launchpad_grid_click_toggles_voice(server_url: str) -> None:
    for page in _page(server_url):
        page.goto(server_url)
        page.wait_for_selector("#connection-status.connected", timeout=10_000)
        page.locator(".pad-button[data-n='7']").click()
        page.wait_for_function(
            """() => document.querySelector('#active-voices')?.textContent?.includes('1 voices')""",
            timeout=10_000,
        )
        assert page.locator(".pad-button[data-n='7'].active").count() == 1
        assert "H7" in (page.text_content("#voice-list") or "")

        page.locator("#panic-button").click()
        page.wait_for_function(
            """() => document.querySelector('#active-voices')?.textContent?.includes('0 voices')""",
            timeout=10_000,
        )
        assert page.locator(".pad-button.active").count() == 0


def test_v2_shaper_pad_and_panic(server_url: str) -> None:
    for page in _page(server_url):
        page.goto(server_url)
        page.wait_for_selector("#connection-status.connected", timeout=10_000)
        result = page.evaluate(
            """async () => {
                await fetch('/nh/v2/scene/control', {
                  method: 'POST', headers: {'Content-Type': 'application/json'},
                  body: JSON.stringify({type: 'pad_on', value: {n: 5, vel: 127}})
                });
                let r = await fetch('/nh/v2/scene');
                let data = await r.json();
                const active = Object.keys(data.sources.shaper.runtime.active_voices);
                await fetch('/nh/v2/scene/control', {
                  method: 'POST', headers: {'Content-Type': 'application/json'},
                  body: JSON.stringify({type: 'panic', value: true})
                });
                r = await fetch('/nh/v2/scene');
                data = await r.json();
                const afterPanic = Object.keys(data.sources.shaper.runtime.active_voices);
                return {active, afterPanic, beaconBands: Object.keys(data.sources.beacon.bands).length};
            }"""
        )
        assert result["active"] == ["5"]
        assert result["afterPanic"] == []
        assert result["beaconBands"] == 32


def test_v2_shell_screenshot(server_url: str) -> None:
    for page in _page(server_url):
        page.goto(server_url)
        page.wait_for_selector("#connection-status.connected", timeout=10_000)
        out = "/tmp/nh-ui-v2-playwright.png"
        page.screenshot(path=out, full_page=True)
        assert Path(out).exists()
        assert Path(out).stat().st_size > 10_000


