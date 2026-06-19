import solin.widgets.projection.bar as projection_bar


class _Slider:
    def __init__(self):
        self.reconnect_states = []

    def setReconnectActive(self, active):
        self.reconnect_states.append(active)


def _bar_with_mode(mode):
    bar = projection_bar.ProjectionBar.__new__(projection_bar.ProjectionBar)
    bar._mode = mode
    bar.seek_slider = _Slider()
    return bar


def test_video_recovery_state_controls_seek_slider_feedback():
    bar = _bar_with_mode("video")

    bar._on_playback_recovery_changed(True)
    bar._on_playback_recovery_changed(False)

    assert bar.seek_slider.reconnect_states == [True, False]


def test_non_video_recovery_state_keeps_seek_slider_feedback_off():
    bar = _bar_with_mode("image")

    bar._on_playback_recovery_changed(True)

    assert bar.seek_slider.reconnect_states == [False]
