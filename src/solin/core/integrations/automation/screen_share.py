"""
screen_share.py — Cross-platform screen share automation via hotkeys + clicks.

Provides screen share start/stop orchestration for Zoom (or any app with a
single share toggle hotkey). Keyboard dispatch is delegated to shortcuts so
automatic shortcuts and auto-share use exactly the same cross-platform path.

Flow:
  1. Send a keyboard shortcut (e.g. Alt+S) to trigger the share dialog
  2. Wait until a new visible Zoom share dialog appears
  3. Send virtual clicks at a pre-configured position to select the target

All functions are designed to run in a background thread.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import ctypes.wintypes
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

from .shortcuts import send_key_sequence

log = logging.getLogger(__name__)
_MacPointT = TypeVar("_MacPointT")

SHARE_DIALOG_DETECTION_TIMEOUT_MS = 5000
SHARE_DIALOG_DETECTION_SETTLE_MS = 500
SHARE_DIALOG_POLL_INTERVAL_MS = 50
SHARE_DIALOG_MIN_WIDTH = 600
SHARE_DIALOG_MIN_HEIGHT = 400
USE_ZOOM_WINDOW_DETECTION_BEFORE_CLICK = True
SHARE_DIALOG_FIXED_DELAY_MS = 500
MOUSE_INTERFERENCE_DISTANCE_PX = 80
MOUSE_INTERFERENCE_POLL_INTERVAL_MS = 25

_WINDOWS_ZOOM_PROCESS_NAMES = frozenset({"zoom.exe"})
_MACOS_ZOOM_OWNER_NAMES = frozenset({"zoom.us", "zoom", "zoom workplace"})
_LINUX_ZOOM_PROCESS_NAMES = frozenset({"zoom", "zoom.real"})


def send_hotkey(shortcut: str) -> bool:
    """
    Send a keyboard shortcut to the foreground window using the shared
    automatic-shortcut dispatcher implementation.

    Args:
        shortcut: Key combination string, e.g. 'Alt+S', 'Ctrl+Shift+F5'.

    Returns:
        True if the shortcut was sent successfully.
    """
    if not shortcut:
        return False
    try:
        return send_key_sequence(shortcut)
    except Exception:  # noqa: BLE001 - desktop automation backend boundary
        log.exception("send_hotkey failed: %s", shortcut)
        return False


def macos_accessibility_trusted() -> bool | None:
    """Return macOS Accessibility permission state, or None on other platforms."""
    if sys.platform != "darwin":
        return None

    try:
        import Quartz
    except ImportError:
        Quartz = None

    if Quartz is not None:
        try:
            return bool(Quartz.AXIsProcessTrusted())
        except AttributeError:
            pass
        except Exception as exc:  # noqa: BLE001 - PyObjC accessibility boundary
            log.warning("Could not read macOS Accessibility permission via PyObjC: %s", exc)
            return False

    app_services = _load_application_services()
    if app_services is None:
        return False
    return bool(app_services.AXIsProcessTrusted())


# ─────────────────────────────────────────────────────────────────
#  Cross-platform virtual mouse clicks
# ─────────────────────────────────────────────────────────────────

class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _INPUT_MOUSE(ctypes.Structure):
    _fields_ = [
        ("dx",          ctypes.c_long),
        ("dy",          ctypes.c_long),
        ("mouseData",   ctypes.c_ulong),
        ("dwFlags",     ctypes.c_ulong),
        ("time",        ctypes.c_ulong),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class _INPUT_CLICK(ctypes.Structure):
    class _UNION(ctypes.Union):
        _fields_ = [("mi", _INPUT_MOUSE)]
    _fields_ = [
        ("type", ctypes.c_ulong),
        ("union", _UNION),
    ]


def send_virtual_clicks(
    x: int,
    y: int,
    count: int = 2,
    interval_ms: int = 80,
) -> bool:
    """
    Send virtual mouse clicks at absolute screen coordinates.

    Uses native OS injection where available:
      • Windows: SendInput against the virtual desktop.
      • macOS: Quartz/CoreGraphics events, requiring Accessibility permission.
      • Linux/X11: xdotool/XTest, with Wayland rejected explicitly.

    Backends restore the cursor position after dispatching the click sequence.

    Args:
        x: Global desktop X coordinate from the picker.
        y: Global desktop Y coordinate from the picker.
        count: Number of clicks to send (default: 2).
        interval_ms: Interval in ms between clicks (default: 80).

    Returns:
        True if all clicks were sent successfully.
    """
    if count <= 0:
        return False
    if sys.platform == "win32":
        return _clicks_win32(x, y, count, interval_ms)
    if sys.platform == "darwin":
        return _clicks_macos(x, y, count, interval_ms)
    if sys.platform.startswith("linux"):
        return _clicks_linux(x, y, count, interval_ms)

    return _clicks_fallback(x, y, count, interval_ms)


def _cursor_position() -> tuple[int, int] | None:
    if sys.platform == "win32":
        point = _POINT()
        try:
            if ctypes.windll.user32.GetCursorPos(ctypes.byref(point)):
                return int(point.x), int(point.y)
        except Exception as exc:  # noqa: BLE001 - Win32 cursor API boundary
            log.debug("Could not read Win32 cursor position: %s", exc)
        return None

    if sys.platform == "darwin":
        position = _cursor_position_macos_pyobjc()
        if position is not None:
            return position
        return _cursor_position_macos_ctypes()

    if sys.platform.startswith("linux"):
        xdotool = shutil.which("xdotool")
        if not xdotool:
            return None
        return _xdotool_position(xdotool, os.environ.copy())

    return None


def _cursor_position_macos_pyobjc() -> tuple[int, int] | None:
    try:
        import Quartz
    except ImportError:
        return None
    try:
        event = Quartz.CGEventCreate(None)
        if not event:
            return None
        x, y = _point_tuple(Quartz.CGEventGetLocation(event))
        return int(round(x)), int(round(y))
    except Exception as exc:  # noqa: BLE001 - PyObjC cursor API boundary
        log.debug("Could not read macOS cursor position through PyObjC: %s", exc)
        return None


def _cursor_position_macos_ctypes() -> tuple[int, int] | None:
    app_services = _load_application_services()
    if app_services is None:
        return None
    event = app_services.CGEventCreate(None)
    if not event:
        return None
    try:
        point = app_services.CGEventGetLocation(event)
        return int(round(point.x)), int(round(point.y))
    finally:
        app_services.CFRelease(event)


def _distance_sq(a: tuple[int, int], b: tuple[int, int]) -> int:
    dx = a[0] - b[0]
    dy = a[1] - b[1]
    return dx * dx + dy * dy


class _MouseInterferenceMonitor:
    def __init__(
        self,
        on_warning: Callable[[], None] | None,
        *,
        threshold_px: int = MOUSE_INTERFERENCE_DISTANCE_PX,
        cursor_position: Callable[[], tuple[int, int] | None] | None = None,
    ) -> None:
        self._on_warning = on_warning
        self._threshold_sq = threshold_px * threshold_px
        self._cursor_position = cursor_position or _cursor_position
        self._baseline = self._cursor_position() if on_warning is not None else None
        self._warned = False

    @property
    def enabled(self) -> bool:
        return self._on_warning is not None and self._baseline is not None

    def check(self, *extra_allowed_positions: tuple[int, int]) -> None:
        if not self.enabled or self._warned or self._baseline is None:
            return
        current = self._cursor_position()
        if current is None:
            return
        allowed_positions = (self._baseline, *extra_allowed_positions)
        if any(_distance_sq(current, allowed) <= self._threshold_sq for allowed in allowed_positions):
            return
        self._warned = True
        try:
            if self._on_warning is not None:
                self._on_warning()
        except Exception as exc:  # noqa: BLE001 - notification callback boundary
            log.debug("Auto-share mouse warning callback failed: %s", exc)

    def watch_during(
        self,
        operation: Callable[[], bool],
        *,
        target: tuple[int, int],
    ) -> bool:
        if not self.enabled:
            return operation()

        stop = threading.Event()

        def _poll() -> None:
            while not stop.wait(MOUSE_INTERFERENCE_POLL_INTERVAL_MS / 1000.0):
                self.check(target)

        thread = threading.Thread(
            target=_poll,
            name="solin-auto-share-mouse-watch",
            daemon=True,
        )
        thread.start()
        try:
            return operation()
        finally:
            stop.set()
            thread.join(timeout=0.2)
            self.check(target)


def _sleep_with_mouse_monitoring(
    milliseconds: int,
    monitor: _MouseInterferenceMonitor | None,
) -> None:
    if milliseconds <= 0:
        return
    if monitor is None or not monitor.enabled:
        time.sleep(milliseconds / 1000.0)
        return
    deadline = time.monotonic() + milliseconds / 1000.0
    interval = MOUSE_INTERFERENCE_POLL_INTERVAL_MS / 1000.0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(interval, remaining))
        monitor.check()


def _clicks_win32(x: int, y: int, count: int, interval_ms: int) -> bool:
    """Windows implementation using SendInput."""
    user32 = ctypes.windll.user32

    SM_CXVIRTUALSCREEN = 78
    SM_CYVIRTUALSCREEN = 79
    SM_XVIRTUALSCREEN = 76
    SM_YVIRTUALSCREEN = 77

    virt_w = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
    virt_h = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
    virt_x = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
    virt_y = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)

    if virt_w <= 0 or virt_h <= 0:
        return False

    norm_x = int(((x - virt_x) * 65535) / (virt_w - 1))
    norm_y = int(((y - virt_y) * 65535) / (virt_h - 1))

    saved_pos = _POINT()
    user32.GetCursorPos(ctypes.byref(saved_pos))

    MOUSEEVENTF_ABSOLUTE    = 0x8000
    MOUSEEVENTF_MOVE        = 0x0001
    MOUSEEVENTF_LEFTDOWN    = 0x0002
    MOUSEEVENTF_LEFTUP      = 0x0004
    MOUSEEVENTF_VIRTUALDESK = 0x4000
    INPUT_MOUSE = 0
    all_ok = True

    try:
        for i in range(count):
            inputs = (_INPUT_CLICK * 3)()

            inputs[0].type = INPUT_MOUSE
            inputs[0].union.mi.dx = norm_x
            inputs[0].union.mi.dy = norm_y
            inputs[0].union.mi.dwFlags = (
                MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_MOVE | MOUSEEVENTF_VIRTUALDESK
            )

            inputs[1].type = INPUT_MOUSE
            inputs[1].union.mi.dx = norm_x
            inputs[1].union.mi.dy = norm_y
            inputs[1].union.mi.dwFlags = (
                MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_LEFTDOWN | MOUSEEVENTF_VIRTUALDESK
            )

            inputs[2].type = INPUT_MOUSE
            inputs[2].union.mi.dx = norm_x
            inputs[2].union.mi.dy = norm_y
            inputs[2].union.mi.dwFlags = (
                MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_LEFTUP | MOUSEEVENTF_VIRTUALDESK
            )

            sent = user32.SendInput(3, ctypes.byref(inputs), ctypes.sizeof(_INPUT_CLICK))
            if sent != 3:
                all_ok = False

            if i < count - 1:
                time.sleep(interval_ms / 1000.0)
    finally:
        user32.SetCursorPos(saved_pos.x, saved_pos.y)

    return all_ok


class _CGPOINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


def _load_application_services():
    path = (
        ctypes.util.find_library("ApplicationServices")
        or "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
    )
    try:
        app_services = ctypes.CDLL(path)
    except OSError as exc:
        log.warning("ApplicationServices unavailable for macOS clicks: %s", exc)
        return None

    app_services.AXIsProcessTrusted.argtypes = []
    app_services.AXIsProcessTrusted.restype = ctypes.c_bool
    app_services.CGEventCreate.argtypes = [ctypes.c_void_p]
    app_services.CGEventCreate.restype = ctypes.c_void_p
    app_services.CGEventGetLocation.argtypes = [ctypes.c_void_p]
    app_services.CGEventGetLocation.restype = _CGPOINT
    app_services.CGEventCreateMouseEvent.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        _CGPOINT,
        ctypes.c_uint32,
    ]
    app_services.CGEventCreateMouseEvent.restype = ctypes.c_void_p
    app_services.CGEventPost.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
    app_services.CGEventPost.restype = None
    app_services.CFRelease.argtypes = [ctypes.c_void_p]
    app_services.CFRelease.restype = None
    return app_services


def _point_tuple(point) -> tuple[float, float]:
    if hasattr(point, "x") and hasattr(point, "y"):
        return float(point.x), float(point.y)
    return float(point[0]), float(point[1])


def _clicks_macos_pyobjc(x: int, y: int, count: int, interval_ms: int) -> bool | None:
    """macOS implementation through PyObjC's Quartz bindings, when installed."""
    try:
        import Quartz
    except ImportError:
        return None

    try:
        trusted = Quartz.AXIsProcessTrusted()
    except AttributeError:
        log.debug("PyObjC Quartz backend does not expose AXIsProcessTrusted")
        return None

    if not trusted:
        log.warning(
            "macOS Accessibility permission is required for automatic share clicks"
        )
        return False

    current_event = Quartz.CGEventCreate(None)
    if not current_event:
        log.warning("CGEventCreate failed while reading cursor position")
        return False
    saved_pos = _point_tuple(Quartz.CGEventGetLocation(current_event))
    target = (float(x), float(y))

    def _post(event_type: int, point: tuple[float, float]) -> bool:
        event = Quartz.CGEventCreateMouseEvent(
            None,
            event_type,
            point,
            Quartz.kCGMouseButtonLeft,
        )
        if not event:
            return False
        # PyObjC owns the returned Core Foundation reference and releases it
        # when the Python proxy is collected. Manual CFRelease here can double
        # release on supported PyObjC metadata.
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
        return True

    return _run_macos_click_sequence(
        count=count,
        interval_ms=interval_ms,
        target=target,
        saved_pos=saved_pos,
        post_move=lambda point: _post(Quartz.kCGEventMouseMoved, point),
        post_down=lambda point: _post(Quartz.kCGEventLeftMouseDown, point),
        post_up=lambda point: _post(Quartz.kCGEventLeftMouseUp, point),
    )


def _clicks_macos(x: int, y: int, count: int, interval_ms: int) -> bool:
    """macOS implementation using native Quartz/CoreGraphics events."""
    pyobjc_result = _clicks_macos_pyobjc(x, y, count, interval_ms)
    if pyobjc_result is not None:
        return pyobjc_result
    return _clicks_macos_ctypes(x, y, count, interval_ms)


def _clicks_macos_ctypes(x: int, y: int, count: int, interval_ms: int) -> bool:
    """macOS Quartz/CoreGraphics fallback through ctypes."""
    app_services = _load_application_services()
    if app_services is None:
        return False
    if not app_services.AXIsProcessTrusted():
        log.warning(
            "macOS Accessibility permission is required for automatic share clicks"
        )
        return False

    K_CG_HID_EVENT_TAP = 0
    K_CG_EVENT_LEFT_MOUSE_DOWN = 1
    K_CG_EVENT_LEFT_MOUSE_UP = 2
    K_CG_EVENT_MOUSE_MOVED = 5
    K_CG_MOUSE_BUTTON_LEFT = 0

    current_event = app_services.CGEventCreate(None)
    if not current_event:
        log.warning("CGEventCreate failed while reading cursor position")
        return False
    saved_pos = app_services.CGEventGetLocation(current_event)
    app_services.CFRelease(current_event)

    target = _CGPOINT(float(x), float(y))

    def _post(event_type: int, point: _CGPOINT) -> bool:
        event = app_services.CGEventCreateMouseEvent(
            None,
            event_type,
            point,
            K_CG_MOUSE_BUTTON_LEFT,
        )
        if not event:
            return False
        try:
            app_services.CGEventPost(K_CG_HID_EVENT_TAP, event)
            return True
        finally:
            app_services.CFRelease(event)

    return _run_macos_click_sequence(
        count=count,
        interval_ms=interval_ms,
        target=target,
        saved_pos=saved_pos,
        post_move=lambda point: _post(K_CG_EVENT_MOUSE_MOVED, point),
        post_down=lambda point: _post(K_CG_EVENT_LEFT_MOUSE_DOWN, point),
        post_up=lambda point: _post(K_CG_EVENT_LEFT_MOUSE_UP, point),
    )


def _run_macos_click_sequence(
    *,
    count: int,
    interval_ms: int,
    target: _MacPointT,
    saved_pos: _MacPointT,
    post_move: Callable[[_MacPointT], bool],
    post_down: Callable[[_MacPointT], bool],
    post_up: Callable[[_MacPointT], bool],
) -> bool:
    ok = True

    def _safe_post(callback, point) -> bool:
        try:
            return bool(callback(point))
        except Exception as exc:  # noqa: BLE001 - Quartz event callback boundary
            log.warning("macOS mouse event post failed: %s", exc)
            return False

    try:
        for i in range(count):
            moved = _safe_post(post_move, target)
            down_sent = False
            try:
                down_sent = _safe_post(post_down, target)
                if down_sent:
                    time.sleep(0.03)
            finally:
                up_sent = _safe_post(post_up, target)
            ok = bool(moved and down_sent and up_sent) and ok
            if i < count - 1:
                time.sleep(interval_ms / 1000.0)
    finally:
        _safe_post(post_move, saved_pos)

    return ok


def _xdotool_position(xdotool: str, env: dict[str, str]) -> tuple[int, int] | None:
    try:
        result = subprocess.run(
            [xdotool, "getmouselocation", "--shell"],
            check=True,
            capture_output=True,
            text=True,
            timeout=1.0,
            env=env,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("xdotool could not read cursor position: %s", exc)
        return None

    values = dict(re.findall(r"^(X|Y)=(-?\d+)$", result.stdout, re.MULTILINE))
    try:
        return int(values["X"]), int(values["Y"])
    except (KeyError, ValueError):
        log.warning("xdotool returned an unexpected cursor position payload")
        return None


def _clicks_linux(x: int, y: int, count: int, interval_ms: int) -> bool:
    """Linux implementation using xdotool/XTest, with cursor restore."""
    xdotool = shutil.which("xdotool")
    if not xdotool:
        log.warning("xdotool not found; automatic share clicks are unavailable")
        return False

    session_type = os.environ.get("XDG_SESSION_TYPE", "").lower()
    if session_type == "wayland":
        log.warning("xdotool cannot reliably automate clicks on Wayland sessions")
        return False

    env = os.environ.copy()
    saved_pos = _xdotool_position(xdotool, env)
    if saved_pos is None:
        return False

    ok = True
    try:
        for i in range(count):
            try:
                subprocess.run(
                    [xdotool, "mousemove", "--sync", str(x), str(y)],
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=2.0,
                    env=env,
                )
                subprocess.run(
                    [xdotool, "click", "--clearmodifiers", "1"],
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=2.0,
                    env=env,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                log.warning("xdotool click failed at (%s, %s): %s", x, y, exc)
                ok = False
                break

            if i < count - 1:
                time.sleep(interval_ms / 1000.0)
    finally:
        try:
            subprocess.run(
                [xdotool, "mousemove", "--sync", str(saved_pos[0]), str(saved_pos[1])],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=1.0,
                env=env,
            )
        except (OSError, subprocess.SubprocessError):
            pass

    return ok


def _clicks_fallback(x: int, y: int, count: int, interval_ms: int) -> bool:
    log.warning("Automatic share clicks are unsupported on this platform: %s", sys.platform)
    return False


# ─────────────────────────────────────────────────────────────────
#  Cross-platform Zoom share-dialog window detection
# ─────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class _ZoomWindowInfo:
    window_id: str
    owner: str
    x: int
    y: int
    width: int
    height: int

    @property
    def area(self) -> int:
        return self.width * self.height


def _is_candidate_share_dialog(window: _ZoomWindowInfo) -> bool:
    return (
        window.width >= SHARE_DIALOG_MIN_WIDTH
        and window.height >= SHARE_DIALOG_MIN_HEIGHT
    )


def _capture_zoom_windows_before_share_click() -> dict[str, _ZoomWindowInfo] | None:
    if not USE_ZOOM_WINDOW_DETECTION_BEFORE_CLICK:
        return {}
    return _list_zoom_windows()


def _wait_before_share_click(
    delay_ms: int,
    initial_windows: dict[str, _ZoomWindowInfo] | None,
    monitor: _MouseInterferenceMonitor | None = None,
) -> bool:
    if not USE_ZOOM_WINDOW_DETECTION_BEFORE_CLICK:
        _sleep_with_mouse_monitoring(delay_ms, monitor)
        return True
    if initial_windows is None:
        log.warning(
            "execute_start_share: Zoom window detection is unavailable on %s",
            sys.platform,
        )
        return False
    detected = _wait_for_new_zoom_window(initial_windows, monitor)
    if detected is None:
        log.warning(
            "execute_start_share: no new Zoom share dialog detected within %dms",
            SHARE_DIALOG_DETECTION_TIMEOUT_MS,
        )
        return False

    log.debug(
        "execute_start_share: detected Zoom dialog %s (%dx%d at %d,%d, owner=%s)",
        detected.window_id,
        detected.width,
        detected.height,
        detected.x,
        detected.y,
        detected.owner,
    )
    _sleep_with_mouse_monitoring(SHARE_DIALOG_DETECTION_SETTLE_MS, monitor)
    return True


def _wait_for_new_zoom_window(
    initial_windows: dict[str, _ZoomWindowInfo],
    monitor: _MouseInterferenceMonitor | None = None,
) -> _ZoomWindowInfo | None:
    deadline = time.monotonic() + (SHARE_DIALOG_DETECTION_TIMEOUT_MS / 1000.0)
    initial_ids = set(initial_windows)

    while time.monotonic() < deadline:
        if monitor is not None:
            monitor.check()
        current_windows = _list_zoom_windows()
        if current_windows is None:
            return None

        candidates = [
            current_windows[window_id]
            for window_id in set(current_windows) - initial_ids
            if _is_candidate_share_dialog(current_windows[window_id])
        ]
        if candidates:
            return max(candidates, key=lambda window: window.area)

        _sleep_with_mouse_monitoring(SHARE_DIALOG_POLL_INTERVAL_MS, monitor)

    return None


def _list_zoom_windows() -> dict[str, _ZoomWindowInfo] | None:
    if sys.platform == "win32":
        return _list_zoom_windows_win32()
    if sys.platform == "darwin":
        return _list_zoom_windows_macos()
    if sys.platform.startswith("linux"):
        return _list_zoom_windows_linux()
    return None


def _list_zoom_windows_win32() -> dict[str, _ZoomWindowInfo] | None:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    windows: dict[str, _ZoomWindowInfo] = {}

    WNDENUMPROC = ctypes.WINFUNCTYPE(
        ctypes.wintypes.BOOL,
        ctypes.wintypes.HWND,
        ctypes.wintypes.LPARAM,
    )
    user32.EnumWindows.argtypes = [WNDENUMPROC, ctypes.wintypes.LPARAM]
    user32.EnumWindows.restype = ctypes.wintypes.BOOL
    user32.IsWindow.argtypes = [ctypes.wintypes.HWND]
    user32.IsWindow.restype = ctypes.wintypes.BOOL
    user32.IsWindowVisible.argtypes = [ctypes.wintypes.HWND]
    user32.IsWindowVisible.restype = ctypes.wintypes.BOOL
    user32.IsIconic.argtypes = [ctypes.wintypes.HWND]
    user32.IsIconic.restype = ctypes.wintypes.BOOL
    user32.GetWindowRect.argtypes = [
        ctypes.wintypes.HWND,
        ctypes.POINTER(ctypes.wintypes.RECT),
    ]
    user32.GetWindowRect.restype = ctypes.wintypes.BOOL

    def _callback(hwnd, _lparam):
        if not user32.IsWindow(hwnd) or not user32.IsWindowVisible(hwnd):
            return True
        try:
            if user32.IsIconic(hwnd):
                return True
            process_name = _win32_process_name_for_window(hwnd)
            if process_name.lower() not in _WINDOWS_ZOOM_PROCESS_NAMES:
                return True

            rect = ctypes.wintypes.RECT()
            if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                return True
            width = int(rect.right - rect.left)
            height = int(rect.bottom - rect.top)
            if width <= 0 or height <= 0:
                return True

            window_id = str(int(hwnd))
            windows[window_id] = _ZoomWindowInfo(
                window_id=window_id,
                owner=process_name,
                x=int(rect.left),
                y=int(rect.top),
                width=width,
                height=height,
            )
        except Exception as exc:  # noqa: BLE001 - Win32 enumeration callback boundary
            log.debug("Skipping Win32 window during Zoom enumeration: %s", exc)
        return True

    try:
        callback = WNDENUMPROC(_callback)
        if not user32.EnumWindows(callback, 0):
            return None
    except Exception as exc:  # noqa: BLE001 - Win32 API boundary
        log.warning("Win32 Zoom window enumeration failed: %s", exc)
        return None

    return windows


def _win32_process_name_for_window(hwnd) -> str:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

    user32.GetWindowThreadProcessId.argtypes = [
        ctypes.wintypes.HWND,
        ctypes.POINTER(ctypes.wintypes.DWORD),
    ]
    user32.GetWindowThreadProcessId.restype = ctypes.wintypes.DWORD
    kernel32.OpenProcess.argtypes = [
        ctypes.wintypes.DWORD,
        ctypes.wintypes.BOOL,
        ctypes.wintypes.DWORD,
    ]
    kernel32.OpenProcess.restype = ctypes.wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [ctypes.wintypes.HANDLE]
    kernel32.CloseHandle.restype = ctypes.wintypes.BOOL

    pid = ctypes.wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return ""

    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not handle:
        return ""

    try:
        size = ctypes.wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        query = getattr(kernel32, "QueryFullProcessImageNameW", None)
        if query is not None:
            query.argtypes = [
                ctypes.wintypes.HANDLE,
                ctypes.wintypes.DWORD,
                ctypes.wintypes.LPWSTR,
                ctypes.POINTER(ctypes.wintypes.DWORD),
            ]
            query.restype = ctypes.wintypes.BOOL
        if query is not None and query(handle, 0, buffer, ctypes.byref(size)):
            return Path(buffer.value).name
    finally:
        kernel32.CloseHandle(handle)

    return ""


def _list_zoom_windows_macos() -> dict[str, _ZoomWindowInfo] | None:
    try:
        import Quartz
    except ImportError:
        return None

    try:
        raw_windows = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly
            | Quartz.kCGWindowListExcludeDesktopElements,
            Quartz.kCGNullWindowID,
        )
    except Exception as exc:  # noqa: BLE001 - PyObjC window-server boundary
        log.warning("macOS Zoom window enumeration failed: %s", exc)
        return None

    windows: dict[str, _ZoomWindowInfo] = {}
    for raw in raw_windows or []:
        owner = str(raw.get("kCGWindowOwnerName") or "").strip()
        if not _is_macos_zoom_owner(owner):
            continue
        bounds = raw.get("kCGWindowBounds") or {}
        try:
            width = int(bounds.get("Width", 0))
            height = int(bounds.get("Height", 0))
            if width <= 0 or height <= 0:
                continue
            window_id = str(int(raw.get("kCGWindowNumber", 0)))
            if window_id == "0":
                continue
            windows[window_id] = _ZoomWindowInfo(
                window_id=window_id,
                owner=owner,
                x=int(bounds.get("X", 0)),
                y=int(bounds.get("Y", 0)),
                width=width,
                height=height,
            )
        except (TypeError, ValueError):
            continue

    return windows


def _is_macos_zoom_owner(owner_name: str) -> bool:
    normalized = owner_name.casefold()
    return any(name in normalized for name in _MACOS_ZOOM_OWNER_NAMES)


def _list_zoom_windows_linux() -> dict[str, _ZoomWindowInfo] | None:
    session_type = os.environ.get("XDG_SESSION_TYPE", "").lower()
    if session_type == "wayland":
        return None

    xdotool = shutil.which("xdotool")
    if not xdotool:
        return None

    env = os.environ.copy()
    pids = _linux_zoom_pids()
    if not pids:
        return {}

    windows: dict[str, _ZoomWindowInfo] = {}
    for pid, owner in pids.items():
        try:
            result = subprocess.run(
                [xdotool, "search", "--onlyvisible", "--pid", str(pid)],
                check=False,
                capture_output=True,
                text=True,
                timeout=1.0,
                env=env,
            )
        except (OSError, subprocess.SubprocessError):
            continue

        for raw_window_id in result.stdout.splitlines():
            window_id = raw_window_id.strip()
            if not window_id:
                continue
            geometry = _xdotool_window_geometry(xdotool, window_id, env)
            if geometry is None:
                continue
            x, y, width, height = geometry
            if width <= 0 or height <= 0:
                continue
            windows[window_id] = _ZoomWindowInfo(
                window_id=window_id,
                owner=owner,
                x=x,
                y=y,
                width=width,
                height=height,
            )

    return windows


def _linux_zoom_pids() -> dict[int, str]:
    pids: dict[int, str] = {}
    proc_root = Path("/proc")
    if not proc_root.is_dir():
        return pids

    for proc_dir in proc_root.iterdir():
        if not proc_dir.name.isdigit():
            continue
        try:
            comm = (proc_dir / "comm").read_text(encoding="utf-8", errors="ignore").strip()
            cmdline = (proc_dir / "cmdline").read_bytes().decode(
                "utf-8", errors="ignore"
            ).replace("\x00", " ")
        except OSError:
            continue
        owner = comm or cmdline
        normalized_owner = owner.casefold()
        normalized_cmdline = cmdline.casefold()
        if (
            normalized_owner in _LINUX_ZOOM_PROCESS_NAMES
            or "/zoom" in normalized_cmdline
            or "zoom.us" in normalized_cmdline
        ):
            pids[int(proc_dir.name)] = owner

    return pids


def _xdotool_window_geometry(
    xdotool: str,
    window_id: str,
    env: dict[str, str],
) -> tuple[int, int, int, int] | None:
    try:
        result = subprocess.run(
            [xdotool, "getwindowgeometry", "--shell", window_id],
            check=True,
            capture_output=True,
            text=True,
            timeout=1.0,
            env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return None

    values = dict(
        re.findall(r"^(X|Y|WIDTH|HEIGHT)=(-?\d+)$", result.stdout, re.MULTILINE)
    )
    try:
        return (
            int(values["X"]),
            int(values["Y"]),
            int(values["WIDTH"]),
            int(values["HEIGHT"]),
        )
    except (KeyError, ValueError):
        return None


# ─────────────────────────────────────────────────────────────────
#  Orchestration: full share sequence
# ─────────────────────────────────────────────────────────────────

def execute_start_share(
    hotkey: str,
    click_x: int = -1,
    click_y: int = -1,
    *,
    delay_ms: int | None = None,
    movement_warning: Callable[[], None] | None = None,
) -> bool:
    """
    Execute the full start-share sequence:
      1. Send the configured hotkey (opens Zoom share dialog)
      2. Wait for the share dialog to appear, or use the fixed delay fallback
      3. Send virtual clicks at configured position (selects share target)

    Args:
        hotkey: Keyboard shortcut to toggle share (e.g. 'Alt+S').
        click_x: Screen X for target click (-1 = skip clicks).
        click_y: Screen Y for target click (-1 = skip clicks).
        delay_ms: Fixed-delay milliseconds to wait between hotkey and clicks.
            Uses SHARE_DIALOG_FIXED_DELAY_MS when omitted.
        movement_warning: Optional callback invoked once if the cursor moves
            enough to risk competing with the automated share-target click.

    Returns:
        True if the sequence completed successfully.
    """
    if not hotkey:
        return False

    click_configured = click_x >= 0 and click_y >= 0
    delay_ms = SHARE_DIALOG_FIXED_DELAY_MS if delay_ms is None else delay_ms
    monitor = (
        _MouseInterferenceMonitor(movement_warning)
        if click_configured else None
    )
    initial_windows = (
        _capture_zoom_windows_before_share_click()
        if click_configured else {}
    )
    if click_configured and USE_ZOOM_WINDOW_DETECTION_BEFORE_CLICK and initial_windows is None:
        log.warning(
            "execute_start_share: Zoom window detection is unavailable on %s",
            sys.platform,
        )
        return False

    ok = send_hotkey(hotkey)
    if not ok:
        log.warning("execute_start_share: hotkey '%s' failed", hotkey)
        return False

    if click_configured:
        if not _wait_before_share_click(delay_ms, initial_windows, monitor):
            return False
        if monitor is not None:
            monitor.check()
            ok = monitor.watch_during(
                lambda: send_virtual_clicks(click_x, click_y, count=2, interval_ms=120),
                target=(click_x, click_y),
            )
        else:
            ok = send_virtual_clicks(click_x, click_y, count=2, interval_ms=120)
        if not ok:
            log.warning("execute_start_share: virtual clicks failed at (%d, %d)", click_x, click_y)
            return False

    return True


def execute_stop_share(hotkey: str) -> bool:
    """
    Execute stop-share by sending the configured share toggle hotkey.

    Args:
        hotkey: Keyboard shortcut to toggle share (e.g. 'Alt+S').

    Returns:
        True if the hotkey was sent successfully.
    """
    if not hotkey:
        return False
    return send_hotkey(hotkey)
