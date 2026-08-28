"""Localized presentation for background-song status values."""

from __future__ import annotations

from PySide6.QtCore import QCoreApplication, QT_TRANSLATE_NOOP

from solin.core.jw.background_song_status import (
    BackgroundSongStatus,
    BackgroundSongStatusCode,
)

_TR_CONTEXT = "BackgroundSongStatus"
_STATUS_SOURCES = {
    BackgroundSongStatusCode.DISABLED: QT_TRANSLATE_NOOP(
        "BackgroundSongStatus",
        "Automatic background song is disabled.",
    ),
    BackgroundSongStatusCode.ENABLE_IN_SETTINGS: QT_TRANSLATE_NOOP(
        "BackgroundSongStatus",
        "Enable automatic background song in Settings.",
    ),
    BackgroundSongStatusCode.STOPPING: QT_TRANSLATE_NOOP(
        "BackgroundSongStatus",
        "Stopping background song...",
    ),
    BackgroundSongStatusCode.CONFIGURE_MEETING: QT_TRANSLATE_NOOP(
        "BackgroundSongStatus",
        "Configure the meeting day/time in Settings.",
    ),
    BackgroundSongStatusCode.WAITING_FOR_MEETING: QT_TRANSLATE_NOOP(
        "BackgroundSongStatus",
        "Waiting for the next configured meeting.",
    ),
    BackgroundSongStatusCode.STOPPED_FOR_MEETING: QT_TRANSLATE_NOOP(
        "BackgroundSongStatus",
        "Stopped for this meeting.",
    ),
    BackgroundSongStatusCode.STOPPED_BEFORE_MEETING: QT_TRANSLATE_NOOP(
        "BackgroundSongStatus",
        "Stopped before the meeting.",
    ),
    BackgroundSongStatusCode.SIGN_LANGUAGE_UNAVAILABLE: QT_TRANSLATE_NOOP(
        "BackgroundSongStatus",
        "Audio songs are unavailable for sign-language media.",
    ),
    BackgroundSongStatusCode.LOADING_AUDIO: QT_TRANSLATE_NOOP(
        "BackgroundSongStatus",
        "Loading audio songs...",
    ),
    BackgroundSongStatusCode.READY: QT_TRANSLATE_NOOP(
        "BackgroundSongStatus",
        "Ready.",
    ),
    BackgroundSongStatusCode.LOAD_FAILED: QT_TRANSLATE_NOOP(
        "BackgroundSongStatus",
        "Could not load audio songs.",
    ),
    BackgroundSongStatusCode.NO_AUDIO: QT_TRANSLATE_NOOP(
        "BackgroundSongStatus",
        "No audio songs available.",
    ),
    BackgroundSongStatusCode.PLAYING: QT_TRANSLATE_NOOP(
        "BackgroundSongStatus",
        "Playing background song.",
    ),
    BackgroundSongStatusCode.PLAYBACK_ERROR: QT_TRANSLATE_NOOP(
        "BackgroundSongStatus",
        "Background-song playback error:\n{error}",
    ),
    BackgroundSongStatusCode.STOPPED: QT_TRANSLATE_NOOP(
        "BackgroundSongStatus",
        "Background song stopped.",
    ),
}
_LOAD_FAILED_DETAIL_SOURCE = QT_TRANSLATE_NOOP(
    "BackgroundSongStatus",
    "Could not load audio songs.\n{error}",
)


def translate_background_song_status(status: BackgroundSongStatus | None) -> str:
    """Translate one typed service status at the UI boundary."""

    if status is None:
        return ""
    source = _STATUS_SOURCES[status.code]
    translated = QCoreApplication.translate(_TR_CONTEXT, source)
    if status.code is BackgroundSongStatusCode.PLAYBACK_ERROR:
        return translated.format(error=status.detail)
    if status.code is BackgroundSongStatusCode.LOAD_FAILED and status.detail:
        return QCoreApplication.translate(
            _TR_CONTEXT,
            _LOAD_FAILED_DETAIL_SOURCE,
        ).format(error=status.detail)
    return translated


assert set(_STATUS_SOURCES) == set(BackgroundSongStatusCode)

__all__ = ["translate_background_song_status"]
