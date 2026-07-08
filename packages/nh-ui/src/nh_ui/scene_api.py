"""Scene-aware API routes for NaturalHarmony UI (Phase 8).

Adds scene inspection, preset v2 load/save, source mixer, and
analysis display endpoints alongside the existing field-based API.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional
from fastapi import HTTPException
from pathlib import Path
import os

from nh_core import BeaconSource, HarmonicScene
from nh_presets import PresetV2, load_v2, save_v2, validate_v2
from nh_model import SceneState


# ── Scene state holder (global, set by main) ──────────────────────────────────

_scene_state: Optional[SceneState] = None
_latest_analysis: Optional[Dict[str, Any]] = None
_legacy_control_handler: Optional[Callable[[Dict[str, Any]], None]] = None


def set_scene_state(state: SceneState) -> None:
    global _scene_state
    _scene_state = state


def get_scene_state() -> Optional[SceneState]:
    return _scene_state


def set_legacy_control_handler(handler: Optional[Callable[[Dict[str, Any]], None]]) -> None:
    """Register a handler that receives legacy-compatible control events for audio."""
    global _legacy_control_handler
    _legacy_control_handler = handler


def _path_to_pad_event(path: str, value: Any) -> Optional[Dict[str, Any]]:
    """Convert a v2 path control like sources.shaper.voice_7_toggle to a legacy pad event."""
    if not path.startswith("sources.shaper.voice_"):
        return None
    suffix = path[len("sources.shaper.voice_"):]
    parts = suffix.split("_", 1)
    if len(parts) != 2:
        return None
    try:
        n = int(parts[0])
    except ValueError:
        return None
    action = parts[1]
    if action == "on":
        return {"type": "pad_on", "value": {"n": n, "vel": int(value * 127) if isinstance(value, (int, float)) else 127}}
    elif action == "off":
        return {"type": "pad_off", "value": {"n": n}}
    elif action == "toggle":
        return {"type": "pad_toggle", "value": {"n": n, "active": True}}
    return None


# ── Routes ────────────────────────────────────────────────────────────────────


def register_scene_routes(app) -> None:
    """Register scene-aware routes on the FastAPI app."""

    # Scene preset list
    @app.get("/nh/v2/presets")
    async def list_scene_presets() -> List[Dict[str, Any]]:
        from nh_presets import load_v2 as loader
        presets = []
        data_dir = Path(os.getenv("NH_DATA_DIR",
                                  str(Path(__file__).resolve().parents[4] / "data")))
        presets_dir = Path(os.getenv("NH_PRESETS_DIR",
                                     str(data_dir / "migrated_presets")))
        if not presets_dir.exists():
            return []
        for path in sorted(presets_dir.glob("*.json")):
            try:
                p = loader(str(path))
                n_sources = len(p.scene.sources)
                source_types = [s.kind for s in p.scene.sources.values()]
                presets.append({
                    "id": path.stem,
                    "name": p.metadata.get("name", path.stem),
                    "version": p.version,
                    "n_sources": n_sources,
                    "source_types": source_types,
                })
            except Exception as e:
                presets.append({"id": path.stem, "error": str(e)})
        return presets

    # Scene preset detail
    @app.get("/nh/v2/presets/{preset_id}")
    async def get_scene_preset(preset_id: str):
        data_dir = Path(os.getenv("NH_DATA_DIR",
                                  str(Path(__file__).resolve().parents[4] / "data")))
        presets_dir = Path(os.getenv("NH_PRESETS_DIR",
                                     str(data_dir / "migrated_presets")))
        path = presets_dir / f"{preset_id}.json"
        if not path.exists():
            raise HTTPException(status_code=404, detail="preset not found")
        try:
            p = load_v2(str(path))
            return p.to_dict()
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e))

    # Scene preset load
    @app.post("/nh/v2/presets/{preset_id}/load")
    async def load_scene_preset(preset_id: str):
        if _scene_state is None:
            raise HTTPException(status_code=503, detail="scene state not available")
        data_dir = Path(os.getenv("NH_DATA_DIR",
                                  str(Path(__file__).resolve().parents[4] / "data")))
        presets_dir = Path(os.getenv("NH_PRESETS_DIR",
                                     str(data_dir / "migrated_presets")))
        path = presets_dir / f"{preset_id}.json"
        if not path.exists():
            raise HTTPException(status_code=404, detail="preset not found")
        try:
            p = load_v2(str(path))
            _scene_state.scene = p.scene
            _scene_state.beacons.clear()
            _scene_state.shapers.clear()
            _scene_state.samples.clear()
            for sid, source in p.scene.sources.items():
                if isinstance(source, BeaconSource):
                    _scene_state.beacons[sid] = BeaconRuntime(source_id=sid, vsrate=source.vsrate)
                elif isinstance(source, ShaperSource):
                    _scene_state.shapers[sid] = ShaperRuntime(source_id=sid)
                elif isinstance(source, SampleSource):
                    _scene_state.samples[sid] = SampleRuntime(source_id=sid, loop=source.loop)
            return {"ok": True, "preset_id": preset_id}
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e))

    # Scene snapshot
    @app.get("/nh/v2/scene")
    async def get_scene():
        if _scene_state is None:
            raise HTTPException(status_code=503, detail="scene state not available")
        return _scene_state.scene_snapshot()

    # Source mixer control
    @app.post("/nh/v2/scene/sources/{source_id}/mute")
    async def toggle_mute(source_id: str, data: Dict[str, Any]):
        if _scene_state is None:
            raise HTTPException(status_code=503)
        mute = data.get("mute", False)
        # Apply gain = 0 for mute, 1 for unmute.
        if source_id in _scene_state.beacons:
            _scene_state.beacons[source_id].gain_offset = 0.0 if mute else 1.0
        elif source_id in _scene_state.shapers:
            _scene_state.shapers[source_id].gain_offset = 0.0 if mute else 1.0
        elif source_id in _scene_state.samples:
            _scene_state.samples[source_id].gain_offset = 0.0 if mute else 1.0
        else:
            raise HTTPException(status_code=404, detail="source not found")
        return {"ok": True, "source_id": source_id, "mute": mute}

    # Source solo
    @app.post("/nh/v2/scene/sources/{source_id}/solo")
    async def toggle_solo(source_id: str, data: Dict[str, Any]):
        if _scene_state is None:
            raise HTTPException(status_code=503)
        solo = data.get("solo", False)
        for sid, br in _scene_state.beacons.items():
            br.gain_offset = (1.0 if sid == source_id else 0.0) if solo else 1.0
        for sid, sr in _scene_state.shapers.items():
            sr.gain_offset = (1.0 if sid == source_id else 0.0) if solo else 1.0
        for sid, sm in _scene_state.samples.items():
            sm.gain_offset = (1.0 if sid == source_id else 0.0) if solo else 1.0
        return {"ok": True, "source_id": source_id, "solo": solo}

    # Scene control (path-based)
    @app.post("/nh/v2/scene/control")
    async def scene_control(event: Dict[str, Any]):
        if _scene_state is None:
            raise HTTPException(status_code=503)
        _scene_state.apply_control(event)

        # Mirror pad controls to the legacy runtime so audio responds immediately.
        path = event.get("path")
        if path and _legacy_control_handler is not None:
            pad_event = _path_to_pad_event(path, event.get("value", 1.0))
            if pad_event is not None:
                try:
                    _legacy_control_handler(pad_event)
                except Exception:
                    pass

        return {"ok": True}

    # Analysis result display
    @app.get("/nh/v2/analysis")
    async def get_latest_analysis():
        return {"analysis": _latest_analysis}

    @app.post("/nh/v2/analysis/mock")
    async def set_mock_analysis(data: Dict[str, Any]):
        """Store a UI-testable analysis result.

        This is the integration seam for the real analyzer: callers can submit an
        AnalysisResult-shaped payload and the UI renders it immediately.
        """
        global _latest_analysis
        _latest_analysis = {
            "audio_path": data.get("audio_path", "mock://field-recording.wav"),
            "duration_s": float(data.get("duration_s", 3.0)),
            "f0_track": data.get("f0_track", {"f0_mean": 110.0, "voiced_fraction": 0.9}),
            "phideus": data.get("phideus", {"h_series": {"concentration": 0.72, "deviation": 0.08}}),
            "proposed_f1": float(data.get("proposed_f1", 55.0)),
            "sample_source": data.get("sample_source", {"source_id": "field_recording", "kind": "sample"}),
        }
        return {"ok": True, "analysis": _latest_analysis}

    @app.post("/nh/v2/analysis/apply-proposed-f1")
    async def apply_proposed_f1():
        if _scene_state is None:
            raise HTTPException(status_code=503, detail="scene state not available")
        if not _latest_analysis or _latest_analysis.get("proposed_f1") is None:
            raise HTTPException(status_code=404, detail="analysis proposed_f1 not available")
        proposed = float(_latest_analysis["proposed_f1"])
        for sid, source in _scene_state.scene.sources.items():
            if isinstance(source, BeaconSource):
                source.f1 = proposed
                if sid in _scene_state.beacons:
                    _scene_state.beacons[sid].f1_offset = 0.0
                return {"ok": True, "source_id": sid, "f1": proposed}
        raise HTTPException(status_code=404, detail="beacon source not found")

    @app.get("/nh/v2/analysis/{sample_id}")
    async def get_analysis(sample_id: str):
        data_dir = Path(os.getenv("NH_DATA_DIR",
                                  str(Path(__file__).resolve().parents[4] / "data")))
        analysis_dir = data_dir / "analysis"
        path = analysis_dir / f"{sample_id}.json"
        if not path.exists():
            raise HTTPException(status_code=404, detail="analysis not found")
        import json
        with open(path, "r") as f:
            return json.load(f)

