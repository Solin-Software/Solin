"""Asynchronous update checks; stopping never waits on the GUI thread."""

from __future__ import annotations

import platform
import sys
from collections.abc import Callable
from pathlib import Path
from threading import Event

from PySide6.QtCore import QObject, QThread, Signal, Slot

from solin.core.foundation.constants import APP_VERSION
from solin.core.releases.channel import UpdateChannel
from solin.core.remote.github_updates import GitHubReleases
from solin.core.remote.update_policy import UpdateInfo, evaluate_update


class UpdateWorker(QObject):
    update_available = Signal(object)
    fetch_failed = Signal(str)
    finished = Signal()

    def __init__(
        self,
        language: str,
        channel: UpdateChannel,
        cache_directory: Path,
        installed_windows: bool,
        cancelled: Event,
        automatic_windows: bool = True,
    ) -> None:
        super().__init__()
        self.language = language
        self.channel = channel
        self.cache_directory = cache_directory
        self.installed_windows = installed_windows
        self.cancelled = cancelled
        self.automatic_windows = automatic_windows

    @Slot()
    def run(self) -> None:
        try:
            manifests = GitHubReleases(self.cache_directory, self.cancelled).discover(
                APP_VERSION, self.channel
            )
            operating_system = {"win32": "windows", "darwin": "macos"}.get(sys.platform, "linux")
            architecture = {"amd64": "x86_64", "aarch64": "arm64"}.get(
                platform.machine().lower(), platform.machine().lower()
            )
            os_version = (
                platform.mac_ver()[0]
                if sys.platform == "darwin"
                else platform.version()
                if sys.platform == "win32"
                else platform.libc_ver()[1]
                if platform.libc_ver()[0] == "glibc"
                else ""
            )
            info = evaluate_update(
                manifests,
                current_version=APP_VERSION,
                channel=self.channel,
                platform=operating_system,
                architecture=architecture,
                os_version=os_version,
                installed_windows=self.installed_windows,
                automatic_windows=self.automatic_windows,
                language=self.language,
            )
            if info and not self.cancelled.is_set():
                self.update_available.emit(info)
        except Exception as exc:  # noqa: BLE001 - Worker boundary must always finish the thread.
            self.fetch_failed.emit(str(exc))
        finally:
            self.finished.emit()


class UpdateService(QObject):
    update_available = Signal(object)
    finished = Signal()

    def __init__(
        self,
        language_code_provider: Callable[[], str],
        channel_provider: Callable[[], UpdateChannel],
        cache_directory: Path,
        parent: QObject | None = None,
        *,
        installed_windows_provider: Callable[[], bool],
        automatic_windows_provider: Callable[[], bool] = lambda: True,
    ) -> None:
        super().__init__(parent)
        self._language = language_code_provider
        self._channel = channel_provider
        self._directory = cache_directory
        self._installed = installed_windows_provider
        self._automatic = automatic_windows_provider
        self._thread: QThread | None = None
        self._worker: UpdateWorker | None = None
        self._cancelled = Event()
        self._stopping = False
        self._delete_when_stopped = False

    def check(self) -> None:
        if self._stopping or self._thread is not None:
            return
        self._cancelled.clear()
        self._worker = UpdateWorker(
            self._language(),
            self._channel(),
            self._directory,
            self._installed(),
            self._cancelled,
            self._automatic(),
        )
        self._thread = QThread(self)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.update_available.connect(self._on_result)
        self._worker.finished.connect(self._worker.deleteLater)
        # Queue deletion in the worker event loop before asking that loop to
        # stop. Reversing these connections can leave a live Python wrapper for
        # an object whose thread has already disappeared after a fast failure.
        self._worker.finished.connect(self._thread.quit)
        self._thread.finished.connect(self._cleanup)
        self._thread.start()

    def _on_result(self, info: UpdateInfo) -> None:
        if not self._stopping:
            self.update_available.emit(info)

    def stop(self, wait_ms: int = 0, delete_when_stopped: bool = False) -> None:
        self._stopping = True
        self._cancelled.set()
        self._delete_when_stopped |= delete_when_stopped
        if self._thread is not None:
            if wait_ms:
                self._thread.wait(wait_ms)
            if self._delete_when_stopped:
                self.setParent(None)
        elif self._delete_when_stopped:
            self.deleteLater()

    def shutdown(self) -> None:
        self.stop(delete_when_stopped=True)

    @Slot()
    def _cleanup(self) -> None:
        if self._thread is not None:
            self._thread.deleteLater()
        self._thread = None
        self._worker = None
        self.finished.emit()
        if self._delete_when_stopped:
            self.deleteLater()
