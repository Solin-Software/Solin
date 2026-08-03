"""Playback vocabulary, independent of any Qt media class.

Solin's media pipeline is libobs. These enums used to be ``QMediaPlayer``'s,
which meant every controller, widget and service that merely wanted to say
"playing" had to import a Qt *media player* — a class the app no longer uses to
play anything.

They are :class:`IntEnum` with **Qt's own numbering** on purpose: a value that
still arrives from a Qt object (a metadata probe, a saved setting) compares equal
to its counterpart here, so the two can coexist without a flag day.

Leaf module: enums only, no imports beyond the stdlib.
"""

from __future__ import annotations

from enum import IntEnum


class PlaybackState(IntEnum):
    """Whether media is stopped, playing or paused."""

    StoppedState = 0
    PlayingState = 1
    PausedState = 2


class MediaStatus(IntEnum):
    """How far a media item has got through loading/buffering.

    Solin only acts on a few of these — loaded, buffered, end-of-media and the
    invalid/no-media pair — but the full set is kept so a status can be logged or
    round-tripped without losing meaning.
    """

    NoMedia = 0
    LoadingMedia = 1
    LoadedMedia = 2
    StalledMedia = 3
    BufferingMedia = 4
    BufferedMedia = 5
    EndOfMedia = 6
    InvalidMedia = 7


#: Statuses where a source is ready to present frames.
READY_STATUSES = frozenset({MediaStatus.LoadedMedia, MediaStatus.BufferedMedia})

__all__ = ["PlaybackState", "MediaStatus", "READY_STATUSES"]
