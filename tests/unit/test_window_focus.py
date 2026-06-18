from __future__ import annotations

from solin.ui import window_focus


class _WindowStub:
    def __init__(self, *, minimized: bool = False) -> None:
        self._minimized = minimized
        self.calls: list[str] = []

    def isMinimized(self) -> bool:
        return self._minimized

    def showFullScreen(self) -> None:
        self.calls.append("showFullScreen")

    def raise_(self) -> None:
        self.calls.append("raise_")

    def activateWindow(self) -> None:
        self.calls.append("activateWindow")


def test_raise_projection_window_restores_minimized_window_before_activation(monkeypatch):
    monkeypatch.setattr(window_focus.sys, "platform", "linux")
    window = _WindowStub(minimized=True)

    window_focus.raise_projection_window(window)

    assert window.calls == ["showFullScreen", "raise_", "activateWindow"]


def test_raise_projection_window_leaves_visible_window_fullscreen_state_unchanged(monkeypatch):
    monkeypatch.setattr(window_focus.sys, "platform", "linux")
    window = _WindowStub(minimized=False)

    window_focus.raise_projection_window(window)

    assert window.calls == ["raise_", "activateWindow"]
