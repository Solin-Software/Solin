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
    parser.add_argument(
        "--raised-hands", action="store_true", help="Also read the open Participants panel"
    )
    arguments = parser.parse_args()
    if sys.platform != "win32":
        parser.error("Zoom UI Automation is available only on Windows")
    if not 10 <= arguments.samples <= 1000:
        parser.error("--samples must be between 10 and 1000")
    sys.coinit_flags = 0
    import ctypes

    from solin.core.integrations.automation.zoom.controls import Automation, ZoomControls
    from solin.core.integrations.automation.zoom.native import WindowsDesktop
    from solin.core.integrations.automation.zoom.participants import ZoomRaisedHands
    from solin.core.integrations.automation.zoom.state import ZoomAutomationError

    desktop = WindowsDesktop()
    desktop.user.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
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
    if arguments.raised_hands:
        monitor = ZoomRaisedHands()
        before = automation.query_count
        status = ""

        def read_hands() -> None:
            nonlocal status
            snapshot = monitor.snapshot(ZoomControls(desktop, automation), open_if_closed=False)
            status = snapshot.status.value

        try:
            timings = _measure(read_hands, arguments.samples)
        except ZoomAutomationError as error:
            parser.error(f"Open a readable Zoom Participants panel: {error.code}")
        result["raised_hands"] = {
            **timings,
            "status": status,
            "uia_queries_per_read": round(
                (automation.query_count - before) / (arguments.samples + 1), 2
            ),
        }
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
