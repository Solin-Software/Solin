#!/usr/bin/env python3
"""A stand-in for ``solin-galene-bridge publish``: counts framed stdin, reports to a file.

Writes ``{"argv", "video", "pcm", "bytes"}`` to ``$FAKE_BRIDGE_REPORT`` every 0.25 s and
prints the bridge's JSON state line once the first frame arrives.
"""

import json
import os
import struct
import sys
import time

report_path = os.environ.get("FAKE_BRIDGE_REPORT", "")
counts = {"argv": sys.argv[1:], "video": 0, "pcm": 0, "bytes": 0}
announced = False
last = 0.0
stream = sys.stdin.buffer
while True:
    header = stream.read(13)
    if len(header) < 13:
        break
    kind, _pts, length = struct.unpack(">BqI", header)
    stream.read(length)
    counts["bytes"] += 13 + length
    counts["video" if kind == 1 else "pcm"] += 1
    if not announced:
        announced = True
        print(json.dumps({"event": "state", "state": "connected"}), flush=True)
    if report_path and time.monotonic() - last > 0.25:
        last = time.monotonic()
        with open(report_path + ".tmp", "w") as handle:
            json.dump(counts, handle)
        os.replace(report_path + ".tmp", report_path)
if report_path:
    with open(report_path, "w") as handle:
        json.dump(counts, handle)
