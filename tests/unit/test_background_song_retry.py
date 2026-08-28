from __future__ import annotations

from types import SimpleNamespace

from solin.core.jw.background_song_service import BackgroundSongService


class _TimerStub:
    def __init__(self) -> None:
        self.started = 0
        self.stopped = 0

    def start(self) -> None:
        self.started += 1

    def stop(self) -> None:
        self.stopped += 1


def test_player_error_schedules_retry_only_while_playback_is_desired():
    timer = _TimerStub()
    service = SimpleNamespace(
        _enabled=True,
        _desired_playing=True,
        _retry_timer=timer,
        _set_status=lambda _code, **_kwargs: None,
    )

    BackgroundSongService._on_player_error(service, "network error")
    assert timer.started == 1

    service._desired_playing = False
    BackgroundSongService._on_player_error(service, "network error")
    assert timer.started == 1


def test_delayed_retry_rechecks_current_playback_intent():
    played: list[bool] = []
    service = SimpleNamespace(
        _enabled=True,
        _desired_playing=False,
        _play_next=lambda: played.append(True),
    )

    BackgroundSongService._retry_after_error(service)
    assert played == []

    service._desired_playing = True
    BackgroundSongService._retry_after_error(service)
    assert played == [True]


def test_stop_cancels_pending_error_retry():
    retry_timer = _TimerStub()
    auto_stop_timer = _TimerStub()
    finished: list[bool] = []
    service = SimpleNamespace(
        _desired_playing=True,
        _auto_stop_timer=auto_stop_timer,
        _retry_timer=retry_timer,
        _fade_seconds=5,
        is_playing=True,
        _finish_stop=lambda: finished.append(True),
    )

    BackgroundSongService.stop(service, immediate=True)

    assert service._desired_playing is False
    assert auto_stop_timer.stopped == 1
    assert retry_timer.stopped == 1
    assert finished == [True]
