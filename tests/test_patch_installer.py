from __future__ import annotations

import pytest

from solin.core.remote import patch_installer


class _SettingsStub:
    def __init__(self, path: str = "") -> None:
        self.path = path
        self.cleared = 0
        self.saved: list[str] = []

    def pending_patch_cleanup_path(self) -> str:
        return self.path

    def clear_pending_patch_cleanup_path(self) -> None:
        self.cleared += 1

    def set_pending_patch_cleanup_path(self, path: str) -> None:
        self.saved.append(path)


def test_cleanup_pending_patch_clears_setting_and_removes_file(monkeypatch):
    settings = _SettingsStub("patch.exe")
    removed: list[str] = []
    monkeypatch.setattr(
        patch_installer.InstallationSettingsStore,
        "create",
        lambda: settings,
    )
    monkeypatch.setattr(patch_installer.os.path, "isfile", lambda path: True)
    monkeypatch.setattr(patch_installer.os, "remove", removed.append)

    patch_installer.cleanup_pending_patch()

    assert settings.cleared == 1
    assert removed == ["patch.exe"]


def test_save_pending_patch_cleanup_uses_installation_settings(monkeypatch):
    settings = _SettingsStub()
    monkeypatch.setattr(
        patch_installer.InstallationSettingsStore,
        "create",
        lambda: settings,
    )

    patch_installer.save_pending_patch_cleanup("patch.exe")

    assert settings.saved == ["patch.exe"]


def test_launch_patch_installer_uses_silent_detached_windows_process(monkeypatch):
    launches: list[tuple[list[str], dict]] = []
    monkeypatch.setattr(patch_installer.os.path, "isfile", lambda path: True)
    monkeypatch.setattr(patch_installer.sys, "platform", "win32")
    monkeypatch.setattr(
        patch_installer.subprocess,
        "Popen",
        lambda args, **kwargs: launches.append((args, kwargs)),
    )

    patch_installer.launch_patch_installer("patch.exe")

    assert launches == [
        (
            [
                "patch.exe",
                "/SILENT",
                "/CLOSEAPPLICATIONS",
                "/RESTARTAPPLICATIONS",
            ],
            {"creationflags": 0x00000008, "close_fds": True},
        )
    ]


def test_launch_patch_installer_rejects_missing_file(monkeypatch):
    monkeypatch.setattr(patch_installer.os.path, "isfile", lambda path: False)

    with pytest.raises(FileNotFoundError):
        patch_installer.launch_patch_installer("missing.exe")
