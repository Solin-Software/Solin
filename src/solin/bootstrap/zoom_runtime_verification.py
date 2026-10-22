"""Headless qualification of the delivered Windows UI Automation runtime."""

from __future__ import annotations

import json
import sys
from collections.abc import Sequence

_SCHEMA = "solin.zoom-runtime.v1"


def _verify() -> None:
    if sys.platform != "win32":
        raise RuntimeError("Zoom runtime qualification is available only on Windows.")

    from solin.core.integrations.automation.zoom.controls import Automation

    # The constructor qualifies COM, bindings, timeouts and the property cache.
    # Do not create ZoomControls or WindowsDesktop: no meeting is required.
    Automation()


def main(arguments: Sequence[str]) -> int:
    """Initialize the native client without Qt, window discovery or user input."""
    result: dict[str, object] = {"schema": _SCHEMA, "ok": False}
    exit_code = 1
    try:
        if list(arguments) != ["--verify-zoom-runtime"]:
            raise ValueError("Usage: --verify-zoom-runtime")
    except ValueError as error:
        result["error"] = type(error).__name__
        print(str(error), file=sys.stderr, flush=True)
        exit_code = 2
    else:
        try:
            _verify()
        except Exception as error:  # noqa: BLE001 - qualification process boundary includes COM and import failures
            result["error"] = type(error).__name__
            print(str(error), file=sys.stderr, flush=True)
        else:
            result["ok"] = True
            exit_code = 0
    print(json.dumps(result, separators=(",", ":")), flush=True)
    return exit_code
