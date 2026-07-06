"""NaturalHarmony UI host."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, File, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

from nh_core import HarmonicField
from nh_presets import Preset, load, save, validate
from nh_runtime import BaseFieldServer
from nh_runtime.transport import TransportMessage

STATIC_DIR = Path(__file__).parent / "static"
PRESETS_DIR = Path("/home/nicolas/Projects/digital-beacon/data/migrated_presets")
UPLOAD_DIR = Path("/home/nicolas/Projects/digital-beacon/data/uploads")

app = FastAPI(title="NaturalHarmony UI")

# The runtime server is managed externally and injected here.
_runtime_server: Optional[BaseFieldServer] = None


def set_runtime_server(server: BaseFieldServer) -> None:
    global _runtime_server
    _runtime_server = server


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
                "name": p.harmonic_field.metadata.get("name", path.stem),
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


@app.websocket("/nh/v1/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    if _runtime_server is None:
        await websocket.send_json({"type": "error", "payload": {"code": "runtime_unavailable", "message": "runtime server not set"}})
        await websocket.close()
        return

    runtime = _runtime_server
    runtime.clients.add(websocket)
    try:
        await _send_capabilities(websocket, runtime)
        await _send_field(websocket, runtime)
        while True:
            data = await websocket.receive_text()
            try:
                msg = TransportMessage.from_json(data)
                await _handle_client_message(websocket, runtime, msg)
            except Exception as e:
                await websocket.send_json(TransportMessage("error", {"code": "parse_error", "message": str(e)}).to_dict())
    except WebSocketDisconnect:
        pass
    finally:
        runtime.clients.discard(websocket)


async def _send_capabilities(websocket, runtime: BaseFieldServer):
    msg = TransportMessage("renderer_capabilities", runtime.renderer_capabilities.to_dict())
    await websocket.send_text(msg.to_json())


async def _send_field(websocket, runtime: BaseFieldServer):
    snapshot = runtime.model.to_snapshot()
    msg = TransportMessage("base_field", snapshot.to_dict())
    await websocket.send_text(msg.to_json())


async def _handle_client_message(websocket, runtime: BaseFieldServer, msg: TransportMessage):
    if msg.type == "control_event":
        runtime.model.apply_control(msg.payload)
    elif msg.type == "sensor_event":
        runtime.model.apply_sensor(msg.payload, runtime.sensor_mapping)
    elif msg.type == "ping":
        await websocket.send_text(TransportMessage("pong", {}).to_json())


def make_app(runtime_server: Optional[BaseFieldServer] = None) -> FastAPI:
    if runtime_server is not None:
        set_runtime_server(runtime_server)
    return app


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8080)
