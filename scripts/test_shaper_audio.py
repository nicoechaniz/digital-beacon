#!/usr/bin/env python3
"""Direct shaper audio test: connect to runtime, toggle pad 7, play 2s, release."""
import asyncio, json, time, websockets
from nh_core import HarmonicScene, BeaconSource, ShaperSource, SpatialBand
from nh_model import SceneState
from nh_runtime import LocalModelClient
from nh_renderers import PythonSounddeviceRenderer

async def main():
    renderer = PythonSounddeviceRenderer(sr=48000, block_size=512)
    client = LocalModelClient(uri='ws://127.0.0.1:8765', renderer=renderer)
    await client.start()
    print('client connected, renderer started')
    await client.send_control({'type': 'pad_toggle', 'value': {'n': 7, 'active': True}})
    print('pad 7 toggled ON')
    await asyncio.sleep(2)
    await client.send_control({'type': 'pad_toggle', 'value': {'n': 7, 'active': False}})
    print('pad 7 toggled OFF')
    await asyncio.sleep(0.5)
    await client.stop()
    print('done')

asyncio.run(main())
