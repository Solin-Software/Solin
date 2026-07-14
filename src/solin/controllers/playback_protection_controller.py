"""Central policy for preventing accidental playback changes."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QEvent, Property, QObject, Signal, Slot
from PySide6.QtWidgets import QApplication

from solin.core.media.settings import MediaSettingsStore


class PlaybackProtectionController(QObject):
    """Own persisted protection state and authorize user playback actions."""

    _BLOCKED_AUTOMATION_EVENT_TYPES = frozenset(
        {
            QEvent.Type.MouseButtonPress,
            QEvent.Type.MouseButtonRelease,
            QEvent.Type.MouseButtonDblClick,
        }
    )

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
        self._automation_locks: set[str] = set()
        self._last_locked = self._calculate_locked()
        media_controller.state_changed.connect(self._on_playback_state_changed)
        application = QApplication.instance()
        if application is not None:
            application.installEventFilter(self)

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

    @property
    def automation_locked(self) -> bool:
        return bool(self._automation_locks)

    def acquire_automation_lock(self, owner: str) -> None:
        """Block Solin pointer interaction for one active desktop automation."""
        owner = str(owner).strip()
        if not owner:
            raise ValueError("automation lock owner must not be empty")
        if owner in self._automation_locks:
            return
        self._automation_locks.add(owner)
        self._emit_locked_if_changed()

    def release_automation_lock(self, owner: str) -> None:
        """Release a previously acquired desktop-automation interaction lock."""
        owner = str(owner).strip()
        if owner not in self._automation_locks:
            return
        self._automation_locks.remove(owner)
        self._emit_locked_if_changed()

    def allow_manual_projection_change(self, *, notify: bool = True) -> bool:
        """Return whether a user-initiated projection replacement may proceed."""

        if self.automation_locked:
            return False
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
        return self.automation_locked or (
            self.enabled and bool(self._media_controller.is_playing)
        )

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802
        """Consume injected clicks that land in Solin during desktop automation."""
        if (
            self.automation_locked
            and event.type() in self._BLOCKED_AUTOMATION_EVENT_TYPES
        ):
            return True
        return super().eventFilter(watched, event)

    def _emit_locked_if_changed(self) -> None:
        locked = self._calculate_locked()
        if locked == self._last_locked:
            return
        self._last_locked = locked
        self.lockedChanged.emit()


__all__ = ["PlaybackProtectionController"]
