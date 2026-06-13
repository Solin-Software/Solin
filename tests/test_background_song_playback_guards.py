from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import solin.core.jw.background_song_service as service_module
from solin.core.jw.background_song_service import BackgroundSongService
from solin.core.meetings.schedule import MIDWEEK, MeetingOccurrence, MeetingSlot


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
    statuses: list[str] = []
    timer = _TimerStub()
    service = SimpleNamespace(
        _fade_seconds=5,
        _stop_before_seconds=10,
        _prefs=object(),
        _active_occurrence=None,
        _suppressed_slot_id="",
        _desired_playing=True,
        _manual_session=True,
        _auto_stop_timer=timer,
        _current_pre_meeting_occurrence=lambda: occurrence,
        _set_status=statuses.append,
        tr=lambda text: text,
    )
    return service, timer, statuses


def test_new_track_is_allowed_during_final_fade_window(monkeypatch):
    occurrence = _occurrence(12)
    service, timer, statuses = _guard_service(occurrence)
    service._scheduled_fade_deadline = None
    monkeypatch.setattr(
        service_module,
        "load_meeting_schedule",
        lambda _prefs: SimpleNamespace(pre_meeting_occurrence=lambda _now: occurrence),
    )
    allowed = BackgroundSongService._can_start_new_track(service)

    assert allowed is True
    assert service._desired_playing is True
    assert service._scheduled_fade_deadline is not None
    assert timer.stopped == 1
    assert statuses == []


def test_new_track_rearms_stop_timer_before_fade_window(monkeypatch):
    occurrence = _occurrence(30)
    service, timer, statuses = _guard_service(occurrence)
    service._scheduled_fade_deadline = None
    monkeypatch.setattr(
        service_module,
        "load_meeting_schedule",
        lambda _prefs: SimpleNamespace(pre_meeting_occurrence=lambda _now: occurrence),
    )
    allowed = BackgroundSongService._can_start_new_track(service)

    assert allowed is True
    assert timer.started_with
    assert 14_000 <= timer.started_with[0] <= 15_000
    assert statuses == []


def test_manual_track_is_allowed_after_meeting_start(monkeypatch):
    occurrence = _occurrence(-30)
    service, timer, statuses = _guard_service(occurrence)
    service._active_occurrence = occurrence
    service._scheduled_fade_deadline = None
    monkeypatch.setattr(
        service_module,
        "load_meeting_schedule",
        lambda _prefs: SimpleNamespace(pre_meeting_occurrence=lambda _now: None),
    )
    allowed = BackgroundSongService._can_start_new_track(service)

    assert allowed is True
    assert service._desired_playing is True
    assert timer.stopped == 0
    assert statuses == []


def test_delayed_automatic_track_is_rejected_after_meeting_start(monkeypatch):
    occurrence = _occurrence(-30)
    service, timer, statuses = _guard_service(occurrence)
    service._manual_session = False
    service._active_occurrence = occurrence
    service._scheduled_fade_deadline = None
    monkeypatch.setattr(
        service_module,
        "load_meeting_schedule",
        lambda _prefs: SimpleNamespace(pre_meeting_occurrence=lambda _now: None),
    )

    allowed = BackgroundSongService._can_start_new_track(service)

    assert allowed is False
    assert service._desired_playing is False
    assert statuses == ["Stopped before the meeting."]


def test_scheduled_fade_uses_only_time_remaining_until_cutoff():
    faded: list[tuple[float, float, bool]] = []
    statuses: list[str] = []
    service = SimpleNamespace(
        _scheduled_fade_deadline=datetime.now().astimezone() + timedelta(seconds=2),
        _desired_playing=True,
        _manual_session=True,
        _fade_to=lambda target, seconds, *, stop_after: faded.append((target, seconds, stop_after)),
        _set_status=statuses.append,
        stop=lambda *, immediate=False: None,
        tr=lambda text: text,
    )

    BackgroundSongService._start_scheduled_fade(service)

    assert service._desired_playing is False
    assert service._manual_session is False
    assert len(faded) == 1
    assert faded[0][0] == 0.0
    assert 1.0 <= faded[0][1] <= 2.0
    assert faded[0][2] is True
    assert statuses == ["Stopping background song..."]


def test_auto_start_inside_fade_window_loads_song_and_schedules_short_fade(
    monkeypatch,
):
    occurrence = _occurrence(12)
    loaded: list[bool] = []
    timer = _TimerStub()
    schedule = SimpleNamespace(
        has_configured_slot=True,
        pre_meeting_occurrence=lambda _now: occurrence,
    )
    monkeypatch.setattr(service_module, "load_meeting_schedule", lambda _prefs: schedule)
    monkeypatch.setattr(service_module._ps, "prefs", lambda: object())
    service = SimpleNamespace(
        _enabled=True,
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
        tr=lambda text: text,
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
        tr=lambda text: text,
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
        tr=lambda text: text,
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
