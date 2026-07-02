from types import SimpleNamespace

import solin.widgets.projection.bar as projection_bar


class _Timer:
    def __init__(self):
        self.started = 0
        self.stopped = 0

    def start(self):
        self.started += 1

    def stop(self):
        self.stopped += 1


class _Control:
    def __init__(self):
        self.enabled = True
        self.history = []

    def setEnabled(self, enabled):
        self.enabled = enabled
        self.history.append(enabled)

    def isEnabled(self):
        return self.enabled

    def setToolTip(self, _text):
        return None

    def setVisible(self, _visible):
        return None


class _Protection:
    locked = False

    def allow_manual_projection_change(self, *, notify=True):
        return not self.locked


class _Signal:
    def __init__(self):
        self.emitted = 0

    def emit(self):
        self.emitted += 1


class _AudioOutput:
    def __init__(self):
        self.volumes = []

    def setVolume(self, volume):
        self.volumes.append(volume)


class _Media:
    def __init__(self):
        self.position = 0
        self.audio_output = _AudioOutput()
        self.deferred = []
        self.paused = 0
        self.seeks = []
        self.played = 0

    def set_local_switch_deferred(self, deferred):
        self.deferred.append(deferred)

    def pause(self):
        self.paused += 1

    def seek(self, position):
        self.seeks.append(position)

    def play(self):
        self.played += 1


def _make_bar(*, muted=False, volume=0.8):
    bar = projection_bar.ProjectionBar.__new__(projection_bar.ProjectionBar)
    bar._announce_state = "off"
    bar._announce_gate_ms = 3500
    bar._announce_timer = _Timer()
    bar._muted = muted
    bar._volume = volume
    bar.media = _Media()
    bar.play_btn = _Control()
    bar.seek_slider = _Control()
    bar.toggle_requested = _Signal()
    bar._playback_protection = _Protection()
    bar._playlist = []
    bar._playlist_index = 0
    bar.prev_btn = _Control()
    bar.next_btn = _Control()
    bar.ov_panel_btn = _Control()
    bar.ov_send_temp_btn = _Control()
    bar._is_from_saved_playlist = False
    return bar


def test_begin_announcement_mode_mutes_locks_controls_and_starts_gate_timer():
    bar = _make_bar(volume=0.65)

    bar.begin_announcement_mode()

    assert bar._announce_state == "gate"
    assert bar.media.deferred == [True]
    assert bar.media.audio_output.volumes == [0.0]
    assert bar.play_btn.enabled is False
    assert bar.seek_slider.enabled is False
    assert bar._announce_timer.started == 1


def test_announcement_gate_waits_for_media_time_before_pausing():
    bar = _make_bar()
    bar.begin_announcement_mode()
    bar.media.position = bar._announce_gate_ms - 1

    bar._on_announce_gate_expired()

    assert bar._announce_state == "gate"
    assert bar.media.paused == 0
    assert bar._announce_timer.stopped == 0
    assert bar.media.audio_output.volumes == [0.0]
    assert bar.play_btn.enabled is False
    assert bar.seek_slider.enabled is False


def test_announcement_gate_pauses_unmutes_and_reenables_only_play(monkeypatch):
    single_shots = []
    monkeypatch.setattr(
        projection_bar,
        "QTimer",
        SimpleNamespace(
            singleShot=lambda ms, callback: single_shots.append(ms) or callback()
        ),
    )
    bar = _make_bar(volume=0.55)
    bar.begin_announcement_mode()
    bar.media.position = bar._announce_gate_ms

    bar._on_announce_gate_expired()

    assert bar._announce_state == "ready"
    assert bar._announce_timer.stopped == 1
    assert bar.media.paused == 1
    assert bar.media.deferred == [True, False]
    assert single_shots == [80]
    assert bar.media.audio_output.volumes == [0.0, 0.55]
    assert bar.play_btn.enabled is True
    assert bar.seek_slider.enabled is False


def test_announcement_gate_keeps_volume_zero_when_user_had_muted(monkeypatch):
    monkeypatch.setattr(
        projection_bar,
        "QTimer",
        SimpleNamespace(singleShot=lambda _ms, callback: callback()),
    )
    bar = _make_bar(muted=True, volume=0.55)
    bar.begin_announcement_mode()
    bar.media.position = bar._announce_gate_ms

    bar._on_announce_gate_expired()

    assert bar.media.audio_output.volumes == [0.0, 0.0]


def test_play_button_in_ready_state_restarts_song_and_unlocks_slider():
    bar = _make_bar()
    bar._announce_state = "ready"
    bar.seek_slider.setEnabled(False)

    bar._on_play_btn_clicked()

    assert bar._announce_state == "off"
    assert bar.media.deferred == [False]
    assert bar.seek_slider.enabled is True
    assert bar.media.seeks == [0]
    assert bar.media.played == 1
    assert bar.toggle_requested.emitted == 0


def test_play_button_outside_announcement_mode_uses_regular_toggle_signal():
    bar = _make_bar()

    bar._on_play_btn_clicked()

    assert bar.toggle_requested.emitted == 1
    assert bar.media.seeks == []
    assert bar.media.played == 0


def test_cancel_announcement_mode_restores_volume_controls_and_deferred_switch():
    bar = _make_bar(volume=0.7)
    bar.begin_announcement_mode()

    bar._cancel_announcement_mode()

    assert bar._announce_state == "off"
    assert bar._announce_timer.stopped == 1
    assert bar.media.deferred == [True, False]
    assert bar.media.audio_output.volumes == [0.0, 0.7]
    assert bar.play_btn.enabled is True
    assert bar.seek_slider.enabled is True
