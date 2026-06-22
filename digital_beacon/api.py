"""Web control surface for digital-beacon.

FastAPI app:
  - GET  /              → static index.html (the dashboard)
  - GET  /api/state     → full state snapshot (f1 + 32 voices)
  - POST /api/panic     → panic all
  - POST /api/harmonic/{n}/gain → set per-voice gain (0..1)
  - POST /api/harmonic/{n}/on   → toggle voice on/off
  - WS   /ws            → push state on every change (VoiceParameterStore._on_change)

Pattern adapted from NaturalHarmony/harmonic_shaper/api.py.
"""

import asyncio
import json
import logging
from pathlib import Path
from typing import Optional, Set

try:
    from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
    from fastapi.responses import HTMLResponse
    from fastapi.staticfiles import StaticFiles
    import uvicorn
    HAS_FASTAPI = True
except ImportError:
    HAS_FASTAPI = False

from .state import VoiceParameterStore

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent.parent / "static"


def create_app(store: VoiceParameterStore) -> "FastAPI":
    if not HAS_FASTAPI:
        raise ImportError("fastapi and uvicorn are required. pip install fastapi uvicorn[standard]")

    app = FastAPI(title="digital-beacon", version="0.1.0")

    # ─── WebSocket connection manager ─────────────────────────────────────
    class _WsManager:
        def __init__(self):
            self._connections: Set[WebSocket] = set()

        async def connect(self, ws: WebSocket):
            await ws.accept()
            self._connections.add(ws)

        def disconnect(self, ws: WebSocket):
            self._connections.discard(ws)

        async def broadcast(self, data: dict):
            dead = set()
            for ws in self._connections:
                try:
                    await ws.send_json(data)
                except Exception:
                    dead.add(ws)
            self._connections -= dead

    ws_mgr = _WsManager()
    _loop: Optional[asyncio.AbstractEventLoop] = None

    def _on_change():
        if _loop and _loop.is_running():
            data = store.to_dict()
            try:
                asyncio.run_coroutine_threadsafe(ws_mgr.broadcast(data), _loop)
            except Exception:
                pass

    store._on_change = _on_change

    @app.on_event("startup")
    async def _startup():
        nonlocal _loop
        _loop = asyncio.get_event_loop()

    # ─── Static ──────────────────────────────────────────────────────────
    @app.get("/", response_class=HTMLResponse)
    async def root():
        index = STATIC_DIR / "index.html"
        if not index.exists():
            return HTMLResponse(
                "<h1>digital-beacon</h1><p>index.html not found in static/</p>",
                status_code=500,
            )
        return HTMLResponse(index.read_text())

    # ─── REST ────────────────────────────────────────────────────────────
    @app.get("/api/state")
    async def get_state():
        return store.to_dict()

    @app.post("/api/panic")
    async def panic():
        store.panic()
        return {"ok": True, "action": "panic"}

    @app.post("/api/harmonic/{n}/gain")
    async def set_voice_gain(n: int, body: dict):
        if n < 1 or n > 32:
            raise HTTPException(400, "n must be 1..32")
        gain = float(body.get("gain", 0.5))
        store.set_gain(n, max(0.0, min(1.0, gain)))
        return {"ok": True, "n": n, "gain": gain}

    @app.post("/api/harmonic/{n}/on")
    async def set_voice_on(n: int, body: dict):
        """Toggle or set a voice active. Sends /beacon/voice/on to the audio
        engine (which makes the Shaper render the sine) and updates store."""
        if n < 1 or n > 32:
            raise HTTPException(400, "n must be 1..32")
        on = bool(body.get("on", True))
        if on:
            # Re-trigger with current f1
            freq = store.f1 * n
            vid = next(_vid_iter())
            store.voice_on(n, vid, freq)
        else:
            # Find the active voice_id for this harmonic_n and turn it off
            snap = store.get_all_snapshot()
            entry = snap.get(n)
            if entry is not None and entry.voice_id is not None:
                store.voice_off(entry.voice_id)
        return {"ok": True, "n": n, "on": on}

    # Monotonic voice_id generator for the API
    def _vid_iter():
        i = 10000
        while True:
            yield i
            i += 1

    # ─── WebSocket ───────────────────────────────────────────────────────
    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket):
        await ws_mgr.connect(ws)
        await ws.send_json(store.to_dict())
        try:
            while True:
                await ws.receive_text()
        except WebSocketDisconnect:
            ws_mgr.disconnect(ws)

    return app


def run_server(store: VoiceParameterStore, host: str = "127.0.0.1", port: int = 8080):
    """Blocking uvicorn runner — call from a thread."""
    app = create_app(store)
    uvicorn.run(app, host=host, port=port, log_level="warning")
