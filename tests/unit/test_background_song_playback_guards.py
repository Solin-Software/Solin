from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

import solin.core.jw.background_song_service as service_module
from solin.core.jw.background_song_service import BackgroundSongService
from solin.core.jw.background_song_status import (
    BackgroundSongStatus,
    BackgroundSongStatusCode,
)
from solin.core.meetings.schedule import MIDWEEK, MeetingOccurrence, MeetingSlot
from solin.ui.background_song_status import translate_background_song_status


class _TimerStub:
    def __init__(self) -> None:
        self.started_with: list[int] = []
        self.stopped = 0

    def start(self, milliseconds: int) -> None:
        self.started_with.append(milliseconds)

    def stop(self) -> None:
        self.stopped += 1


def _occurrence(seconds_until_start: int) -> MeetingOccurrence:
    now = datetime.now().astimezone()
    slot = MeetingSlot(MIDWEEK, now.weekday(), now.hour * 60 + now.minute)
    return MeetingOccurrence(slot=slot, starts_at=now + timedelta(seconds=seconds_until_start))


def _guard_service(occurrence: MeetingOccurrence):
    statuses: list[BackgroundSongStatusCode] = []
    timer = _TimerStub()
    service = SimpleNamespace(
        _fade_seconds=5,
        _stop_before_seconds=10,
        _schedule_source=SimpleNamespace(
            load=lambda: SimpleNamespace(pre_meeting_occurrence=lambda _now: occurrence)
        ),
        _active_occurrence=None,
        _suppressed_slot_id="",
        _desired_playing=True,
        _manual_session=True,
        _auto_stop_timer=timer,
        _current_pre_meeting_occurrence=lambda: occurrence,
        _set_status=statuses.append,
    )
    return service, timer, statuses


def test_new_track_is_allowed_during_final_fade_window():
    occurrence = _occurrence(12)
    service, timer, statuses = _guard_service(occurrence)
    service._scheduled_fade_deadline = None

    allowed = BackgroundSongService._can_start_new_track(service)

    assert allowed is True
    assert service._desired_playing is True
    assert service._scheduled_fade_deadline is not None
    assert timer.stopped == 1
    assert statuses == []


def test_new_track_rearms_stop_timer_before_fade_window():
    occurrence = _occurrence(30)
    service, timer, statuses = _guard_service(occurrence)
    service._scheduled_fade_deadline = None

    allowed = BackgroundSongService._can_start_new_track(service)

    assert allowed is True
    assert timer.started_with
    assert 14_000 <= timer.started_with[0] <= 15_000
    assert statuses == []


def test_manual_track_is_allowed_after_meeting_start():
    occurrence = _occurrence(-30)
    service, timer, statuses = _guard_service(occurrence)
    service._active_occurrence = occurrence
    service._scheduled_fade_deadline = None
    service._schedule_source = SimpleNamespace(
        load=lambda: SimpleNamespace(pre_meeting_occurrence=lambda _now: None)
    )

    allowed = BackgroundSongService._can_start_new_track(service)

    assert allowed is True
    assert service._desired_playing is True
    assert timer.stopped == 0
    assert statuses == []


def test_delayed_automatic_track_is_rejected_after_meeting_start():
    occurrence = _occurrence(-30)
    service, timer, statuses = _guard_service(occurrence)
    service._manual_session = False
    service._active_occurrence = occurrence
    service._scheduled_fade_deadline = None
    service._schedule_source = SimpleNamespace(
        load=lambda: SimpleNamespace(pre_meeting_occurrence=lambda _now: None)
    )

    allowed = BackgroundSongService._can_start_new_track(service)

    assert allowed is False
    assert service._desired_playing is False
    assert statuses == [BackgroundSongStatusCode.STOPPED_BEFORE_MEETING]


def test_scheduled_fade_uses_only_time_remaining_until_cutoff():
    faded: list[tuple[float, float, bool]] = []
    statuses: list[BackgroundSongStatusCode] = []
    service = SimpleNamespace(
        _scheduled_fade_deadline=datetime.now().astimezone() + timedelta(seconds=2),
        _desired_playing=True,
        _manual_session=True,
        _fade_to=lambda target, seconds, *, stop_after: faded.append((target, seconds, stop_after)),
        _set_status=statuses.append,
        stop=lambda *, immediate=False: None,
        _notify_scheduled_stop=lambda: None,
    )

    BackgroundSongService._start_scheduled_fade(service)

    assert service._desired_playing is False
    assert service._manual_session is False
    assert len(faded) == 1
    assert faded[0][0] == 0.0
    assert 1.0 <= faded[0][1] <= 2.0
    assert faded[0][2] is True
    assert statuses == [BackgroundSongStatusCode.STOPPING]


def test_scheduled_stop_event_is_emitted_once_per_occurrence():
    from types import MethodType

    events = []
    service = SimpleNamespace(
        _active_occurrence=_occurrence(30), _notified_stop_slot_id="",
        is_playing=True, _desired_playing=True,
        scheduled_stop_started=SimpleNamespace(emit=events.append),
    )
    service._notify_scheduled_stop = MethodType(BackgroundSongService._notify_scheduled_stop, service)
    service._notify_scheduled_stop()
    service._notify_scheduled_stop()
    assert events == [service._active_occurrence.slot_id]
    service._active_occurrence = _occurrence(24 * 3600)
    service._notify_scheduled_stop()
    assert len(events) == 2


def test_zero_fade_scheduled_stop_emits_before_stopping_audio():
    events = []
    service = SimpleNamespace(
        _enabled=True, _active_occurrence=_occurrence(30),
        _notify_scheduled_stop=lambda: events.append("scheduled"),
        stop=lambda: events.append("stop"),
    )
    BackgroundSongService._begin_scheduled_stop(service)
    assert events == ["scheduled", "stop"]


@pytest.mark.parametrize("playing,fade_seconds", [(True, 5), (True, 0), (False, 5)])
def test_manual_stop_preserves_fade_without_publishing_a_scheduled_stop_event(
    playing, fade_seconds,
):
    events = []
    fades = []
    timer = _TimerStub()
    service = SimpleNamespace(
        _current_pre_meeting_occurrence=lambda: _occurrence(30),
        _auto_stop_timer=timer, _retry_timer=timer, _fade_seconds=fade_seconds,
        is_playing=playing,
        _fade_to=lambda target, seconds, *, stop_after: fades.append((target, seconds, stop_after)),
        _set_status=lambda _code: None,
        _finish_stop=lambda: events.append("stop"),
        _notify_scheduled_stop=lambda: events.append("scheduled"),
    )
    BackgroundSongService.stop(service, manual=True)
    assert events == ([] if playing and fade_seconds else ["stop"])
    assert fades == ([(0.0, fade_seconds, True)] if playing and fade_seconds else [])
    assert not service._desired_playing
    assert timer.stopped == 2


def test_manual_stop_suppresses_automatic_restart_for_the_current_meeting():
    occurrence = _occurrence(30)
    service, timer, statuses = _guard_service(occurrence)
    service._enabled = True
    service._fade_seconds = 0
    service.is_playing = False
    service._retry_timer = _TimerStub()
    service._finish_stop = lambda: None
    # Keep the same schedule snapshot throughout this playback evaluation.
    schedule = service._schedule_source.load()
    schedule.has_configured_slot = True
    service._schedule_source.load = lambda: schedule

    BackgroundSongService.stop(service, manual=True)
    BackgroundSongService.evaluate_auto_playback(service)

    assert service._suppressed_slot_id == occurrence.slot_id
    assert not service._desired_playing
    assert not timer.started_with
    assert statuses == [BackgroundSongStatusCode.STOPPED_FOR_MEETING]


def test_auto_start_inside_fade_window_loads_song_and_schedules_short_fade():
    occurrence = _occurrence(12)
    loaded: list[bool] = []
    timer = _TimerStub()
    schedule = SimpleNamespace(
        has_configured_slot=True,
        pre_meeting_occurrence=lambda _now: occurrence,
    )
    service = SimpleNamespace(
        _enabled=True,
        _schedule_source=SimpleNamespace(load=lambda: schedule),
        _active_occurrence=None,
        _scheduled_fade_deadline=None,
        _suppressed_slot_id="",
        _fade_seconds=5,
        _stop_before_seconds=10,
        _auto_stop_timer=timer,
        is_playing=False,
        _desired_playing=False,
        _manual_session=True,
        _songs=[],
        _ensure_songs_loaded=lambda: loaded.append(True),
        _play_next=lambda: None,
        _start_scheduled_fade=lambda: None,
        stop=lambda *, immediate=False: None,
        _set_status=lambda _text: None,
    )

    BackgroundSongService.evaluate_auto_playback(service)

    assert loaded == [True]
    assert service._desired_playing is True
    assert service._manual_session is False
    assert service._scheduled_fade_deadline is not None


def test_sign_language_context_stops_existing_audio_immediately(monkeypatch):
    stopped: list[bool] = []
    service = SimpleNamespace(
        _lang=object(),
        _songs=[{"url": "https://example.test/song.mp3"}],
        _queue=[{"url": "https://example.test/song.mp3"}],
        _active_key="audio:en",
        stop=lambda *, immediate=False: stopped.append(immediate),
        _set_status=lambda _text: None,
    )
    monkeypatch.setattr(
        service_module,
        "jw_media_language_context",
        lambda _lang: SimpleNamespace(is_sign_language=True),
    )

    BackgroundSongService._ensure_songs_loaded(service)

    assert stopped == [True]
    assert service._songs == []
    assert service._queue == []
    assert service._active_key == ""


def _ready_service(*, current_title: str, is_playing: bool):
    plays: list[bool] = []
    statuses: list[str] = []
    service = SimpleNamespace(
        _active_key="E||regular|audio",
        _loading=True,
        _desired_playing=True,
        is_playing=is_playing,
        _current_title=current_title,
        _set_songs=lambda _items: None,
        _play_next=lambda: plays.append(True),
        _set_status=statuses.append,
    )
    return service, plays, statuses


def test_cached_songs_ready_does_not_restart_track_in_flight():
    # A manual start/skip already issued _play_next (current_title set) and the
    # player is still buffering (is_playing False). The redundant songs_ready
    # that ensure_loaded() schedules for cached lists must not start a second
    # track — that is what desynced the popup title from the audio.
    service, plays, statuses = _ready_service(current_title="127. Como quero ser", is_playing=False)

    BackgroundSongService._on_songs_ready(
        service, "E||regular|audio", [{"url": "u"}], "", 0.0, False
    )

    assert plays == []
    assert statuses == []


def test_songs_ready_starts_playback_when_idle_and_waiting():
    # Genuine "waiting for the list to load" case: nothing handed to the player
    # yet (current_title empty), playback desired — songs_ready must start it.
    service, plays, statuses = _ready_service(current_title="", is_playing=False)

    BackgroundSongService._on_songs_ready(
        service, "E||regular|audio", [{"url": "u"}], "", 0.0, False
    )

    assert plays == [True]


def test_late_song_list_cannot_resume_playback_after_a_manual_stop():
    service, plays, _statuses = _ready_service(current_title="", is_playing=False)
    service._current_pre_meeting_occurrence = lambda: None
    service._auto_stop_timer = _TimerStub()
    service._retry_timer = _TimerStub()
    service._fade_seconds = 0
    service._finish_stop = lambda: None

    BackgroundSongService.stop(service, manual=True)
    BackgroundSongService._on_songs_ready(
        service, "E||regular|audio", [{"url": "u"}], "", 0.0, False
    )

    assert plays == []
    assert not service._desired_playing


def test_background_song_status_translation_renders_at_ui_boundary():
    status = BackgroundSongStatus(BackgroundSongStatusCode.STOPPING)

    assert translate_background_song_status(status) == "Stopping background song..."


def test_background_song_error_detail_renders_at_ui_boundary():
    status = BackgroundSongStatus(
        BackgroundSongStatusCode.PLAYBACK_ERROR,
        "network error",
    )

    assert translate_background_song_status(status) == (
        "Background-song playback error:\nnetwork error"
    )


def test_background_song_load_error_detail_is_a_complete_ui_message():
    status = BackgroundSongStatus(
        BackgroundSongStatusCode.LOAD_FAILED,
        "network error",
    )

    assert translate_background_song_status(status) == (
        "Could not load audio songs.\nnetwork error"
    )
