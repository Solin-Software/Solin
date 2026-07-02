"""Central policy for preventing accidental playback changes."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Property, QObject, Signal, Slot

from solin.core.media.settings import MediaSettingsStore


class PlaybackProtectionController(QObject):
    """Own persisted protection state and authorize user playback actions."""

    enabledChanged = Signal()
    lockedChanged = Signal()
    manualChangeBlocked = Signal()

    def __init__(
        self,
        settings: MediaSettingsStore,
        media_controller: Any,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self._media_controller = media_controller
        self._last_locked = self._calculate_locked()
        media_controller.state_changed.connect(self._on_playback_state_changed)

    @Property(bool, notify=enabledChanged)
    def enabled(self) -> bool:
        return self._settings.playback_protection_enabled()

    @Property(bool, notify=lockedChanged)
    def locked(self) -> bool:
        return self._calculate_locked()

    @Slot(bool)
    def setEnabled(self, enabled: bool) -> None:  # noqa: N802 - QML API
        self.set_enabled(enabled)

    def set_enabled(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if enabled == self.enabled:
            return
        self._settings.set_playback_protection_enabled(enabled)
        self.enabledChanged.emit()
        self._emit_locked_if_changed()

    def allow_manual_projection_change(self, *, notify: bool = True) -> bool:
        """Return whether a user-initiated projection replacement may proceed."""

        if not self.locked:
            return True
        if notify:
            self.manualChangeBlocked.emit()
        return False

    @Slot(int)
    def requestSeek(self, position: int) -> None:  # noqa: N802 - QML/Qt API
        self.request_seek(position)

    def request_seek(self, position: int) -> bool:
        """Apply a user seek only when playback protection permits it."""

        if self.locked:
            return False
        self._media_controller.seek(int(position))
        return True

    def _on_playback_state_changed(self, _state: object) -> None:
        self._emit_locked_if_changed()

    def _calculate_locked(self) -> bool:
        return self.enabled and bool(self._media_controller.is_playing)

    def _emit_locked_if_changed(self) -> None:
        locked = self._calculate_locked()
        if locked == self._last_locked:
            return
        self._last_locked = locked
        self.lockedChanged.emit()


__all__ = ["PlaybackProtectionController"]
