from __future__ import annotations


from ..core.integrations.automation.auto_key_actions import (
    EVENT_MEDIA_ENDED,
    EVENT_MEDIA_PAUSED,
    EVENT_MEDIA_RESUMED,
    EVENT_MEDIA_STARTED,
)
from ..core.media.playback_state import PlaybackState


class AutoKeyProjectionController:
    """Dispatches auto-key events from projection media state transitions."""

    def __init__(self, dispatcher, projection_bar) -> None:
        self._dispatcher = dispatcher
        self._projection_bar = projection_bar
        self._visual_active = False
        self._video_paused = False

    def set_visual_active(self, active: bool) -> None:
        if self._visual_active == active:
            return
        self._visual_active = active
        self._video_paused = False
        self._dispatcher.dispatch(EVENT_MEDIA_STARTED if active else EVENT_MEDIA_ENDED)

    def prepare_video_session(self) -> None:
        self._video_paused = False

    def on_media_state(self, state) -> None:
        if (
            not self._visual_active
            or not self._projection_bar.is_video_mode()
            or self._projection_bar.is_audio_mode()
        ):
            return
        if state == PlaybackState.PausedState:
            if not self._video_paused:
                self._video_paused = True
                self._dispatcher.dispatch(EVENT_MEDIA_PAUSED)
        elif state == PlaybackState.PlayingState:
            if self._video_paused:
                self._video_paused = False
                self._dispatcher.dispatch(EVENT_MEDIA_RESUMED)
