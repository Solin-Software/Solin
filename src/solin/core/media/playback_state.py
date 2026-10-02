"""Qt-free media playback-state vocabulary.

Replaces ``QMediaPlayer.PlaybackState`` as the app-wide play/pause/stop state that
``MediaController.state_changed`` carries. libobs is the only media engine, so the
state originates from the sidecar's ``obs_media_state`` integers and is mapped to
these three members for the UI. The value never crosses a process boundary — the
wire vocabularies (``core.scenes.media_control.MediaPlaybackState`` and
``core.remote_control.contracts.PlaybackState``) are separate and unchanged.
"""

from __future__ import annotations

from enum import Enum


class SolinPlaybackState(Enum):
    """The three playback states the UI binds to."""

    STOPPED = 0
    PLAYING = 1
    PAUSED = 2


# obs_media_state integers (see core.scenes.media_control.MediaPlaybackState /
# core.scenes.libobs_media_source.STATE_*) → the UI state. Anything not listed
# (NONE=0, STOPPED=5, ENDED=6, ERROR=7) resolves to STOPPED.
ENGINE_STATE_PLAYING = 1
# A dropped stream is reported as buffering rather than ended, so the transport
# can show it re-establishing instead of tearing the item down.
ENGINE_STATE_BUFFERING = 3
ENGINE_STATE_PAUSED = 4
ENGINE_STATE_ENDED = 6
ENGINE_STATE_TO_SOLIN: dict[int, SolinPlaybackState] = {
    ENGINE_STATE_PLAYING: SolinPlaybackState.PLAYING,
    2: SolinPlaybackState.PAUSED,  # OPENING
    ENGINE_STATE_BUFFERING: SolinPlaybackState.PAUSED,
    ENGINE_STATE_PAUSED: SolinPlaybackState.PAUSED,
}


__all__ = [
    "SolinPlaybackState",
    "ENGINE_STATE_PLAYING",
    "ENGINE_STATE_BUFFERING",
    "ENGINE_STATE_PAUSED",
    "ENGINE_STATE_ENDED",
    "ENGINE_STATE_TO_SOLIN",
]
