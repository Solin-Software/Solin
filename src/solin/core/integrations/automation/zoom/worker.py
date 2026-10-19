"""Private stdin/stdout worker; no Qt, screen sharing, or participant enumeration."""

from __future__ import annotations

import ctypes
import json
import logging
import os
import sys
import threading
import time
from dataclasses import asdict

from .sequences import SEQUENCES, run_sequence
from .state import ZoomAutomationError
from .protocol import MAX_MESSAGE_BYTES, PROTOCOL_VERSION

log = logging.getLogger(__name__)


def _write(message: dict) -> None:
    sys.stdout.write(json.dumps(message, ensure_ascii=True, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _watch_parent() -> None:
    """A parent crash must not leave an automation process holding input/COM."""
    if sys.platform != "win32":
        raise ZoomAutomationError("unsupported_platform")
    from ctypes import wintypes as wt

    pid = int(os.environ.get("SOLIN_ZOOM_PARENT_PID", "0"))
    if not pid:
        return
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    kernel.OpenProcess.restype = wt.HANDLE
    kernel.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
    kernel.WaitForSingleObject.restype = wt.DWORD
    kernel.CloseHandle.argtypes = [wt.HANDLE]
    kernel.CloseHandle.restype = wt.BOOL
    handle = kernel.OpenProcess(0x00100000, False, pid)
    if not handle:
        raise ZoomAutomationError("parent_not_found")

    def wait():
        try:
            kernel.WaitForSingleObject(handle, 0xFFFFFFFF)
            os._exit(0)
        finally:
            kernel.CloseHandle(handle)

    threading.Thread(target=wait, name="zoom-parent-watch", daemon=True).start()


def main() -> int:
    if sys.platform != "win32":
        return 1
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    sys.coinit_flags = 0  # pyright: ignore[reportAttributeAccessIssue] - comtypes' sys extension
    ole = ctypes.OleDLL("ole32")
    ole.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    ole.CoInitializeEx.restype = ctypes.c_long
    result = ole.CoInitializeEx(None, 0)
    if result not in (0, 1):
        return 1
    desktop = None
    try:
        _watch_parent()
        from .native import WindowsDesktop
        from .controls import Automation, ZoomControls

        desktop = WindowsDesktop()
        # All coordinates used by UIA and SendInput are physical pixels.
        desktop.user.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
        automation = None
        _write({"ready": PROTOCOL_VERSION})
        while line := sys.stdin.buffer.readline(MAX_MESSAGE_BYTES + 1):
            if len(line) > MAX_MESSAGE_BYTES:
                return 2
            request = json.loads(line)
            if not isinstance(request, dict) or set(request) != {"id", "command"}:
                return 2
            request_id, command = request["id"], request["command"]
            if (
                type(request_id) is not int
                or request_id <= 0
                or command not in SEQUENCES | {"meeting", "inspect"}
            ):
                return 2
            started = time.perf_counter()
            reply: dict = {"id": request_id}
            try:
                if command == "meeting":
                    reply["connected"] = desktop.meeting() is not None
                elif command == "inspect":
                    automation = automation or Automation()
                    controls = ZoomControls(desktop, automation)
                    reply["state"] = asdict(controls.inspect())
                    reply["queries"] = automation.query_count
                else:
                    automation = automation or Automation()
                    controls = ZoomControls(desktop, automation)
                    foreground = desktop.user.GetForegroundWindow()
                    cursor = desktop.cursor()
                    try:
                        outcome = run_sequence(
                            controls,
                            command,
                            lambda step, request_id=request_id: _write(
                                {"id": request_id, "step": step}
                            ),
                        )
                        reply["result"] = asdict(outcome)
                    finally:
                        try:
                            desktop.restore(foreground, cursor)
                        except Exception:  # noqa: BLE001 - restoration cannot invalidate completed actions
                            log.warning(
                                "Could not restore focus/cursor after Zoom action", exc_info=True
                            )
            except ZoomAutomationError as exc:
                reply["error"] = exc.code
            except Exception:  # noqa: BLE001 - private worker provider/protocol boundary
                log.exception("Zoom command failed: %s", command)
                reply["error"] = "automation_failed"
            reply["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 3)
            _write(reply)
    finally:
        ole.CoUninitialize()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
