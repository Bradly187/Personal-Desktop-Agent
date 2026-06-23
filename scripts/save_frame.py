"""Minimal WS server: receive one camera_frame from the sidecar and save it."""
import asyncio, base64, json, sys
import numpy as np
import cv2
import aiohttp
from aiohttp import web

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8800
OUT = sys.argv[2] if len(sys.argv) > 2 else "logs/debug_frame.jpg"

saved = asyncio.Event()

async def ws_handler(request):
    ws = web.WebSocketResponse(max_msg_size=16*1024*1024)
    await ws.prepare(request)
    print(f"Sidecar connected")
    async for raw in ws:
        if raw.type != web.WSMsgType.TEXT:
            continue
        try:
            msg = json.loads(raw.data)
        except Exception:
            continue
        if msg.get("type") == "camera_frame":
            img_bytes = base64.b64decode(msg["image_b64"])
            arr = np.frombuffer(img_bytes, dtype="u1")
            bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if bgr is not None:
                cv2.imwrite(OUT, bgr)
                print(f"Saved {bgr.shape[1]}x{bgr.shape[0]} frame -> {OUT}")
                saved.set()
                return ws
    return ws

async def main():
    app = web.Application()
    app.router.add_get("/ws", ws_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", PORT)
    await site.start()
    print(f"Waiting for sidecar on ws://127.0.0.1:{PORT}/ws ...")
    await asyncio.wait_for(saved.wait(), timeout=30)
    await runner.cleanup()

asyncio.run(main())
