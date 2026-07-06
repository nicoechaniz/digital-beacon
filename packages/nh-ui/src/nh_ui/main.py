"""Launcher for nh-ui host + runtime server."""
import asyncio

from nh_core import HarmonicField, RendererCapabilities
from nh_runtime import BaseFieldServer
from nh_ui.server import set_runtime_server
import uvicorn

async def main():
    runtime = BaseFieldServer(
        base_field=HarmonicField(f1=65.0),
        host="127.0.0.1",
        port=8765,
        update_hz=10.0,
        renderer_capabilities=RendererCapabilities(
            max_partials=32,
            supports_phase=True,
            supports_spatial=True,
        ),
        sensor_mapping={
            "muse_focus": {"param": "master_gain", "scale": 1.0, "offset": 0.0},
            "imu.orientation.yaw": {"param": "spatial_rotation", "scale": 1.0, "offset": 0.0},
            "imu.orientation.pitch": {"param": "f1_offset", "scale": 0.5, "offset": 0.0},
        },
    )
    await runtime.start()
    set_runtime_server(runtime)
    config = uvicorn.Config("nh_ui.server:app", host="127.0.0.1", port=8080, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()

if __name__ == "__main__":
    asyncio.run(main())
