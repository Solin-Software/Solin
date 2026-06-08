"""Unit tests for IdleMediaSource.set_playing — the pause/resume + restart logic.

These exercise the *real* production method without a QApplication by
constructing the object via ``__new__`` (skipping the Qt __init__) and injecting
a fake player.  The QMediaPlayer enum is importable without a running app, so the
state comparisons in the method run exactly as in production.
"""

from PySide6.QtMultimedia import QMediaPlayer

from app.projection import idle_source
from app.projection.idle_source import IdleMediaSource

_PLAYING = QMediaPlayer.PlaybackState.PlayingState
_PAUSED = QMediaPlayer.PlaybackState.PausedState
_STOPPED = QMediaPlayer.PlaybackState.StoppedState


class _FakePlayer:
    def __init__(self, state):
        self._state = state
        self.positions = []
        self.play_calls = 0
        self.pause_calls = 0

    def playbackState(self):
        return self._state

    def setPosition(self, pos):
        self.positions.append(pos)

    def play(self):
        self.play_calls += 1

    def pause(self):
        self.pause_calls += 1


def _make_source(player, *, media_type="video"):
    src = IdleMediaSource.__new__(IdleMediaSource)  # bypass Qt construction
    src._type = media_type
    src._player = player
    return src


def test_default_restart_flag_is_enabled():
    assert idle_source.IDLE_VIDEO_RESTART_ON_RESUME is True


def test_resume_restarts_from_beginning_when_enabled(monkeypatch):
    monkeypatch.setattr(idle_source, "IDLE_VIDEO_RESTART_ON_RESUME", True)
    player = _FakePlayer(_PAUSED)
    _make_source(player).set_playing(True)
    assert player.positions == [0]      # rewound
    assert player.play_calls == 1


def test_resume_keeps_position_when_disabled(monkeypatch):
    monkeypatch.setattr(idle_source, "IDLE_VIDEO_RESTART_ON_RESUME", False)
    player = _FakePlayer(_PAUSED)
    _make_source(player).set_playing(True)
    assert player.positions == []       # not rewound
    assert player.play_calls == 1


def test_set_playing_true_is_noop_when_already_playing():
    player = _FakePlayer(_PLAYING)
    _make_source(player).set_playing(True)
    assert player.play_calls == 0
    assert player.positions == []


def test_set_playing_false_pauses_a_playing_video():
    player = _FakePlayer(_PLAYING)
    _make_source(player).set_playing(False)
    assert player.pause_calls == 1


def test_set_playing_false_is_noop_when_not_playing():
    player = _FakePlayer(_PAUSED)
    _make_source(player).set_playing(False)
    assert player.pause_calls == 0


def test_set_playing_is_noop_for_image_idle(monkeypatch):
    monkeypatch.setattr(idle_source, "IDLE_VIDEO_RESTART_ON_RESUME", True)
    player = _FakePlayer(_PAUSED)
    _make_source(player, media_type="image").set_playing(True)
    assert player.play_calls == 0
    assert player.positions == []
