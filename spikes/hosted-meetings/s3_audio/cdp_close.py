"""Close a Chromium started with --remote-debugging-port via the DevTools protocol.

Signals can't reach snap-confined browsers from a confined shell; a DevTools
`Browser.close` over localhost works regardless.

    python cdp_close.py 9333
"""

import asyncio
import sys

import aiohttp


async def main(port: int) -> None:
    async with aiohttp.ClientSession() as session:
        async with session.get(f"http://127.0.0.1:{port}/json/version") as r:
            ws_url = (await r.json())["webSocketDebuggerUrl"]
        async with session.ws_connect(ws_url) as ws:
            await ws.send_json({"id": 1, "method": "Browser.close"})
            try:
                await asyncio.wait_for(ws.receive(), 3)
            except asyncio.TimeoutError:
                pass


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1])))
