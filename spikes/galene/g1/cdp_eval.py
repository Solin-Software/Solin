"""Evaluate a JS expression in the first page of a Chromium started with --remote-debugging-port.

    python cdp_eval.py 9334 "document.getElementById('panel').textContent"
"""

import asyncio
import json
import sys

import aiohttp


async def main(port: int, expr: str) -> None:
    async with aiohttp.ClientSession() as session:
        async with session.get(f"http://127.0.0.1:{port}/json") as r:
            page = next(t for t in await r.json() if t["type"] == "page")
        async with session.ws_connect(page["webSocketDebuggerUrl"]) as ws:
            await ws.send_json({"id": 1, "method": "Runtime.evaluate", "params": {"expression": expr, "returnByValue": True, "awaitPromise": True}})
            msg = json.loads((await ws.receive()).data)
            res = msg["result"]; print(res["result"].get("value", res.get("exceptionDetails", res)))


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]), sys.argv[2]))
