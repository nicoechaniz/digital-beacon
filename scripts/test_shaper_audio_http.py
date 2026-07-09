#!/usr/bin/env python3
"""HTTP shaper audio test: send path control via HTTP and render locally."""
import asyncio, requests, time
from nh_runtime import LocalModelClient
from nh_renderers import PythonSounddeviceRenderer

async def main():
    renderer = PythonSounddeviceRenderer(sr=48000, block_size=512)
    client = LocalModelClient(uri='ws://127.0.0.1:8765', renderer=renderer)
    await client.start()
    print('client connected, renderer started')
    
    # Send control via HTTP (same as UI)
    r = requests.post('http://127.0.0.1:8080/nh/v2/scene/control', json={"path": "sources.shaper.voice_7_toggle", "value": 1.0})
    print('HTTP toggle status:', r.status_code)
    time.sleep(0.2)
    
    # Listen for 2 seconds
    await asyncio.sleep(2)
    
    # Toggle off
    r = requests.post('http://127.0.0.1:8080/nh/v2/scene/control', json={"path": "sources.shaper.voice_7_toggle", "value": 0.0})
    print('HTTP toggle off status:', r.status_code)
    await asyncio.sleep(0.5)
    
    await client.stop()
    print('done')

asyncio.run(main())
