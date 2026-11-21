"""Private stdin/stdout worker for the Windows meeting automation."""

from __future__ import annotations

import ctypes
import json
import logging
import os
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict
from typing import TYPE_CHECKING

from .protocol import (
    MAX_MESSAGE_BYTES,
    PARTICIPANT_ACTIONS,
    PROTOCOL_VERSION,
    RAISED_HANDS,
    valid_request,
)
from .sequences import run_sequence
from .state import ParticipantAction, ZoomAutomationError

if TYPE_CHECKING:
    from .native import WindowsDesktop

log = logging.getLogger(__name__)


def _write(message: dict) -> None:
    frame = json.dumps(message, separators=(",", ":")).encode() + b"\n"
    if len(frame) > MAX_MESSAGE_BYTES:
        frame = json.dumps({"id": message["id"], "error": "snapshot_too_large"}).encode() + b"\n"
    sys.stdout.buffer.write(frame)
    sys.stdout.buffer.flush()


@contextmanager
def _restore_input(desktop: WindowsDesktop):
    foreground = desktop.user.GetForegroundWindow()
    cursor = desktop.cursor()
    try:
        yield
    finally:
        try:
            desktop.restore(foreground, cursor)
        except Exception:  # noqa: BLE001 - restoration cannot invalidate completed actions
            log.warning("Could not restore focus/cursor after Zoom action", exc_info=True)


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
    hands = None
    try:
        _watch_parent()
        from .controls import Automation, ZoomControls
        from .native import WindowsDesktop
        from .participants import ZoomRaisedHands

        desktop = WindowsDesktop()
        # All coordinates used by UIA and SendInput are physical pixels.
        desktop.user.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
        automation = None
        hands = ZoomRaisedHands()
        _write({"ready": PROTOCOL_VERSION})
        while line := sys.stdin.buffer.readline(MAX_MESSAGE_BYTES + 1):
            if len(line) > MAX_MESSAGE_BYTES:
                return 2
            try:
                request = json.loads(line)
            except (ValueError, RecursionError):
                return 2
            if not valid_request(request):
                return 2
            request_id, command = request["id"], request["command"]
            started = time.perf_counter()
            reply: dict = {"id": request_id}
            try:
                if command == "meeting":
                    # While a connected monitor is active, the service sends
                    # snapshots instead of these connection-only probes.
                    hands.clear()
                    reply["connected"] = desktop.meeting() is not None
                else:
                    automation = automation or Automation()
                    controls = ZoomControls(desktop, automation)
                    if command == "inspect":
                        reply["state"] = asdict(controls.inspect())
                        reply["queries"] = automation.query_count
                    elif command == RAISED_HANDS:
                        if request["open_panel"]:
                            hands.clear()
                            with _restore_input(desktop):
                                controls.begin_step()
                                snapshot = hands.snapshot(controls, open_if_closed=True)
                        else:
                            snapshot = hands.snapshot(controls, open_if_closed=False)
                        reply["snapshot"] = asdict(snapshot)
                    elif command in PARTICIPANT_ACTIONS:
                        outcome = hands.act(
                            controls,
                            ParticipantAction(command),
                            request["reference"],
                            lambda step, request_id=request_id: _write(
                                {"id": request_id, "participant_step": asdict(step)}
                            ),
                        )
                        reply["result"] = asdict(outcome)
                    else:
                        with _restore_input(desktop):
                            outcome = run_sequence(
                                controls,
                                command,
                                lambda step, request_id=request_id: _write(
                                    {"id": request_id, "step": step}
                                ),
                            )
                        reply["result"] = asdict(outcome)
            except ZoomAutomationError as exc:
                if exc.code == "meeting_not_found":
                    hands.clear()
                reply["error"] = exc.code
            except Exception as exc:  # noqa: BLE001 - private worker provider boundary
                if command == RAISED_HANDS or command in PARTICIPANT_ACTIONS:
                    hands.clear()
                    log.warning("Zoom %s failed: %s", command, type(exc).__name__)
                else:
                    log.exception("Zoom command failed: %s", command)
                reply["error"] = "automation_failed"
            reply["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 3)
            _write(reply)
    finally:
        if hands is not None:
            hands.clear()
        ole.CoUninitialize()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
