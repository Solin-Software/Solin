from __future__ import annotations

import ctypes
from types import SimpleNamespace

import pytest

from solin.core.integrations.automation.zoom import native
from solin.core.integrations.automation.zoom.state import ZoomAutomationError


def test_desktop_rejects_unsupported_platform_before_loading_dlls(monkeypatch):
    monkeypatch.setattr(native.sys, "platform", "linux")
    with pytest.raises(ZoomAutomationError, match="unsupported_platform"):
        native.WindowsDesktop()


@pytest.mark.skipif(native.sys.platform != "win32", reason="Windows ABI")
def test_input_union_preserves_native_pointer_alignment():
    assert ctypes.sizeof(native.Input) == (40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28)


def test_partial_native_click_releases_mouse_before_reporting_failure():
    desktop = native.WindowsDesktop.__new__(native.WindowsDesktop)
    calls = []

    def send(count, batch, size):
        calls.append(count)
        return 1

    desktop.user = SimpleNamespace(SendInput=send)
    with pytest.raises(ZoomAutomationError, match="input_failed"):
        desktop._send(
            [
                native.Input(kind=0, mouse=native.MouseInput(flags=2)),
                native.Input(kind=0, mouse=native.MouseInput(flags=4)),
            ]
        )
    assert calls == [2, 1]


@pytest.mark.parametrize("visible,class_name", [(False, "meeting"), (True, "reused-dialog")])
def test_hidden_or_reused_window_cannot_receive_meeting_actions(visible, class_name):
    desktop = native.WindowsDesktop.__new__(native.WindowsDesktop)

    def read_class(handle, buffer, size):
        buffer.value = class_name

    def read_pid(handle, pointer):
        pointer._obj.value = 2

    desktop.user = SimpleNamespace(
        IsWindow=lambda _: True, IsWindowVisible=lambda _: visible,
        GetClassNameW=read_class, GetWindowThreadProcessId=read_pid,
    )
    desktop.is_zoom = lambda _: True
    assert not desktop.valid(native.Window(1, 2, "meeting", 20))
