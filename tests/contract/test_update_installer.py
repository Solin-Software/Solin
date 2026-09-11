from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from solin.core.remote import update_installer


class Settings:
    def __init__(self, path: Path):
        self.path = str(path)

    def pending_update_cleanup_path(self):
        return self.path

    def clear_pending_update_cleanup_path(self):
        self.path = ""


def test_cleanup_removes_only_managed_file(tmp_path):
    path = tmp_path / "setup.exe"
    path.write_bytes(b"installer")
    settings = Settings(path)
    update_installer.cleanup_pending_update(settings, directory=tmp_path)
    assert not path.exists()
    assert not settings.path


def test_cleanup_rejects_outside_path(tmp_path):
    path = tmp_path / "keep.exe"
    path.write_bytes(b"data")
    settings = Settings(path)
    update_installer.cleanup_pending_update(settings, directory=tmp_path / "updates")
    assert path.exists()
    assert not settings.path


def test_cleanup_retains_locked_path(tmp_path, monkeypatch):
    path = tmp_path / "setup.exe"
    settings = Settings(path)

    def locked(*args, **kwargs):
        raise PermissionError("in use")

    monkeypatch.setattr(Path, "unlink", locked)
    update_installer.cleanup_pending_update(settings, directory=tmp_path)
    assert settings.path == str(path)


@pytest.mark.parametrize(
    ("hive_name", "scope", "expected"),
    [
        ("HKEY_CURRENT_USER", "user", "/CURRENTUSER"),
        ("HKEY_LOCAL_MACHINE", "machine", "/ALLUSERS"),
    ],
)
def test_windows_installation_matches_executable_registry_scope(
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
    monkeypatch.setattr(update_installer, "winreg", fake_winreg)

    assert update_installer.windows_installation(
        str(executable)
    ) == update_installer.WindowsInstallation(install_dir, scope)


def test_windows_installation_rejects_unmatched_executable(monkeypatch, tmp_path):
    fake_winreg = SimpleNamespace(
        HKEY_CURRENT_USER=object(),
        HKEY_LOCAL_MACHINE=object(),
        KEY_READ=1,
        KEY_WOW64_64KEY=2,
        OpenKey=lambda *_args: (_ for _ in ()).throw(FileNotFoundError()),
    )
    monkeypatch.setattr(update_installer, "winreg", fake_winreg)

    with pytest.raises(RuntimeError, match="scope could not be resolved"):
        update_installer.windows_installation(str(tmp_path / "Unknown" / "Solin.exe"))


def test_unregistered_inno_layout_uses_full_setup_without_inferring_scope(
    monkeypatch, tmp_path
):
    executable = tmp_path / "Solin.exe"
    executable.write_bytes(b"app")
    (tmp_path / "unins000.exe").write_bytes(b"uninstaller")
    monkeypatch.setattr(update_installer.sys, "platform", "win32")
    monkeypatch.setattr(update_installer.sys, "executable", str(executable))
    monkeypatch.setattr(
        update_installer.ctypes,
        "windll",
        SimpleNamespace(shell32=SimpleNamespace(IsUserAnAdmin=lambda: False)),
        raising=False,
    )
    monkeypatch.setattr(
        update_installer,
        "windows_installation",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("registry missing")),
    )

    assert update_installer.windows_installation_present() is True
    assert update_installer.windows_auto_update_available() is False


class Kernel:
    def __init__(self, ready=False):
        self.ready = ready
        self.signalled = []
        self.closed = []

    def WaitForSingleObject(self, handle, timeout):
        return 0 if self.ready else 258

    def SetEvent(self, handle):
        self.signalled.append(handle)

    def CloseHandle(self, handle):
        self.closed.append(handle)


def test_installer_readiness_does_not_close_application(monkeypatch):
    installer = update_installer.UpdateInstaller()
    kernel = Kernel(ready=True)
    monkeypatch.setattr(installer, "_kernel", lambda: kernel)
    installer._ready_handle = 11
    installer._cancel_handle = 12
    signals = []
    installer.ready.connect(lambda: signals.append("ready"))
    installer._poll()
    assert signals == ["ready"]
    assert kernel.signalled == []
    # The lifecycle may reject closing (for example unsaved work).
    installer.abort()
    assert kernel.signalled == [12]
    assert kernel.closed == [11, 12]


def test_installer_accepted_shutdown_never_cancels(monkeypatch):
    installer = update_installer.UpdateInstaller()
    kernel = Kernel()
    monkeypatch.setattr(installer, "_kernel", lambda: kernel)
    installer._ready_handle = 11
    installer._cancel_handle = 12
    installer.accept_shutdown()
    installer.abort()
    assert kernel.signalled == []
    assert kernel.closed == [11, 12]


def test_installer_timeout_signals_cancellation_and_failure(monkeypatch):
    installer = update_installer.UpdateInstaller()
    kernel = Kernel()
    monkeypatch.setattr(installer, "_kernel", lambda: kernel)
    installer._ready_handle = 11
    installer._cancel_handle = 12
    installer._deadline = 0
    errors = []
    installer.failed.connect(errors.append)
    installer._poll()
    assert kernel.signalled == [12]
    assert len(errors) == 1
    assert "remains open" in errors[0]


def test_cancelled_elevation_fails_without_requesting_application_shutdown(monkeypatch):
    installer = update_installer.UpdateInstaller()
    kernel = Kernel()
    monkeypatch.setattr(installer, "_kernel", lambda: kernel)
    installer._ready_handle = 11
    installer._cancel_handle = 12
    installer._process = SimpleNamespace(poll=lambda: 2)
    errors = []
    ready = []
    installer.failed.connect(errors.append)
    installer.ready.connect(lambda: ready.append(True))

    installer._poll()

    assert ready == []
    assert kernel.signalled == [12]
    assert "cancelled or failed" in errors[0]
