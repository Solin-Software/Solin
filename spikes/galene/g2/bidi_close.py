"""Close a Firefox started with --remote-debugging-port via WebDriver BiDi.

Signals can't reach snap-confined browsers from a confined shell.

    python3 bidi_close.py 9335
"""

import asyncio
import json
import sys

import websockets


async def main(port: int) -> None:
    async with websockets.connect(f"ws://127.0.0.1:{port}/session") as ws:
        await ws.send(json.dumps({"id": 1, "method": "session.new", "params": {"capabilities": {}}}))
        await ws.recv()
        await ws.send(json.dumps({"id": 2, "method": "browser.close", "params": {}}))
        try:
            await asyncio.wait_for(ws.recv(), 3)
        except (asyncio.TimeoutError, websockets.ConnectionClosed):
            pass


asyncio.run(main(int(sys.argv[1])))
