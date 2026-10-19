from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QCoreApplication

from solin.core.integrations.automation import shortcuts
from solin.core.integrations.automation.auto_key_actions import (
    AutoKeyAction,
    EVENT_MEDIA_STARTED,
    EVENT_ZOOM_BEFORE_MEETING,
    EVENT_ZOOM_MEETING_STARTED,
    EVENT_ZOOM_AFTER_MEETING,
)


def test_linux_chord_clears_stray_modifiers_and_checks_xdotool_result(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(shortcuts.shutil, "which", lambda _name: "/bin/xdotool")

    def _run(arguments, **_kwargs):
        calls.append(arguments)
        return subprocess.CompletedProcess(arguments, 1)

    monkeypatch.setattr(shortcuts.subprocess, "run", _run)

    assert shortcuts._send_chord_linux(["ctrl", "shift"], "S") is False
    assert calls == [
        ["/bin/xdotool", "key", "--clearmodifiers", "ctrl+shift+S"]
    ]


def test_linux_chord_reports_success_only_for_successful_xdotool(monkeypatch):
    monkeypatch.setattr(shortcuts.shutil, "which", lambda _name: "/bin/xdotool")
    monkeypatch.setattr(
        shortcuts.subprocess,
        "run",
        lambda arguments, **_kwargs: subprocess.CompletedProcess(arguments, 0),
    )

    assert shortcuts._send_chord_linux(["alt"], "S") is True


@pytest.mark.parametrize("platform", ["win32", "darwin", "linux"])
@pytest.mark.parametrize("enabled", [True, False])
def test_dispatch_respects_event_platform_and_enabled_actions(monkeypatch, platform, enabled):
    events = [EVENT_MEDIA_STARTED, EVENT_ZOOM_BEFORE_MEETING,
              EVENT_ZOOM_MEETING_STARTED, EVENT_ZOOM_AFTER_MEETING]
    actions = [AutoKeyAction(event, event, f"Ctrl+{i}") for i, event in enumerate(events, 1)]
    actions.append(AutoKeyAction("disabled", EVENT_MEDIA_STARTED, "Ctrl+9", enabled=False))
    dispatcher = shortcuts.AutoKeyDispatcher(SimpleNamespace(
        is_enabled=lambda: enabled, actions=lambda: actions,
    ))
    monkeypatch.setattr(shortcuts, "sys", SimpleNamespace(platform=platform))
    sent = []
    monkeypatch.setattr(dispatcher, "_send", sent.append)
    try:
        for event in events:
            dispatcher.dispatch(event)
        QCoreApplication.processEvents()
        expected = actions[:4 if platform == "win32" else 1] if enabled else []
        assert sent == [action.sequence for action in expected]
    finally:
        dispatcher.deleteLater()
