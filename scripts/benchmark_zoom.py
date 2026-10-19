"""Read-only Windows Zoom discovery and cached UIA snapshot benchmark."""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import time
from collections.abc import Callable


def _measure(read: Callable, count: int) -> dict:
    started = time.perf_counter()
    read()
    cold = (time.perf_counter() - started) * 1000
    samples = []
    for _ in range(count):
        started = time.perf_counter()
        read()
        samples.append((time.perf_counter() - started) * 1000)
    return {
        "samples": count,
        "cold_ms": round(cold, 3),
        "p50_ms": round(statistics.median(samples), 3),
        "p95_ms": round(sorted(samples)[math.ceil(count * 0.95) - 1], 3),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=30)
    arguments = parser.parse_args()
    if sys.platform != "win32":
        parser.error("Zoom UI Automation is available only on Windows")
    if not 10 <= arguments.samples <= 1000:
        parser.error("--samples must be between 10 and 1000")
    sys.coinit_flags = 0
    from solin.core.integrations.automation.zoom.controls import Automation, ZoomControls
    from solin.core.integrations.automation.zoom.native import WindowsDesktop

    desktop = WindowsDesktop()
    if desktop.meeting() is None:
        parser.error("Open a Zoom meeting before measuring snapshots")
    automation = Automation()
    result = {
        "discovery": _measure(desktop.meeting, arguments.samples),
        "snapshot": _measure(
            lambda: ZoomControls(desktop, automation).inspect(), arguments.samples
        ),
        "uia_queries": automation.query_count,
    }
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
