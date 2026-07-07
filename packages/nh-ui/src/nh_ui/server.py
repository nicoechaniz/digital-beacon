"""NaturalHarmony UI host."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set, Union

from fastapi import FastAPI, File, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

from nh_core import HarmonicField
from nh_presets import Preset, load, save, validate
from nh_runtime import BaseFieldServer
from nh_runtime.transport import TransportMessage

STATIC_DIR = Path(__file__).parent / "static"
# Repo layout: <root>/packages/nh-ui/src/nh_ui/server.py -> parents[4] == <root>.
# Paths are overridable via env so the host is portable across checkouts.
_REPO_ROOT = Path(__file__).resolve().parents[4]
_DATA_DIR = Path(os.getenv("NH_DATA_DIR", str(_REPO_ROOT / "data")))
PRESETS_DIR = Path(os.getenv("NH_PRESETS_DIR", str(_DATA_DIR / "migrated_presets")))
UPLOAD_DIR = Path(os.getenv("NH_UPLOAD_DIR", str(_DATA_DIR / "uploads")))

app = FastAPI(title="NaturalHarmony UI")
app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")

# The runtime server is managed externally and injected here.
_runtime_server: Optional[BaseFieldServer] = None

# UI WebSocket clients connected to the FastAPI host. We mirror the runtime's
# base-field broadcast to them so the SPA stays in sync.
_ui_clients: Set[WebSocket] = set()
_ui_broadcast_task: Optional[asyncio.Task] = None

# Renderer selection is managed externally. The UI can read and write it.
_current_renderer: str = "python"
RendererCallback = Callable[[str], Union[None, Awaitable[None]]]
_renderer_changed_callback: Optional[RendererCallback] = None

# Optional callback for launchpad LED / external mirroring of controls (set by main)
_launchpad_control_handler: Optional[Callable[[Dict[str, Any]], None]] = None

# Event loop running the UI server. Captured so control-event broadcasts issued
# from other threads (e.g. the Launchpad MIDI reader) can be scheduled safely.
_ui_loop: Optional[asyncio.AbstractEventLoop] = None

def set_ui_loop(loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
    """Register the event loop that runs the UI server.

    ``main`` calls this explicitly so off-loop threads have a reliable target for
    thread-safe scheduling. When called without an argument it captures the
    currently running loop, which is how the WebSocket endpoint and tests bind it.
    """
    global _ui_loop
    if loop is not None:
        _ui_loop = loop
        return
    try:
        _ui_loop = asyncio.get_running_loop()
    except RuntimeError:
        pass


def set_runtime_server(server: BaseFieldServer) -> None:
    global _runtime_server
    _runtime_server = server


def set_renderer_changed_callback(callback: Optional[RendererCallback]) -> None:
    global _renderer_changed_callback
    _renderer_changed_callback = callback


def set_launchpad_control_handler(handler: Optional[Callable[[Dict[str, Any]], None]]) -> None:
    """Register handler for control events (e.g. to drive Launchpad LED feedback from any source)."""
    global _launchpad_control_handler
    _launchpad_control_handler = handler


def get_renderer_selection() -> str:
    return _current_renderer


def set_renderer_selection(renderer: str) -> None:
    global _current_renderer
    if renderer not in ("webaudio", "python"):
        raise ValueError(f"unsupported renderer: {renderer}")
    _current_renderer = renderer
    if _renderer_changed_callback is not None:
        result = _renderer_changed_callback(renderer)
        if asyncio.iscoroutine(result):
            asyncio.create_task(result)


@app.get("/")
async def root():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/nh/v1/presets")
async def list_presets() -> List[Dict[str, Any]]:
    presets = []
    if not PRESETS_DIR.exists():
        return []
    for path in sorted(PRESETS_DIR.glob("*.json")):
        try:
            p = load(str(path))
            presets.append({
                "id": path.stem,
                "name": p.metadata.get("name", path.stem),
                "version": p.version,
                "f1": p.harmonic_field.f1,
                "n_partials": len(p.harmonic_field.partials),
            })
        except Exception as e:
            presets.append({"id": path.stem, "error": str(e)})
    return presets


@app.get("/nh/v1/presets/{preset_id}")
async def get_preset(preset_id: str):
    path = PRESETS_DIR / f"{preset_id}.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="preset not found")
    try:
        p = load(str(path))
        return p.to_dict()
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/nh/v1/presets/{preset_id}/load")
async def load_preset(preset_id: str):
    path = PRESETS_DIR / f"{preset_id}.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="preset not found")
    try:
        p = load(str(path))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    if _runtime_server is None:
        raise HTTPException(status_code=503, detail="runtime server not available")
    _runtime_server.update_base_field(p.harmonic_field)
    await _runtime_server._broadcast_field()
    return {"ok": True, "preset_id": preset_id, "f1": p.harmonic_field.f1}


@app.post("/nh/v1/presets")
async def create_preset(data: Dict[str, Any]):
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    try:
        p = Preset.from_dict(data)
        errors = validate(p)
        if errors:
            raise HTTPException(status_code=400, detail={"errors": errors})
        name = p.harmonic_field.metadata.get("name", "untitled")
        path = UPLOAD_DIR / f"{name}.json"
        save(p, str(path))
        return {"ok": True, "path": str(path)}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/nh/v1/analyze")
async def analyze_wav(file: UploadFile = File(...)):
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    dest = UPLOAD_DIR / file.filename
    with open(dest, "wb") as f:
        content = await file.read()
        f.write(content)
    # Placeholder; analysis will be wired in M2.
    return {"ok": True, "path": str(dest), "f1": None, "note": "analysis wired in M2"}


@app.get("/nh/v1/renderer")
async def get_renderer() -> Dict[str, str]:
    return {"renderer": _current_renderer}


@app.post("/nh/v1/renderer")
async def set_renderer(data: Dict[str, Any]):
    renderer = data.get("renderer")
    if renderer not in ("webaudio", "python"):
        raise HTTPException(status_code=400, detail="renderer must be 'webaudio' or 'python'")
    try:
        set_renderer_selection(renderer)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "renderer": renderer}


@app.websocket("/nh/v1/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    # Bind the UI loop so cross-thread control broadcasts land on this loop.
    set_ui_loop()
    if _runtime_server is None:
        await websocket.send_json({"type": "error", "payload": {"code": "runtime_unavailable", "message": "runtime server not set"}})
        await websocket.close()
        return

    runtime = _runtime_server
    _ui_clients.add(websocket)
    _ensure_ui_broadcast_loop()
    try:
        await _send_capabilities(websocket, runtime)
        await _send_field(websocket, runtime)
        while True:
            data = await websocket.receive_text()
            try:
                msg = TransportMessage.from_json(data)
                await _handle_client_message(websocket, runtime, msg)
                # Eager feedback so the UI reacts without waiting for the next
                # broadcast tick.
                await _send_field_to_clients()
            except Exception as e:
                await websocket.send_json(TransportMessage("error", {"code": "parse_error", "message": str(e)}).to_dict())
    except WebSocketDisconnect:
        pass
    finally:
        _ui_clients.discard(websocket)
        if not _ui_clients:
            _stop_ui_broadcast_loop()


async def _send_capabilities(websocket, runtime: BaseFieldServer):
    msg = TransportMessage("renderer_capabilities", runtime.renderer_capabilities.to_dict())
    await websocket.send_text(msg.to_json())


async def _send_field(websocket, runtime: BaseFieldServer):
    snapshot = runtime.model.to_snapshot()
    msg = TransportMessage("base_field", snapshot.to_dict())
    await websocket.send_text(msg.to_json())


async def _handle_client_message(websocket, runtime: BaseFieldServer, msg: TransportMessage):
    if msg.type == "control_event":
        etype = msg.payload.get("type")
        if etype == "select_renderer":
            selected = msg.payload.get("value")
            available = getattr(runtime.renderer_capabilities, "available_renderers", None) or []
            if selected and available and selected in available:
                try:
                    set_renderer_selection(selected)
                except ValueError as exc:
                    await websocket.send_text(TransportMessage("error", {"code": "invalid_renderer", "message": str(exc)}).to_json())
                    return
                await websocket.send_text(TransportMessage("renderer_selected", {"renderer": selected}).to_json())
            else:
                await websocket.send_text(TransportMessage("error", {"code": "invalid_renderer", "message": f"renderer {selected} not available"}).to_json())
            return
        # Pad events (pad_on/pad_off/pad_toggle) map to partial gains inside the
        # model, so physical-controller and web-originated controls behave the same.
        runtime.model.apply_control(msg.payload)
        # Drive launchpad handler (for LED feedback from UI-initiated controls like panic)
        if _launchpad_control_handler is not None:
            try:
                _launchpad_control_handler(msg.payload)
            except Exception:
                pass
        # Broadcast original control (esp pads/panic) so UI mirrors update from any source
        if etype in ("pad_on", "pad_off", "pad_toggle", "panic"):
            broadcast_control_event(msg.payload)
    elif msg.type == "sensor_event":
        runtime.model.apply_sensor(msg.payload, runtime.sensor_mapping)
    elif msg.type == "ping":
        await websocket.send_text(TransportMessage("pong", {}).to_json())


async def _send_field_to_clients():
    """Broadcast the current base-field snapshot to every UI WebSocket client."""
    if not _ui_clients or _runtime_server is None:
        return
    snapshot = _runtime_server.model.to_snapshot()
    msg = TransportMessage("base_field", snapshot.to_dict())
    payload = msg.to_json()
    disconnected = []
    for client in _ui_clients:
        try:
            await client.send_text(payload)
        except Exception:
            disconnected.append(client)
    for client in disconnected:
        _ui_clients.discard(client)


async def _ui_broadcast_loop():
    """Periodic mirror of the runtime base field to UI clients."""
    try:
        while True:
            await asyncio.sleep(0.1)
            await _send_field_to_clients()
    except asyncio.CancelledError:
        pass


def _ensure_ui_broadcast_loop():
    """Start the UI broadcast task if it is not already running."""
    global _ui_broadcast_task
    if _ui_broadcast_task is None or _ui_broadcast_task.done():
        _ui_broadcast_task = asyncio.create_task(_ui_broadcast_loop())


def _stop_ui_broadcast_loop():
    """Cancel the UI broadcast task when no clients remain."""
    global _ui_broadcast_task
    if _ui_broadcast_task is not None and not _ui_broadcast_task.done():
        _ui_broadcast_task.cancel()
    _ui_broadcast_task = None


def broadcast_control_event(payload: Dict[str, Any]) -> None:
    """Broadcast a control_event (e.g. pad_toggle from launchpad) to all UI WS clients for mirrors.

    Safe to call from any thread (uses threadsafe scheduling when needed).
    """
    set_ui_loop()
    if not _ui_clients:
        return
    msg = TransportMessage("control_event", payload)
    payload_json = msg.to_json()
    disconnected = []

    def _send() -> None:
        for client in list(_ui_clients):
            try:
                asyncio.create_task(client.send_text(payload_json))
            except Exception:
                disconnected.append(client)
        for client in disconnected:
            _ui_clients.discard(client)

    try:
        if _ui_loop is not None and asyncio.get_running_loop() is not _ui_loop:
            _ui_loop.call_soon_threadsafe(_send)
        else:
            _send()
    except RuntimeError:
        if _ui_loop is not None:
            _ui_loop.call_soon_threadsafe(_send)


def make_app(runtime_server: Optional[BaseFieldServer] = None) -> FastAPI:
    if runtime_server is not None:
        set_runtime_server(runtime_server)
    return app


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8080)
