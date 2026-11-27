"""Qt-free media playback-state vocabulary.

The app-wide play/pause/stop state is carried by
``MediaController.state_changed``. libobs is the only media engine, so the
state originates from the sidecar's ``obs_media_state`` integers and is mapped to
these three members for the UI. The value never crosses a process boundary — the
wire vocabularies (``core.scenes.media_control.MediaPlaybackState`` and
``core.remote_control.media.contracts.PlaybackState``) are separate and unchanged.
"""

from __future__ import annotations

from enum import Enum

from solin.core.scenes.media_control import MediaPlaybackState


class SolinPlaybackState(Enum):
    """The three playback states the UI binds to."""

    STOPPED = 0
    PLAYING = 1
    PAUSED = 2


# Unlisted engine states resolve to STOPPED.
ENGINE_STATE_TO_SOLIN: dict[MediaPlaybackState, SolinPlaybackState] = {
    MediaPlaybackState.PLAYING: SolinPlaybackState.PLAYING,
    MediaPlaybackState.OPENING: SolinPlaybackState.PAUSED,
    MediaPlaybackState.BUFFERING: SolinPlaybackState.PAUSED,
    MediaPlaybackState.PAUSED: SolinPlaybackState.PAUSED,
}


__all__ = [
    "SolinPlaybackState",
    "ENGINE_STATE_TO_SOLIN",
]
