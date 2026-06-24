"""Web control surface for digital-beacon.

FastAPI app:
  - GET  /                          -> static/index.html (the dashboard)
  - GET  /api/state                 -> f1 + 32 voices snapshot (Shaper side)
  - POST /api/panic                 -> panic all (Shaper + beacon via /beacon/panic)
  - POST /api/harmonic/{n}/{param}  -> proxy to SC beacon (gain/az/dist/q/on)
  - POST /api/f1                    -> set f1 (re-tunes all band centers)
  - POST /api/vsource               -> set varispeed rate
  - POST /api/master                -> set master gain
  - POST /api/reset                 -> reset beacon to defaults
  - GET  /api/presets               -> list available preset files
  - POST /api/presets/save          -> save current SC state as preset
  - POST /api/presets/load          -> load a preset (pushes to SC)
  - WS   /ws                        -> push Shaper state on every change

Pattern adapted from NaturalHarmony/harmonic_shaper/api.py and
beacon-spatial/webui.py (load/save/presets logic brought in).
"""

import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import Optional, Set

try:
    from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
    from fastapi.responses import HTMLResponse
    import uvicorn
    HAS_FASTAPI = True
except ImportError:
    HAS_FASTAPI = False

from pythonosc.udp_client import SimpleUDPClient

from .state import VoiceParameterStore
from . import config
from .recorder import Recorder

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent.parent / "static"
PRESETS_DIR = Path(__file__).parent.parent / "configs"
PRESETS_DIR.mkdir(parents=True, exist_ok=True)


def create_app(store: VoiceParameterStore) -> "FastAPI":
    if not HAS_FASTAPI:
        raise ImportError("fastapi and uvicorn are required. pip install fastapi uvicorn[standard]")

    app = FastAPI(title="digital-beacon", version="0.2.0")

    # OSC client to the SC beacon engine
    sc_osc = SimpleUDPClient(config.SCLANG_HOST, config.SCLANG_OSC_PORT)

    # Recording manager (records the user's PipeWire monitor = the mix
    # they actually hear). Single global instance — only one recording
    # session at a time.
    recorder = Recorder()

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
            n_active = sum(1 for v in data.get("voices", {}).values() if v.get("active"))
            log.debug("WS push: %d active voices", n_active)
            try:
                asyncio.run_coroutine_threadsafe(ws_mgr.broadcast(data), _loop)
            except Exception:
                log.exception("WS broadcast failed")
        else:
            log.debug("WS push skipped (_loop=%s running=%s)", 
                       bool(_loop), _loop.is_running() if _loop else False)

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

    # ─── REST: Shaper state ──────────────────────────────────────────────
    @app.get("/api/state")
    async def get_state():
        return store.to_dict()

    @app.post("/api/panic")
    async def panic():
        store.panic()
        return {"ok": True, "action": "panic"}

    # ─── REST: SC beacon control (proxy to sclang:57120) ─────────────────
    @app.post("/api/harmonic/{n}/{param}")
    async def set_band_param(n: int, param: str, body: dict):
        """Proxy band control to the SC beacon engine.

        param: gain | az | dist | q | on
        """
        if n < 1 or n > 32:
            raise HTTPException(400, "n must be 1..32")
        if param not in ("gain", "az", "dist", "q", "on"):
            raise HTTPException(400, f"unknown param: {param}")
        addr = f"/beacon/{param}/{n}"
        if param == "on":
            value = 1.0 if body.get("on", True) else 0.0
        else:
            value = float(body.get(param, 0.0))
        sc_osc.send_message(addr, [value])
        return {"ok": True, "n": n, "param": param, "value": value}

    @app.post("/api/reset")
    async def reset():
        sc_osc.send_message("/beacon/reset", [])
        return {"ok": True}

    @app.post("/api/f1")
    async def set_f1(body: dict):
        hz = float(body.get("f1", config.DEFAULT_F1))
        sc_osc.send_message("/beacon/f1", [hz])
        store.update_f1(hz)
        return {"ok": True, "f1": hz}

    @app.post("/api/vsource")
    async def set_vsource(body: dict):
        rate = float(body.get("rate", 1.0))
        sc_osc.send_message("/beacon/vsource", [rate])
        store.set_vsrate(rate)
        return {"ok": True, "rate": rate}

    @app.post("/api/master")
    async def set_master(body: dict):
        value = float(body.get("master", 0.9))
        sc_osc.send_message("/beacon/master", [value])
        return {"ok": True, "master": value}

    # ─── REST: Shaper global control (must come before /{n}/{param}) ───────
    @app.post("/api/shaper/global/{param}")
    async def set_shaper_global(param: str, body: dict):
        """Set global Shaper parameters.

        param: attack | release | master | sidechain | lfo_rate_divisor | lfo_waveform | lfo_amount
        """
        if param == "lfo_waveform":
            store.set_lfo_waveform(body.get(param, "sine"))
            return {"ok": True, "param": param, "value": body.get(param)}
        value = float(body.get(param, 0.0))
        if param == "attack":
            store.set_global_attack(value)
        elif param == "release":
            store.set_global_release(value)
        elif param == "master":
            store.set_master_gain(value)
        elif param == "sidechain":
            store.set_sidechain_amount(value)
        elif param == "lfo_rate_divisor":
            store.set_lfo_rate_divisor(int(value))
        elif param == "lfo_amount":
            store.set_lfo_amount(value)
        else:
            raise HTTPException(400, f"unknown global param: {param}")
        return {"ok": True, "param": param, "value": value}

    # ─── REST: Shaper per-harmonic control ────────────────────────────────
    @app.post("/api/shaper/{n}/{param}")
    async def set_shaper_param(n: int, param: str, body: dict):
        """Set per-harmonic Shaper parameter.

        param: gain | pan | phase_deg | attack_s | release_s | shape | lfo_gain | lfo_pan | lfo_phase
        """
        if n < 1 or n > config.N_BANDS:
            raise HTTPException(400, f"n must be 1..{config.N_BANDS}")
        value = float(body.get(param, 0.0))
        if param == "gain":
            store.set_gain(n, value)
        elif param == "pan":
            store.set_pan(n, value)
        elif param == "phase_deg":
            store.set_phase(n, value)
        elif param == "attack_s":
            store.set_attack(n, value)
        elif param == "release_s":
            store.set_release(n, value)
        elif param == "shape":
            store.set_shape(n, value)
        elif param == "lfo_gain":
            store.set_lfo_gain(n, value)
        elif param == "lfo_pan":
            store.set_lfo_pan(n, value)
        elif param == "lfo_phase":
            store.set_lfo_phase(n, value)
        else:
            raise HTTPException(400, f"unknown param: {param}")
        return {"ok": True, "n": n, "param": param, "value": value}

    # ─── REST: Presets (load/save/list) ──────────────────────────────────
    def _safe_name(name: str) -> str:
        s = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_")
        return s or "preset"

    @app.get("/api/presets")
    async def list_presets():
        files = sorted(p for p in PRESETS_DIR.glob("*.json"))
        return {"ok": True, "presets": [p.stem for p in files]}

    @app.post("/api/presets/save")
    async def save_preset(body: dict):
        name = _safe_name(body.get("name", "").strip())
        if not name:
            return {"ok": False, "error": "No name"}
        state = body.get("state", {})
        # Add a snapshot of the live Shaper state from the server so the
        # preset captures the *actual* values (not just what the client
        # thinks it sent). The client's payload is accepted as-is for the
        # beacon side (the server doesn't track those), but we overlay the
        # Shaper globals + per-voice params from the store.
        live = store.to_dict()
        shaper_section = {
            "master_gain": live.get("master_gain"),
            "global_attack_s": live.get("global_attack_s"),
            "global_release_s": live.get("global_release_s"),
            "sidechain_amount": live.get("sidechain_amount"),
            "lfo_rate_divisor": live.get("lfo_rate_divisor"),
            "lfo_waveform": live.get("lfo_waveform"),
            "lfo_amount": live.get("lfo_amount"),
            "voices": live.get("voices", {}),
        }
        # Beacon section: client-tracked (server is dumb proxy to sclang)
        beacon_section = {
            "f1": state.get("f1"),
            "vsrate": state.get("vsrate"),
            "master": state.get("master"),
            "bands": state.get("bands", []),
        }
        full = {
            "version": 2,
            "saved_at": int(time.time()),
            "beacon": beacon_section,
            "shaper": shaper_section,
        }
        path = PRESETS_DIR / f"{name}.json"
        with open(path, "w") as f:
            json.dump(full, f, indent=2)
        return {
            "ok": True, "name": name, "path": str(path),
            "beacon_bands": len(beacon_section["bands"]),
            "shaper_voices": len(shaper_section["voices"]),
        }

    @app.post("/api/presets/load")
    async def load_preset(body: dict):
        name = body.get("name", "").strip()
        if not name:
            return {"ok": False, "error": "No name"}
        # Try several path strategies in order:
        # 1. Exact filename (preserves spaces and special chars)
        # 2. safe_name (underscores, etc.)
        # 3. As a glob match
        candidates = [
            PRESETS_DIR / f"{name}.json",
            PRESETS_DIR / f"{_safe_name(name)}.json",
        ]
        path = next((p for p in candidates if p.exists()), None)
        if path is None:
            # Last resort: glob match
            matches = list(PRESETS_DIR.glob(f"{_safe_name(name)}*.json"))
            if matches:
                path = matches[0]
        if path is None:
            return {"ok": False, "error": f"Not found: {name}"}
        with open(path) as f:
            state = json.load(f)
        # ── Beacon side (always present, legacy or new format) ────────
        bands = state.get("bands", [])
        if not bands and isinstance(state.get("beacon"), dict):
            bands = state["beacon"].get("bands", [])
        for band in bands:
            n = band.get("n", 0)
            if not (1 <= n <= 32):
                continue
            for param in ("gain", "az", "dist", "q", "on"):
                if param in band:
                    sc_osc.send_message(f"/beacon/{param}/{n}", [float(band[param])])
        # master: try top-level "master" (legacy) then beacon.master (new)
        master_val = state.get("master")
        if master_val is None and isinstance(state.get("beacon"), dict):
            master_val = state["beacon"].get("master")
        if master_val is not None:
            sc_osc.send_message("/beacon/master", [float(master_val)])
        # f1 + vsrate (new format only)
        if isinstance(state.get("beacon"), dict):
            if state["beacon"].get("f1") is not None:
                f1 = float(state["beacon"]["f1"])
                sc_osc.send_message("/beacon/f1", [f1])
                store.update_f1(f1)
            if state["beacon"].get("vsrate") is not None:
                rate = float(state["beacon"]["vsrate"])
                sc_osc.send_message("/beacon/vsource", [rate])
                store.set_vsrate(rate)
        # ── Shaper side (new format only — old presets skip this) ─────
        sh = state.get("shaper")
        if isinstance(sh, dict):
            for gname in ("global_attack_s", "global_release_s", "sidechain_amount",
                          "lfo_amount", "master_gain"):
                if gname in sh and sh[gname] is not None:
                    fn = {
                        "global_attack_s": store.set_global_attack,
                        "global_release_s": store.set_global_release,
                        "sidechain_amount": store.set_sidechain_amount,
                        "lfo_amount": store.set_lfo_amount,
                        "master_gain": store.set_master_gain,
                    }[gname]
                    fn(float(sh[gname]))
            if sh.get("lfo_waveform"):
                store.set_lfo_waveform(sh["lfo_waveform"])
            if sh.get("lfo_rate_divisor") is not None:
                store.set_lfo_rate_divisor(int(sh["lfo_rate_divisor"]))
            for n_str, vp in (sh.get("voices") or {}).items():
                try:
                    n = int(n_str)
                except (ValueError, TypeError):
                    continue
                if not (1 <= n <= config.N_BANDS):
                    continue
                if vp.get("gain") is not None:
                    store.set_gain(n, float(vp["gain"]))
                if vp.get("pan") is not None:
                    store.set_pan(n, float(vp["pan"]))
                if vp.get("phase_deg") is not None:
                    store.set_phase(n, float(vp["phase_deg"]))
                if vp.get("attack_s") is not None:
                    store.set_attack(n, float(vp["attack_s"]))
                if vp.get("release_s") is not None:
                    store.set_release(n, float(vp["release_s"]))
                if vp.get("shape") is not None:
                    store.set_shape(n, float(vp["shape"]))
                if vp.get("lfo_gain") is not None:
                    store.set_lfo_gain(n, float(vp["lfo_gain"]))
                if vp.get("lfo_pan") is not None:
                    store.set_lfo_pan(n, float(vp["lfo_pan"]))
                if vp.get("lfo_phase") is not None:
                    store.set_lfo_phase(n, float(vp["lfo_phase"]))
        return {"ok": True, "name": name, "state": state}

    # ─── REST: Recording (pw-record on PipeWire monitor) ────────────────
    @app.get("/api/record/status")
    async def record_status():
        return recorder.status()

    @app.post("/api/record/start")
    async def record_start(body: Optional[dict] = None):
        name = (body or {}).get("name") or None
        return recorder.start(name)

    @app.post("/api/record/stop")
    async def record_stop():
        return recorder.stop()

    @app.post("/api/record/toggle")
    async def record_toggle(body: Optional[dict] = None):
        name = (body or {}).get("name") or None
        return recorder.toggle(name)

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
