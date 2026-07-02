from __future__ import annotations

from PySide6.QtCore import QObject, Signal

from solin.controllers.playback_protection_controller import (
    PlaybackProtectionController,
)


class _Settings:
    def __init__(self) -> None:
        self.value = False

    def playback_protection_enabled(self) -> bool:
        return self.value

    def set_playback_protection_enabled(self, enabled: bool) -> None:
        self.value = bool(enabled)


class _Media(QObject):
    state_changed = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.is_playing = False
        self.seeks: list[int] = []

    def set_playing(self, playing: bool) -> None:
        self.is_playing = playing
        self.state_changed.emit("playing" if playing else "paused")

    def seek(self, position: int) -> None:
        self.seeks.append(position)


def test_protection_persists_and_tracks_live_playback_lock() -> None:
    settings = _Settings()
    media = _Media()
    controller = PlaybackProtectionController(settings, media)
    enabled_changes: list[bool] = []
    locked_changes: list[bool] = []
    controller.enabledChanged.connect(
        lambda: enabled_changes.append(controller.enabled)
    )
    controller.lockedChanged.connect(
        lambda: locked_changes.append(controller.locked)
    )

    controller.set_enabled(True)
    media.set_playing(True)
    media.set_playing(False)

    assert settings.value is True
    assert enabled_changes == [True]
    assert locked_changes == [True, False]
    assert controller.locked is False


def test_manual_changes_and_seeks_are_rejected_only_while_locked() -> None:
    settings = _Settings()
    media = _Media()
    controller = PlaybackProtectionController(settings, media)
    blocked: list[bool] = []
    controller.manualChangeBlocked.connect(lambda: blocked.append(True))
    controller.set_enabled(True)
    media.set_playing(True)

    assert controller.allow_manual_projection_change() is False
    assert controller.request_seek(1_500) is False
    assert blocked == [True]
    assert media.seeks == []

    media.set_playing(False)

    assert controller.allow_manual_projection_change() is True
    assert controller.request_seek(2_000) is True
    assert media.seeks == [2_000]


def test_disabling_protection_unlocks_current_playback_immediately() -> None:
    settings = _Settings()
    settings.value = True
    media = _Media()
    media.is_playing = True
    controller = PlaybackProtectionController(settings, media)

    controller.set_enabled(False)

    assert controller.enabled is False
    assert controller.locked is False
    assert controller.request_seek(750) is True
    assert media.seeks == [750]
