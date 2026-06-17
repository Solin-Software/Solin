from __future__ import annotations

from solin.core.integrations.automation.zoom import native


def test_initialize_com_noops_off_windows(monkeypatch):
    monkeypatch.setattr(native.sys, "platform", "linux")

    native.initialize_com_for_current_thread()


def test_share_selection_dialog_open_returns_false_off_windows(monkeypatch):
    monkeypatch.setattr(native.sys, "platform", "linux")

    assert native.share_selection_dialog_open() is False
