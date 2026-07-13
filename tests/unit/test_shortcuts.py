from __future__ import annotations

import subprocess

from solin.core.integrations.automation import shortcuts


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
