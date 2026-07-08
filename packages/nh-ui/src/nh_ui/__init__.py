from nh_ui.server import app, make_app, set_runtime_server
from nh_ui.scene_api import (
    register_scene_routes,
    set_scene_state,
    get_scene_state,
)

# Wire scene routes into the FastAPI app (Phase 8 — scene-aware UI).
register_scene_routes(app)

__all__ = [
    "app",
    "make_app",
    "set_runtime_server",
    "register_scene_routes",
    "set_scene_state",
    "get_scene_state",
]
