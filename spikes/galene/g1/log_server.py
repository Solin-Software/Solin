"""Serves a web directory and stores the JSON lines its pages post to /log.

Usage: LOG_NAME=g1_ffmpeg [WEB=../g2/web] [PORT=8091] python3 log_server.py
"""

import os
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
WEB = Path(os.environ.get("WEB", HERE / "web")).resolve()
RESULTS = HERE.parent / "results"
LOG = RESULTS / f"{os.environ.get('LOG_NAME', 'g1_probe')}_{time.strftime('%Y%m%d_%H%M%S')}.jsonl"


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB), **kwargs)

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        RESULTS.mkdir(exist_ok=True)
        with LOG.open("ab") as f:
            f.write(body.rstrip(b"\n") + b"\n")
        self.send_response(204)
        self._cors()
        self.end_headers()

    def log_message(self, *_):
        pass


if __name__ == "__main__":
    print(f"log: {LOG}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", int(os.environ.get("PORT", 8091))), Handler).serve_forever()
