
from solin.controllers.auto_key_projection_controller import AutoKeyProjectionController
from solin.core.integrations.automation.auto_key_actions import (
    EVENT_MEDIA_ENDED,
    EVENT_MEDIA_PAUSED,
    EVENT_MEDIA_RESUMED,
    EVENT_MEDIA_STARTED,
)
from solin.core.media.playback_state import PlaybackState


class _DispatcherStub:
    def __init__(self):
        self.events = []

    def dispatch(self, event):
        self.events.append(event)


class _ProjectionBarStub:
    def __init__(self, video=True, audio=False):
        self.video = video
        self.audio = audio

    def is_video_mode(self):
        return self.video

    def is_audio_mode(self):
        return self.audio


def test_visual_active_dispatches_start_and_end_once_per_edge():
    dispatcher = _DispatcherStub()
    controller = AutoKeyProjectionController(dispatcher, _ProjectionBarStub())

    controller.set_visual_active(True)
    controller.set_visual_active(True)
    controller.set_visual_active(False)
    controller.set_visual_active(False)

    assert dispatcher.events == [EVENT_MEDIA_STARTED, EVENT_MEDIA_ENDED]


def test_media_state_dispatches_pause_and_resume_for_active_visual_video():
    dispatcher = _DispatcherStub()
    controller = AutoKeyProjectionController(dispatcher, _ProjectionBarStub())
    controller.set_visual_active(True)

    controller.on_media_state(PlaybackState.PausedState)
    controller.on_media_state(PlaybackState.PausedState)
    controller.on_media_state(PlaybackState.PlayingState)
    controller.on_media_state(PlaybackState.PlayingState)

    assert dispatcher.events == [
        EVENT_MEDIA_STARTED,
        EVENT_MEDIA_PAUSED,
        EVENT_MEDIA_RESUMED,
    ]


def test_media_state_ignores_audio_or_inactive_projection():
    dispatcher = _DispatcherStub()
    controller = AutoKeyProjectionController(dispatcher, _ProjectionBarStub(audio=True))
    controller.set_visual_active(True)

    controller.on_media_state(PlaybackState.PausedState)

    assert dispatcher.events == [EVENT_MEDIA_STARTED]
