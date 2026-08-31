from __future__ import annotations

import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Protocol

from PySide6.QtCore import QObject, QUrl, Signal
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

try:
    import winreg
except ImportError:  # pragma: no cover - Windows-only installation metadata
    winreg = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

_INSTALLER_REGISTRY_PATH = r"Software\Solin\Solin"


class PatchCleanupSettings(Protocol):
    def pending_patch_cleanup_path(self) -> str: ...

    def clear_pending_patch_cleanup_path(self) -> None: ...

    def set_pending_patch_cleanup_path(self, path: str) -> None: ...


def cleanup_pending_patch(settings: PatchCleanupSettings) -> None:
    """Remove the patch executable recorded by the previous app session."""
    path = settings.pending_patch_cleanup_path()
    if not path:
        return

    settings.clear_pending_patch_cleanup_path()
    try:
        if os.path.isfile(path):
            os.remove(path)
            log.debug("[Update] patch temp removed: %s", path)
    except OSError as exc:
        log.debug("[Update] failed to remove patch temp: %s", exc)


def save_pending_patch_cleanup(
    settings: PatchCleanupSettings,
    path: str,
) -> None:
    settings.set_pending_patch_cleanup_path(path)


def _windows_patch_install_mode(executable: str | None = None) -> str:
    if winreg is None:
        raise RuntimeError("Windows installation metadata is unavailable.")

    executable_path = Path(executable or sys.executable).resolve()
    application_directory = executable_path.parent
    matches: list[str] = []
    for hive, mode, expected_scope in (
        (winreg.HKEY_CURRENT_USER, "/CURRENTUSER", "user"),
        (winreg.HKEY_LOCAL_MACHINE, "/ALLUSERS", "machine"),
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
            matches.append(mode)
    if len(matches) != 1:
        raise RuntimeError("The installed Solin scope could not be resolved for this executable.")
    return matches[0]


def launch_patch_installer(path: str) -> None:
    if not path or not os.path.isfile(path):
        raise FileNotFoundError(path)

    args = [
        path,
        "/SILENT",
        "/CLOSEAPPLICATIONS",
        "/RESTARTAPPLICATIONS",
    ]
    if sys.platform == "win32":
        args.append(_windows_patch_install_mode())
        detached_process = 0x00000008
        subprocess.Popen(
            args,
            creationflags=detached_process,
            close_fds=True,
        )
        return
    subprocess.Popen(args, close_fds=True)


class PatchDownloadWorker(QObject):
    """Event-loop-driven patch downloader used by the update presentation."""

    progress = Signal(int)
    finished = Signal(str)
    failed = Signal(str)

    def __init__(self, url: str, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._url = url
        self._path = ""
        self._file = None
        self._reply: QNetworkReply | None = None
        self._network = QNetworkAccessManager(self)

    def start(self) -> None:
        suffix = os.path.splitext(self._url.split("?", 1)[0])[-1] or ".exe"
        fd, self._path = tempfile.mkstemp(suffix=suffix, prefix="Solin_patch_")
        self._file = os.fdopen(fd, "wb")

        request = QNetworkRequest(QUrl(self._url))
        request.setAttribute(
            QNetworkRequest.Attribute.RedirectPolicyAttribute,
            QNetworkRequest.RedirectPolicy.NoLessSafeRedirectPolicy,
        )
        self._reply = self._network.get(request)
        self._reply.downloadProgress.connect(self._on_progress)
        self._reply.readyRead.connect(self._on_data)
        self._reply.finished.connect(self._on_finished)
        self._reply.errorOccurred.connect(self._on_error)

    def abort(self) -> None:
        if self._reply is not None:
            self._reply.abort()

    def _on_progress(self, received: int, total: int) -> None:
        if total > 0:
            self.progress.emit(int(received * 100 / total))

    def _on_data(self) -> None:
        if self._reply is None or self._file is None:
            return
        data = self._reply.readAll()
        if data:
            self._file.write(bytes(data))

    def _on_finished(self) -> None:
        if self._file is not None:
            self._file.flush()
            self._file.close()
            self._file = None
        if self._reply is not None and self._reply.error() == QNetworkReply.NetworkError.NoError:
            self.finished.emit(self._path)

    def _on_error(self, error: QNetworkReply.NetworkError) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None
        try:
            if self._path and os.path.isfile(self._path):
                os.remove(self._path)
        except OSError:
            pass
        message = self._reply.errorString() if self._reply else str(error)
        self.failed.emit(message)
