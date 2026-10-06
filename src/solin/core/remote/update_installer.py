from __future__ import annotations

import ctypes
import logging
import os
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from PySide6.QtCore import QObject, QTimer, Signal

try:
    import winreg
except ImportError:
    winreg = None  # type: ignore[assignment]

log = logging.getLogger(__name__)
_INSTALLER_REGISTRY_PATH = r"Software\Solin\Solin"


class UpdateCleanupSettings(Protocol):
    def pending_update_cleanup_path(self) -> str: ...
    def clear_pending_update_cleanup_path(self) -> None: ...
    def set_pending_update_cleanup_path(self, path: str) -> None: ...


def cleanup_pending_update(settings: UpdateCleanupSettings, *, directory: Path) -> None:
    value = settings.pending_update_cleanup_path()
    if not value:
        return
    path = Path(value)
    # Never follow a recorded path outside our download directory.
    if path.is_symlink() or path.resolve().parent != directory.resolve():
        settings.clear_pending_update_cleanup_path()
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        log.debug("Update package remains in use; cleanup will retry", exc_info=True)
        return
    settings.clear_pending_update_cleanup_path()


@dataclass(frozen=True)
class WindowsInstallation:
    path: Path
    scope: str


def windows_installation_present() -> bool:
    if sys.platform != "win32":
        return False
    try:
        windows_installation()
        return True
    except (OSError, RuntimeError):
        # An Inno uninstaller beside the running executable identifies an
        # installed layout even when its registry metadata is missing or does
        # not match. It must receive the full setup for assisted repair; scope
        # is deliberately left unresolved, so automatic installation remains
        # unavailable.
        application_directory = Path(sys.executable).resolve().parent
        return any(application_directory.glob("unins[0-9][0-9][0-9].exe"))


def windows_auto_update_available() -> bool:
    if sys.platform != "win32":
        return False
    try:
        if ctypes.windll.shell32.IsUserAnAdmin():
            return False
        windows_installation()
        return True
    except (OSError, RuntimeError):
        return False


def windows_installation(executable: str | None = None) -> WindowsInstallation:
    if winreg is None:
        raise RuntimeError("Windows installation metadata is unavailable.")

    executable_path = Path(executable or sys.executable).resolve()
    application_directory = executable_path.parent
    matches: list[WindowsInstallation] = []
    for hive, expected_scope in (
        (winreg.HKEY_CURRENT_USER, "user"),
        (winreg.HKEY_LOCAL_MACHINE, "machine"),
    ):
        try:
            with winreg.OpenKey(
                hive,
                _INSTALLER_REGISTRY_PATH,
                0,
                winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
            ) as key:
                install_path = Path(winreg.QueryValueEx(key, "InstallPath")[0])
                install_scope = str(winreg.QueryValueEx(key, "InstallScope")[0])
        except OSError:
            continue
        if (
            install_scope.casefold() == expected_scope
            and install_path.resolve() == application_directory
        ):
            matches.append(WindowsInstallation(application_directory, expected_scope))
    if len(matches) != 1:
        raise RuntimeError("The installed Solin scope could not be resolved for this executable.")
    return matches[0]


class UpdateInstaller(QObject):
    """Wait for the initialized installer before requesting a graceful app close."""

    ready = Signal()
    failed = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._timer = QTimer(self)
        self._timer.setInterval(100)
        self._timer.timeout.connect(self._poll)
        self._process: subprocess.Popen | None = None
        self._ready_handle = 0
        self._cancel_handle = 0
        self._accepted = False
        self._deadline = 0.0
        self._path = Path()
        self._installation: WindowsInstallation | None = None
        self._token = ""

    def start(self, path: str) -> None:
        if self._timer.isActive():
            raise RuntimeError("An installer is already starting.")
        self._path = Path(path).resolve(strict=True)
        if not self._path.is_file():
            raise FileNotFoundError(path)
        if not windows_auto_update_available():
            raise RuntimeError("This installation requires assisted installation.")
        self._installation = windows_installation()
        try:
            self._launch()
        except (OSError, RuntimeError, subprocess.SubprocessError):
            self.abort()
            raise
        self._timer.start()

    @staticmethod
    def _kernel():
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateEventW.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_wchar_p,
        ]
        kernel.CreateEventW.restype = ctypes.c_void_p
        kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel.SetEvent.argtypes = [ctypes.c_void_p]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        return kernel

    def _launch(self) -> None:
        assert self._installation is not None
        kernel = self._kernel()
        self._token = uuid.uuid4().hex
        self._ready_handle = kernel.CreateEventW(
            None, True, False, "Local\\SolinUpdateReady-" + self._token
        )
        self._cancel_handle = kernel.CreateEventW(
            None, True, False, "Local\\SolinUpdateCancel-" + self._token
        )
        if not self._ready_handle or not self._cancel_handle:
            raise ctypes.WinError(ctypes.get_last_error())
        self._process = subprocess.Popen(
            [
                str(self._path),
                "/SILENT",
                "/NORESTART",
                "/NOCLOSEAPPLICATIONS",
                "/ALLUSERS" if self._installation.scope == "machine" else "/CURRENTUSER",
                f"/DIR={self._installation.path}",
                f"/SOLINUPDATE={self._token}",
                f"/SOLINPID={os.getpid()}",
            ],
            close_fds=True,
        )
        self._deadline = time.monotonic() + 180

    def _poll(self) -> None:
        try:
            if (
                self._ready_handle
                and self._kernel().WaitForSingleObject(self._ready_handle, 0) == 0
            ):
                self._timer.stop()
                self.ready.emit()
            elif self._process is not None and self._process.poll() is not None:
                raise RuntimeError(
                    "Installer startup was cancelled or failed. The application remains open."
                )
            elif time.monotonic() > self._deadline:
                raise RuntimeError(
                    "The installer did not become ready. The application remains open."
                )
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            self.abort()
            self.failed.emit(str(exc))

    def accept_shutdown(self) -> None:
        self._accepted = True
        self._close_handles()

    def _close_handles(self) -> None:
        for handle in (self._ready_handle, self._cancel_handle):
            if handle:
                self._kernel().CloseHandle(handle)
        self._ready_handle = self._cancel_handle = 0

    def abort(self) -> None:
        self._timer.stop()
        if self._cancel_handle and not self._accepted:
            self._kernel().SetEvent(self._cancel_handle)
        self._close_handles()
