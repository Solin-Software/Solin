from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace

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
    monkeypatch.setattr(patch_installer.os.path, "isfile", lambda path: True)
    monkeypatch.setattr(patch_installer.os, "remove", removed.append)

    patch_installer.cleanup_pending_patch(settings)

    assert settings.cleared == 1
    assert removed == ["patch.exe"]


def test_save_pending_patch_cleanup_uses_installation_settings():
    settings = _SettingsStub()

    patch_installer.save_pending_patch_cleanup(settings, "patch.exe")

    assert settings.saved == ["patch.exe"]


def test_launch_patch_installer_uses_machine_scope_for_machine_install(monkeypatch):
    launches: list[tuple[list[str], dict]] = []
    monkeypatch.setattr(patch_installer.os.path, "isfile", lambda path: True)
    monkeypatch.setattr(patch_installer.sys, "platform", "win32")
    monkeypatch.setattr(patch_installer, "_windows_patch_install_mode", lambda: "/ALLUSERS")
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
                "/ALLUSERS",
            ],
            {"creationflags": 0x00000008, "close_fds": True},
        )
    ]


@pytest.mark.parametrize(
    ("hive_name", "scope", "expected"),
    [
        ("HKEY_CURRENT_USER", "user", "/CURRENTUSER"),
        ("HKEY_LOCAL_MACHINE", "machine", "/ALLUSERS"),
    ],
)
def test_windows_patch_install_mode_matches_executable_registry_scope(
    monkeypatch, tmp_path, hive_name, scope, expected
):
    install_dir = tmp_path / "Solin"
    executable = install_dir / "Solin.exe"
    executable.parent.mkdir()
    executable.write_bytes(b"exe")
    user_hive = object()
    machine_hive = object()
    target_hive = user_hive if hive_name == "HKEY_CURRENT_USER" else machine_hive
    values = {
        target_hive: {
            "InstallPath": str(install_dir),
            "InstallScope": scope,
        }
    }

    def open_key(hive, _path, _reserved, _access):
        if hive not in values:
            raise FileNotFoundError
        return nullcontext(hive)

    fake_winreg = SimpleNamespace(
        HKEY_CURRENT_USER=user_hive,
        HKEY_LOCAL_MACHINE=machine_hive,
        KEY_READ=1,
        KEY_WOW64_64KEY=2,
        OpenKey=open_key,
        QueryValueEx=lambda hive, name: (values[hive][name], 1),
    )
    monkeypatch.setattr(patch_installer, "winreg", fake_winreg)

    assert patch_installer._windows_patch_install_mode(str(executable)) == expected


def test_windows_patch_install_mode_rejects_unmatched_executable(monkeypatch, tmp_path):
    fake_winreg = SimpleNamespace(
        HKEY_CURRENT_USER=object(),
        HKEY_LOCAL_MACHINE=object(),
        KEY_READ=1,
        KEY_WOW64_64KEY=2,
        OpenKey=lambda *_args: (_ for _ in ()).throw(FileNotFoundError()),
    )
    monkeypatch.setattr(patch_installer, "winreg", fake_winreg)

    with pytest.raises(RuntimeError, match="scope could not be resolved"):
        patch_installer._windows_patch_install_mode(str(tmp_path / "Unknown" / "Solin.exe"))


def test_launch_patch_installer_rejects_missing_file(monkeypatch):
    monkeypatch.setattr(patch_installer.os.path, "isfile", lambda path: False)

    with pytest.raises(FileNotFoundError):
        patch_installer.launch_patch_installer("missing.exe")
