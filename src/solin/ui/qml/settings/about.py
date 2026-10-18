"""Installation-scoped About preferences and lazy release-note presentation."""

from __future__ import annotations

from collections.abc import Callable
from threading import Event
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QCoreApplication, QObject, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices

from solin.core.foundation.settings_store import InstallationSettingsStore
from solin.core.releases.channel import UpdateChannel
from solin.core.releases.version import ReleaseVersion
from solin.core.remote.release_history import ReleaseHistoryCursorError
from solin.core.remote.urls import is_safe_remote_url
from solin.ui.async_load import AsyncLoadHandle
from solin.ui.release_notes import release_notes_html

from .domain import SettingsDomain

if TYPE_CHECKING:
    from solin.core.remote.github_updates import GitHubReleases


class AboutSettings(SettingsDomain):
    update_channel_changed = Signal()

    def __init__(
        self,
        installation_settings: InstallationSettingsStore,
        history_client_factory: Callable[[Event], GitHubReleases],
        language_code_provider: Callable[[], str],
        installed_version: str,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._settings = installation_settings
        self._history_factory = history_client_factory
        self._language_provider = language_code_provider
        self._installed = ReleaseVersion.parse(installed_version) or ReleaseVersion.from_tag(
            installed_version
        )
        self._language = self._language_provider()
        self._activated = False
        self._closed = False
        self._generation = 0
        self._cursor: str | None = None
        self._load: AsyncLoadHandle | None = None
        self.publish(
            installedVersion=self._installed.display_version if self._installed else installed_version,
            installedIsBeta=bool(self._installed and self._installed.is_beta),
            betaUpdatesEnabled=self._settings.update_channel() == UpdateChannel.BETA,
            includeBetaNotes=self._settings.release_history_include_beta(),
            historyEntries=[], historyBusy=False, historyLoaded=False,
            historyHasMore=False, historyError="",
        )

    @Slot(str, "QVariant")
    def setValue(self, key: str, value: Any) -> None:  # noqa: N802 - QML API
        if self._closed:
            return
        if key == "betaUpdatesEnabled":
            enabled = bool(value)
            if enabled == self._state[key]:
                return
            self._settings.set_update_channel(UpdateChannel.BETA if enabled else UpdateChannel.STABLE)
            self.publish(betaUpdatesEnabled=enabled)
            self.update_channel_changed.emit()
        elif key == "includeBetaNotes":
            enabled = bool(value)
            if enabled == self._state[key]:
                return
            self._settings.set_release_history_include_beta(enabled)
            self.publish(includeBetaNotes=enabled)
            self._reset_history()
        else:
            super().setValue(key, value)

    @Slot()
    def activate(self) -> None:
        """Start the first request only when the About page is instantiated."""
        if self._closed or self._activated:
            return
        self._activated = True
        self._language = self._language_provider()
        self._request_page()

    @Slot()
    def loadMore(self) -> None:  # noqa: N802 - QML API
        if self._activated and self._state["historyHasMore"]:
            self._request_page()

    @Slot()
    def retry(self) -> None:
        if self._activated and self._state["historyError"]:
            self._request_page()

    @Slot(str, bool)
    def setReleaseExpanded(self, version: str, expanded: bool) -> None:  # noqa: N802
        if not self._closed:
            self.publish(historyEntries=[
                {**entry, "expanded": expanded} if entry["version"] == version else entry
                for entry in self._state["historyEntries"]
            ])

    @Slot(str)
    def openLink(self, url: str) -> None:  # noqa: N802 - QML API
        if self._closed:
            return
        if is_safe_remote_url(url):
            QDesktopServices.openUrl(QUrl(url))

    def refresh_language(self) -> None:
        if self._closed:
            return
        language = self._language_provider()
        if language != self._language:
            self._language = language
            self._reset_history()
        elif self._state["historyError"]:
            self.publish(historyError=self._error_message())

    def _reset_history(self) -> None:
        self._generation += 1
        self._cancel_load()
        self._cursor = None
        self.publish(historyEntries=[], historyBusy=False, historyLoaded=False,
                     historyHasMore=False, historyError="")
        if self._activated:
            self._request_page()

    def _request_page(self) -> None:
        if self._closed or self._state["historyBusy"]:
            return
        generation, language = self._generation, self._language
        include_beta, cursor = self._state["includeBetaNotes"], self._cursor
        cancelled = Event()
        factory, installed = self._history_factory, self._installed

        def load():
            client = factory(cancelled)
            reset = False
            try:
                page = client.history(include_beta=include_beta, language=language,
                                      cursor=cursor, page_size=5)
            except ReleaseHistoryCursorError:
                if cursor is None or cancelled.is_set():
                    raise
                page = client.history(include_beta=include_beta, language=language,
                                      cursor=None, page_size=5)
                reset = True
            entries = [{
                "version": item.version.display_version, "isBeta": item.version.is_beta,
                "isInstalled": item.version == installed,
                "html": release_notes_html(item.markdown), "language": item.language,
            } for item in page.items if not cancelled.is_set()]
            return entries, page.next_cursor, reset

        def current() -> bool:
            return (not self._closed and generation == self._generation
                    and language == self._language_provider())

        def apply(result) -> None:
            if not current():
                return
            page_entries, next_cursor, reset = result
            entries = [] if reset else list(self._state["historyEntries"])
            known = {entry["version"] for entry in entries}
            for item in page_entries:
                version = item["version"]
                if version in known:
                    continue
                known.add(version)
                entries.append({**item, "expanded": False})
            self._cursor = next_cursor
            self.publish(historyEntries=entries, historyBusy=False, historyLoaded=True,
                         historyHasMore=next_cursor is not None, historyError="")

        def failed(_message: str) -> None:
            if current():
                self.publish(historyBusy=False, historyError=self._error_message())

        handle = AsyncLoadHandle(load, apply, self, thread_name_prefix="about-release-history",
                                 cancel_load=cancelled.set)
        handle.failed.connect(failed)
        handle.completed.connect(handle.deleteLater)
        handle.failed.connect(handle.deleteLater)
        handle.completed.connect(lambda: self._forget_load(handle))
        handle.failed.connect(lambda: self._forget_load(handle))
        self._load = handle
        self.publish(historyBusy=True, historyError="")
        handle.start()

    def _forget_load(self, handle: AsyncLoadHandle) -> None:
        if self._load is handle:
            self._load = None
            # Language delivery can precede the host's retranslation event.
            # Reject the old result and leave the consumer with a fresh request.
            if not self._closed and self._language_provider() != self._language:
                self.refresh_language()

    @staticmethod
    def _error_message() -> str:
        return QCoreApplication.translate("SettingsWidget", "Could not load release notes.")

    def _cancel_load(self) -> None:
        handle, self._load = self._load, None
        if handle is not None:
            handle.cancel()
            handle.deleteLater()

    def cleanup(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._generation += 1
        self._cancel_load()
