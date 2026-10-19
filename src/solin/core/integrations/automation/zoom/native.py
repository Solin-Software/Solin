"""Typed Win32 discovery and guarded input for the isolated Zoom process."""

from __future__ import annotations

import ctypes
import sys
from _ctypes import CFuncPtr
from ctypes import wintypes as wt
from dataclasses import dataclass

from .state import ZoomAutomationError

MEETING_CLASS = "ConfMultiTabContentWndClass"
FLOATING_CLASS = "ZPFloatToolbarClass"
TOOLBAR_CLASS = "ZPControlPanelClass"


@dataclass(frozen=True, slots=True)
class Window:
    handle: int
    pid: int
    class_name: str
    area: int


class Point(ctypes.Structure):
    _fields_ = [("x", wt.LONG), ("y", wt.LONG)]


class MouseInput(ctypes.Structure):
    _fields_ = [
        ("dx", wt.LONG),
        ("dy", wt.LONG),
        ("data", wt.DWORD),
        ("flags", wt.DWORD),
        ("time", wt.DWORD),
        ("extra", ctypes.c_size_t),
    ]


class KeyInput(ctypes.Structure):
    _fields_ = [
        ("vk", wt.WORD),
        ("scan", wt.WORD),
        ("flags", wt.DWORD),
        ("time", wt.DWORD),
        ("extra", ctypes.c_size_t),
    ]


class InputData(ctypes.Union):
    _fields_ = [("mouse", MouseInput), ("key", KeyInput)]


class Input(ctypes.Structure):
    _anonymous_ = ("data",)
    _fields_ = [("kind", wt.DWORD), ("data", InputData)]


class WindowsDesktop:
    """No UIA desktop walk: inspect native top-level windows by class and process."""

    user: ctypes.CDLL
    kernel: ctypes.CDLL
    callback_type: type[CFuncPtr]

    def __init__(self):
        if sys.platform != "win32":
            raise ZoomAutomationError("unsupported_platform")
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.callback_type = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
        signatures = {
            "EnumWindows": ([self.callback_type, wt.LPARAM], wt.BOOL),
            "GetClassNameW": ([wt.HWND, wt.LPWSTR, ctypes.c_int], ctypes.c_int),
            "GetWindowThreadProcessId": ([wt.HWND, ctypes.POINTER(wt.DWORD)], wt.DWORD),
            "IsWindowVisible": ([wt.HWND], wt.BOOL),
            "IsWindow": ([wt.HWND], wt.BOOL),
            "IsIconic": ([wt.HWND], wt.BOOL),
            "GetWindowRect": ([wt.HWND, ctypes.POINTER(wt.RECT)], wt.BOOL),
            "GetForegroundWindow": ([], wt.HWND),
            "SetForegroundWindow": ([wt.HWND], wt.BOOL),
            "BringWindowToTop": ([wt.HWND], wt.BOOL),
            "ShowWindow": ([wt.HWND, ctypes.c_int], wt.BOOL),
            "AttachThreadInput": ([wt.DWORD, wt.DWORD, wt.BOOL], wt.BOOL),
            "GetCursorPos": ([ctypes.POINTER(Point)], wt.BOOL),
            "SetCursorPos": ([ctypes.c_int, ctypes.c_int], wt.BOOL),
            "WindowFromPoint": ([Point], wt.HWND),
            "GetAncestor": ([wt.HWND, wt.UINT], wt.HWND),
            "ScreenToClient": ([wt.HWND, ctypes.POINTER(Point)], wt.BOOL),
            "PostMessageW": ([wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM], wt.BOOL),
            "SendInput": ([wt.UINT, ctypes.POINTER(Input), ctypes.c_int], wt.UINT),
            "SetThreadDpiAwarenessContext": ([ctypes.c_void_p], ctypes.c_void_p),
        }
        for name, (arguments, result) in signatures.items():
            fn = getattr(self.user, name)
            fn.argtypes, fn.restype = arguments, result
        for name, arguments, result in [
            ("OpenProcess", [wt.DWORD, wt.BOOL, wt.DWORD], wt.HANDLE),
            (
                "QueryFullProcessImageNameW",
                [wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)],
                wt.BOOL,
            ),
            ("CloseHandle", [wt.HANDLE], wt.BOOL),
            ("GetCurrentThreadId", [], wt.DWORD),
        ]:
            fn = getattr(self.kernel, name)
            fn.argtypes, fn.restype = arguments, result
        self._last_cursor: tuple[int, int] | None = None

    def is_zoom(self, handle: int) -> bool:
        pid = wt.DWORD()
        self.user.GetWindowThreadProcessId(handle, ctypes.byref(pid))
        process = self.kernel.OpenProcess(0x1000, False, pid.value)
        if not process:
            return False
        try:
            size = wt.DWORD(32768)
            name = ctypes.create_unicode_buffer(size.value)
            return bool(
                self.kernel.QueryFullProcessImageNameW(process, 0, name, ctypes.byref(size))
            ) and name.value.casefold().endswith("\\zoom.exe")
        finally:
            self.kernel.CloseHandle(process)

    def windows(self, *classes: str, pid: int | None = None) -> list[Window]:
        found = []

        def visit(handle, _parameter):
            name = ctypes.create_unicode_buffer(256)
            self.user.GetClassNameW(handle, name, len(name))
            if (classes and name.value not in classes) or not self.user.IsWindowVisible(handle):
                return True
            process_id = wt.DWORD()
            self.user.GetWindowThreadProcessId(handle, ctypes.byref(process_id))
            if (pid is not None and process_id.value != pid) or not self.is_zoom(handle):
                return True
            rect = wt.RECT()
            self.user.GetWindowRect(handle, ctypes.byref(rect))
            area = max(0, rect.right - rect.left) * max(0, rect.bottom - rect.top)
            found.append(Window(int(handle), process_id.value, name.value, area))
            return True

        self.user.EnumWindows(self.callback_type(visit), 0)
        return found

    def meeting(self) -> Window | None:
        windows = self.windows(MEETING_CLASS, FLOATING_CLASS)
        return max(windows, key=lambda w: (w.class_name == MEETING_CLASS, w.area), default=None)

    def valid(self, window: Window) -> bool:
        if not self.user.IsWindow(window.handle) or not self.user.IsWindowVisible(window.handle):
            return False
        pid = wt.DWORD()
        class_name = ctypes.create_unicode_buffer(256)
        self.user.GetClassNameW(window.handle, class_name, len(class_name))
        self.user.GetWindowThreadProcessId(window.handle, ctypes.byref(pid))
        return (
            pid.value == window.pid
            and class_name.value == window.class_name
            and self.is_zoom(window.handle)
        )

    def focus(self, window: Window) -> None:
        if not self.valid(window):
            raise ZoomAutomationError("meeting_not_found")
        if self.user.IsIconic(window.handle):
            self.user.ShowWindow(window.handle, 9)
        foreground = self.user.GetForegroundWindow()
        if foreground == window.handle:
            return
        current = self.kernel.GetCurrentThreadId()
        other = self.user.GetWindowThreadProcessId(foreground, None)
        attached = other != current and bool(self.user.AttachThreadInput(current, other, True))
        try:
            self.user.BringWindowToTop(window.handle)
            self.user.SetForegroundWindow(window.handle)
        finally:
            if attached:
                self.user.AttachThreadInput(current, other, False)
        if self.user.GetForegroundWindow() != window.handle:
            raise ZoomAutomationError("focus_failed")

    def cursor(self) -> tuple[int, int]:
        if sys.platform != "win32":
            raise ZoomAutomationError("unsupported_platform")
        point = Point()
        if not self.user.GetCursorPos(ctypes.byref(point)):
            raise ctypes.WinError(ctypes.get_last_error())
        return point.x, point.y

    def move(self, x: int, y: int) -> None:
        if not self.user.SetCursorPos(x, y):
            raise ZoomAutomationError("input_failed")
        self._last_cursor = (x, y)

    def hover(self, window: Window, *, real: bool = False) -> None:
        rect = wt.RECT()
        self.user.GetWindowRect(window.handle, ctypes.byref(rect))
        point = Point((rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2)
        if real:
            if self.user.GetAncestor(self.user.WindowFromPoint(point), 2) != window.handle:
                raise ZoomAutomationError("window_occluded")
            self.move(point.x + 3, point.y + 3)
            self.move(point.x, point.y)
        else:
            target = self.user.WindowFromPoint(point)
            if not target or self.user.GetAncestor(target, 2) != window.handle:
                target = window.handle
            self.user.ScreenToClient(target, ctypes.byref(point))
            self.user.PostMessageW(
                target, 0x0200, 0, ((point.y & 0xFFFF) << 16) | (point.x & 0xFFFF)
            )

    def _send(self, values: list[Input]) -> None:
        batch = (Input * len(values))(*values)
        sent = self.user.SendInput(len(batch), batch, ctypes.sizeof(Input))
        if sent != len(batch):
            if sent > 0:
                # A partial click must not leave the mouse button held down.
                release = Input(kind=0, mouse=MouseInput(flags=0x0004))
                self.user.SendInput(1, ctypes.byref(release), ctypes.sizeof(Input))
            raise ZoomAutomationError("input_failed")

    def click(self, window: Window, rect: tuple[int, int, int, int]) -> None:
        self.focus(window)
        left, top, right, bottom = rect
        point = Point((left + right) // 2, (top + bottom) // 2)
        if (
            right <= left
            or bottom <= top
            or self.user.GetAncestor(self.user.WindowFromPoint(point), 2) != window.handle
        ):
            raise ZoomAutomationError("window_occluded")
        self.move(point.x, point.y)
        self._send(
            [
                Input(kind=0, mouse=MouseInput(flags=0x0002)),
                Input(kind=0, mouse=MouseInput(flags=0x0004)),
            ]
        )

    def restore(self, foreground: int, cursor: tuple[int, int]) -> None:
        if self._last_cursor is not None and self.cursor() == self._last_cursor:
            self.user.SetCursorPos(*cursor)
        current = self.user.GetForegroundWindow()
        if foreground and current != foreground and self.is_zoom(current):
            self.user.SetForegroundWindow(foreground)
        self._last_cursor = None
