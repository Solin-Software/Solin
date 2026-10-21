"""Serves the A/V probe page, hands out dev tokens and stores probe measurements.

Spike code. Usage:
    .venv/bin/python s1_publish/probe_server.py   # then open http://127.0.0.1:8090/
"""

from __future__ import annotations

import json
import os
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from livekit import api

HERE = Path(__file__).resolve().parent
WEB = HERE / "web"
RESULTS = HERE.parent / "results"
LOG = RESULTS / f"{os.environ.get('LOG_NAME', 'av_probe')}_{time.strftime('%Y%m%d_%H%M%S')}.jsonl"


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB), **kwargs)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path != "/token":
            return super().do_GET()
        q = parse_qs(url.query)
        token = (
            api.AccessToken("devkey", "secret")
            .with_identity(q.get("identity", ["probe"])[0])
            .with_grants(api.VideoGrants(room_join=True, room=q.get("room", ["s1"])[0], can_publish=False))
            .to_jwt()
        )
        self._json({"token": token, "url": "ws://127.0.0.1:7880"})

    def do_POST(self):
        if self.path != "/log":
            self.send_error(404)
            return
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        RESULTS.mkdir(exist_ok=True)
        with LOG.open("ab") as f:
            f.write(body.rstrip(b"\n") + b"\n")
        self._json({"ok": True})

    def _json(self, payload: dict) -> None:
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_):
        pass


if __name__ == "__main__":
    print(f"probe page: http://127.0.0.1:8090/  (log: {LOG})", flush=True)
    ThreadingHTTPServer(("127.0.0.1", 8090), Handler).serve_forever()
