"""End-to-end Playwright tests for the NaturalHarmony UI host.

These tests start the real FastAPI/WebSocket server in a subprocess and drive
the built web UI in a headless Chromium browser via Playwright directly.
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

from nh_core import HarmonicField, Partial
from nh_presets import Preset, save
from nh_ui.server import PRESETS_DIR


PYTHON = sys.executable


def _free_port() -> int:
    """Return a free TCP port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_for_port(port: int, timeout: float = 30.0) -> None:
    """Wait until the server is accepting connections on the given port."""
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
    """Start the nh-ui server in a subprocess and yield its HTTP URL.

    The runtime is created inside the subprocess so the WebSocket endpoint has
    a real BaseFieldServer to talk to.
    """
    port = _free_port()

    script = f"""
import uvicorn
from nh_core import HarmonicField, RendererCapabilities
from nh_runtime import BaseFieldServer
from nh_ui.server import make_app

runtime = BaseFieldServer(
    base_field=HarmonicField(f1=65.0),
    renderer_capabilities=RendererCapabilities(
        max_partials=32,
        supports_phase=True,
        supports_spatial=True,
        available_renderers=["python", "webaudio"],
    ),
)
make_app(runtime)
uvicorn.run('nh_ui.server:app', host='127.0.0.1', port={port}, log_level='warning')
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


@pytest.fixture
def preset_file():
    """Create a temporary preset that can be loaded from the preset browser."""
    PRESETS_DIR.mkdir(parents=True, exist_ok=True)
    field = HarmonicField(f1=110.0)
    field.partials[1] = Partial(n=1, gain=1.0)
    field.partials[2] = Partial(n=2, gain=0.5)
    p = Preset(harmonic_field=field)
    path = PRESETS_DIR / "test_e2e.json"
    save(p, str(path))
    yield "test_e2e"
    path.unlink(missing_ok=True)


def _page(server_url: str):
    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path="/usr/bin/google-chrome",
            args=["--headless", "--no-sandbox", "--disable-gpu"],
        )
        page = browser.new_page()
        try:
            yield page
        finally:
            browser.close()


def test_ui_loads_and_connects(server_url: str) -> None:
    """The built SPA loads and the WebSocket connects."""
    for page in _page(server_url):
        page.goto(server_url)
        page.wait_for_selector("h1", timeout=10_000)
        title = page.text_content("h1")
        assert title == "NaturalHarmony"
        page.wait_for_selector("#connection-status.connected", timeout=10_000)
        status = page.text_content("#connection-status")
        assert status == "Connected"
        log = page.text_content("#status-log")
        assert "WebSocket connected" in log


def test_renderer_selector_populates_and_switches(server_url: str) -> None:
    """The renderer dropdown is populated and can switch between backends."""
    for page in _page(server_url):
        page.goto(server_url)
        page.wait_for_selector("#connection-status.connected", timeout=10_000)
        page.locator('.tab-btn[data-tab="renderer"]').click()

        select = page.locator("#renderer-select")
        select.wait_for(state="visible", timeout=10_000)
        page.wait_for_function(
            """() => {
                const s = document.querySelector('#renderer-select');
                return s && !s.disabled && s.options.length > 1;
            }""",
            timeout=10_000,
        )

        options = select.evaluate("el => Array.from(el.options).map(o => o.value)")
        assert "python" in options
        assert "webaudio" in options

        select.select_option("webaudio")
        page.wait_for_timeout(250)
        status_text = page.text_content("#renderer-status")
        if status_text:
            assert "webaudio" in status_text.lower()


def test_load_preset_change_f1_and_panic(server_url: str, preset_file: str) -> None:
    """Load a preset, adjust f1/partial gain, then panic resets the UI."""
    for page in _page(server_url):
        page.goto(server_url)
        page.wait_for_selector("#connection-status.connected", timeout=10_000)
        page.wait_for_selector("#panic:not([disabled])", timeout=10_000)
        page.locator('.tab-btn[data-tab="performance"]').click()

        # Load preset via HTTP
        resp = page.evaluate(
            f"async () => {{ const r = await fetch('/nh/v1/presets/{preset_file}/load', {{method: 'POST'}}); return r.json(); }}"
        )
        assert resp["ok"] is True
        assert resp["f1"] == 110.0

        # Change f1 offset
        slider = page.locator("#f1-slider")
        slider.fill("10")
        slider.dispatch_event("input")

        # Change partial gain
        partial_slider = page.locator(".partial-slider").first
        partial_slider.fill("0.5")
        partial_slider.dispatch_event("input")

        page.wait_for_timeout(500)

        # Click PANIC
        page.locator("#panic").click()
        page.locator('.tab-btn[data-tab="log"]').click()
        page.wait_for_selector("#status-log div", timeout=5_000)
        log_text = page.locator("#status-log").text_content()
        assert "Panic" in log_text or "panic" in log_text.lower()


def test_launchpad_grid_shows_64_pads(server_url: str) -> None:
    """The launchpad mirror renders the full 8x8 (64-pad) grid after connect."""
    for page in _page(server_url):
        page.goto(server_url)
        page.wait_for_selector("#connection-status.connected", timeout=10_000)
        page.locator('.tab-btn[data-tab="launchpad"]').click()
        page.wait_for_selector("#launchpad-mirror .launchpad-pad", timeout=10_000)
        count = page.locator("#launchpad-mirror .launchpad-pad").count()
        assert count == 64


def test_presets_section_loads(server_url: str) -> None:
    """The preset browser section renders after the WebSocket connects."""
    for page in _page(server_url):
        page.goto(server_url)
        page.wait_for_selector("#connection-status.connected", timeout=10_000)
        page.locator('.tab-btn[data-tab="presets"]').click()
        page.wait_for_selector(
            "#preset-browser select",
            timeout=10_000,
            state="attached",
        )
        heading = page.text_content("#preset-browser h2")
        assert heading == "Presets"


def test_ping_through_websocket(server_url: str) -> None:
    """We can send a ping through the browser socket and get a pong."""
    for page in _page(server_url):
        page.goto(server_url)
        page.wait_for_selector("#connection-status.connected", timeout=10_000)

        pong = page.evaluate(
            """async () => {
                const ws = window.ws || new WebSocket(location.origin.replace(/^http/, 'ws') + '/nh/v1/ws');
                if (!window.ws) window.ws = ws;
                if (ws.readyState !== WebSocket.OPEN) {
                    await new Promise((res, rej) => {
                        ws.onopen = res;
                        ws.onerror = rej;
                        setTimeout(() => rej(new Error('websocket open timeout')), 5000);
                    });
                }
                const reply = await new Promise((resolve, reject) => {
                    const handler = (ev) => {
                        const msg = JSON.parse(ev.data);
                        if (msg.type === 'pong') {
                            ws.removeEventListener('message', handler);
                            resolve(msg);
                        }
                    };
                    ws.addEventListener('message', handler);
                    ws.send(JSON.stringify({ type: 'ping', payload: {} }));
                    setTimeout(() => reject(new Error('pong timeout')), 5000);
                });
                return reply;
            }"""
        )
        assert pong == {"type": "pong", "payload": {}}
