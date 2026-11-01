"""Qualify the delivered Cocoa presenter and CoreMediaIO native loader."""

from __future__ import annotations

import json
import sys
from collections.abc import Sequence


def _verify() -> None:
    if sys.platform != "darwin":
        raise RuntimeError("macOS runtime qualification is available only on macOS.")

    from pylibobs.macos_camera import _native as load_camera
    from PySide6.QtWidgets import QApplication

    from solin.widgets.projection.native_surface import NativeVideoSurface

    # Resolve the same native ABI used by CameraOutput, without requiring an
    # installed camera extension or claiming a device from another application.
    load_camera()
    app = QApplication(["Solin"])
    if app.platformName() != "cocoa":
        raise RuntimeError("Native presentation qualification requires the Cocoa Qt backend.")
    surface = NativeVideoSurface()
    try:
        surface.resize(64, 64)
        if not surface.surface_endpoint:
            raise RuntimeError("The packaged native presenter did not create a surface endpoint.")
        app.processEvents()
    finally:
        surface.close()
        app.processEvents()


def main(arguments: Sequence[str]) -> int:
    """Run before application services, profiles and settings are initialized."""
    result: dict[str, object] = {"schema": "solin.macos-runtime.v1", "ok": False}
    exit_code = 1
    if list(arguments) != ["--verify-macos-runtime"]:
        result["error"] = "ValueError"
        print("Usage: --verify-macos-runtime", file=sys.stderr, flush=True)
        exit_code = 2
    else:
        try:
            _verify()
        except Exception as error:  # noqa: BLE001 - isolated native qualification boundary
            result["error"] = type(error).__name__
            print(str(error), file=sys.stderr, flush=True)
        else:
            result["ok"] = True
            exit_code = 0
    print(json.dumps(result, separators=(",", ":")), flush=True)
    return exit_code
