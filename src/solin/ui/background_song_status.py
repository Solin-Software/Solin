"""Presentation translation for background-song service status sources."""

from __future__ import annotations

from PySide6.QtCore import QCoreApplication

from solin.core.jw.background_song_status import (
    STATUS_CONFIGURE_MEETING,
    STATUS_DISABLED,
    STATUS_ENABLE_IN_SETTINGS,
    STATUS_LOAD_FAILED,
    STATUS_LOADING_AUDIO,
    STATUS_NO_AUDIO,
    STATUS_PLAYING,
    STATUS_READY,
    STATUS_SIGN_LANGUAGE_UNAVAILABLE,
    STATUS_STOPPED,
    STATUS_STOPPED_BEFORE_MEETING,
    STATUS_STOPPED_FOR_MEETING,
    STATUS_STOPPING,
    STATUS_WAITING_FOR_MEETING,
)

_TR_CONTEXT = "BackgroundSongService"


def translate_background_song_status(source: str) -> str:
    """Translate a known background-song status source string at the UI boundary."""
    if source == STATUS_DISABLED:
        return QCoreApplication.translate(
            _TR_CONTEXT,
            "Automatic background song is disabled.",
        )
    if source == STATUS_ENABLE_IN_SETTINGS:
        return QCoreApplication.translate(
            _TR_CONTEXT,
            "Enable automatic background song in Settings.",
        )
    if source == STATUS_STOPPING:
        return QCoreApplication.translate(_TR_CONTEXT, "Stopping background song...")
    if source == STATUS_CONFIGURE_MEETING:
        return QCoreApplication.translate(
            _TR_CONTEXT,
            "Configure the meeting day/time in Settings.",
        )
    if source == STATUS_WAITING_FOR_MEETING:
        return QCoreApplication.translate(
            _TR_CONTEXT,
            "Waiting for the next configured meeting.",
        )
    if source == STATUS_STOPPED_FOR_MEETING:
        return QCoreApplication.translate(_TR_CONTEXT, "Stopped for this meeting.")
    if source == STATUS_STOPPED_BEFORE_MEETING:
        return QCoreApplication.translate(_TR_CONTEXT, "Stopped before the meeting.")
    if source == STATUS_SIGN_LANGUAGE_UNAVAILABLE:
        return QCoreApplication.translate(
            _TR_CONTEXT,
            "Audio songs are unavailable for sign-language media.",
        )
    if source == STATUS_LOADING_AUDIO:
        return QCoreApplication.translate(_TR_CONTEXT, "Loading audio songs...")
    if source == STATUS_READY:
        return QCoreApplication.translate(_TR_CONTEXT, "Ready.")
    if source == STATUS_LOAD_FAILED:
        return QCoreApplication.translate(_TR_CONTEXT, "Could not load audio songs.")
    if source == STATUS_NO_AUDIO:
        return QCoreApplication.translate(_TR_CONTEXT, "No audio songs available.")
    if source == STATUS_PLAYING:
        return QCoreApplication.translate(_TR_CONTEXT, "Playing background song.")
    if source == STATUS_STOPPED:
        return QCoreApplication.translate(_TR_CONTEXT, "Background song stopped.")
    return source

