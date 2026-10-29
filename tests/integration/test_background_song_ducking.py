from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QAbstractAnimation, QObject, Signal

from solin.core.jw.background_song_service import BackgroundSongService
from solin.core.media.playback_state import SolinPlaybackState
from solin.core.meetings.schedule import MIDWEEK, MeetingOccurrence, MeetingSlot
from solin.core.projection.application import ProjectionSession
from tests._qt import wait_until


class _Media(QObject):
    media_ended = Signal()
    state_changed = Signal(object)
    error_occurred = Signal(str)

    def __init__(self):
        super().__init__()
        self.volume = 0.0
        self.is_playing = False
        self.requests = []
        self.stops = 0
        self.volumes = []
        self.playing_volumes = []

    def set_volume(self, volume):
        self.volume = volume
        self.volumes.append(volume)
        if self.is_playing:
            self.playing_volumes.append(volume)

    def stop(self):
        self.stops += 1
        self.is_playing = False
        self.state_changed.emit(SolinPlaybackState.STOPPED)

    def start_playback(self, request):
        self.requests.append((request, self.volume))
        self.is_playing = True
        self.state_changed.emit(SolinPlaybackState.PLAYING)


class _Songs(QObject):
    songs_ready = Signal(str, list, str, float, bool)
    songs_failed = Signal(str, str)
    loading_changed = Signal(str, bool)


@pytest.fixture
def song_service(request):
    media = _Media()
    songs = _Songs()
    settings = SimpleNamespace(
        is_enabled=lambda: False,
        volume_percent=lambda: 40,
        fade_seconds=lambda: 5,
        stop_before_seconds=lambda: 10,
        set_volume_percent=lambda _value: None,
    )
    schedule = SimpleNamespace(has_configured_slot=False, pre_meeting_occurrence=lambda _now: None)
    service = BackgroundSongService(
        SimpleNamespace(), settings, SimpleNamespace(load=lambda: schedule), songs, media,
    )
    request.addfinalizer(service.deleteLater)
    request.addfinalizer(service.shutdown)
    service._enabled = True
    service._songs = [{"url": "https://example.test/song.mp3", "title": "Song"}]
    service._ensure_songs_loaded = lambda **_options: None
    service.start_manual()
    return service, media


def test_media_mute_fades_without_interrupting_background_playback(song_service):
    service, media = song_service
    requests, stops = len(media.requests), media.stops
    title = service.current_title

    service.set_media_muted(True)
    assert media.volume == pytest.approx(0.4)
    wait_until(lambda: media.volume == 0.0, description="background mute")
    assert service.is_playing and service.playback_requested
    assert service.current_title == title
    assert (len(media.requests), media.stops) == (requests, stops)

    service.set_media_muted(False)
    assert media.volume == 0.0
    wait_until(lambda: abs(media.volume - 0.4) < 1e-6, description="background volume restore")
    assert (len(media.requests), media.stops) == (requests, stops)


def _finish_media_fade(service):
    service._media_fade.setCurrentTime(service._media_fade.duration())


def test_reversing_a_fade_continues_from_the_current_output_gain(song_service):
    service, media = song_service
    service.set_media_muted(True)
    service._media_fade.setCurrentTime(service._media_fade.duration() // 2)
    halfway = media.volume
    assert 0.0 < halfway < 0.4

    before = len(media.volumes)
    service.set_media_muted(False)
    assert media.volume == pytest.approx(halfway)
    assert all(value == pytest.approx(halfway) for value in media.volumes[before:])
    _finish_media_fade(service)
    assert media.volume == pytest.approx(0.4)


def test_volume_changes_while_muted_are_used_when_media_exits(song_service):
    service, media = song_service
    service.set_media_muted(True)
    _finish_media_fade(service)

    service.set_volume_percent(65)
    assert service.volume_percent == 65
    assert media.volume == 0.0

    service.set_media_muted(False)
    _finish_media_fade(service)
    assert media.volume == pytest.approx(0.65)


@pytest.mark.parametrize("action", ["skip", "ended", "retry", "manual_start"])
def test_new_background_tracks_start_silent_during_foreground_media(song_service, action):
    service, media = song_service
    if action == "manual_start":
        service.stop(immediate=True)
    service.set_media_muted(True)
    _finish_media_fade(service)
    before = len(media.requests)

    if action == "skip":
        service.skip()
    elif action == "ended":
        media.media_ended.emit()
    elif action == "retry":
        media.error_occurred.emit("network error")
        wait_until(lambda: len(media.requests) > before, description="background retry")
    else:
        service.start_manual()

    assert len(media.requests) == before + 1
    assert media.requests[-1][1] == 0.0
    assert media.volume == 0.0
    assert service.is_playing


@pytest.mark.parametrize("scheduled", [False, True])
def test_media_exit_cannot_cancel_or_raise_volume_during_a_background_stop(song_service, scheduled):
    service, media = song_service
    service.set_media_muted(True)
    _finish_media_fade(service)
    if scheduled:
        now = datetime.now().astimezone()
        service._active_occurrence = MeetingOccurrence(
            slot=MeetingSlot(MIDWEEK, now.weekday(), 19 * 60),
            starts_at=now + timedelta(seconds=10.15),
        )
        service._scheduled_fade_deadline = now + timedelta(seconds=0.15)
        service._start_scheduled_fade()
    else:
        service._fade_seconds = 1
        service.stop(manual=True)

    service.set_media_muted(False)
    assert media.volume == 0.0
    assert not service.playback_requested
    wait_until(lambda: not service.is_playing, description="background stop")
    assert not service.current_title


def test_media_exit_after_cutoff_does_not_restart_background_song(song_service):
    service, media = song_service
    now = datetime.now().astimezone()
    occurrence = MeetingOccurrence(
        slot=MeetingSlot(MIDWEEK, now.weekday(), 19 * 60),
        starts_at=now + timedelta(seconds=5),
    )
    service._schedule_source.load = lambda: SimpleNamespace(
        has_configured_slot=True, pre_meeting_occurrence=lambda _now: occurrence,
    )
    service.set_media_muted(True)
    _finish_media_fade(service)
    before = len(media.requests)

    service.evaluate_auto_playback()
    service.set_media_muted(False)
    service.evaluate_auto_playback()

    assert not service.is_playing and not service.playback_requested
    assert len(media.requests) == before


@pytest.mark.parametrize("scheduled", [False, True])
@pytest.mark.parametrize("release_progress", [0, 0.5])
def test_background_stop_cancels_an_already_started_volume_release(
    song_service, scheduled, release_progress,
):
    service, media = song_service
    service.set_media_muted(True)
    _finish_media_fade(service)
    service.set_media_muted(False)
    service._media_fade.setCurrentTime(round(service._media_fade.duration() * release_progress))
    before = media.volume

    if scheduled:
        service._scheduled_fade_deadline = datetime.now().astimezone() + timedelta(seconds=0.15)
        service._start_scheduled_fade()
    else:
        service._fade_seconds = 1
        service.stop(manual=True)

    history_start = len(media.playing_volumes)
    wait_until(lambda: not service.is_playing, description="stop during media release")
    assert all(volume <= before for volume in media.playing_volumes[history_start:])
    assert not service.playback_requested


def test_media_events_after_shutdown_cannot_restart_volume_animation(song_service):
    service, media = song_service
    service.set_media_muted(True)
    service.shutdown()
    volume = media.volume

    service.set_media_muted(False)
    assert service._media_fade.state() == QAbstractAnimation.State.Stopped
    assert media.volume == volume
    assert not service.is_playing


@pytest.mark.parametrize("is_audio", [False, True])
def test_projection_session_mutes_until_audio_or_video_is_replaced(song_service, is_audio):
    from solin.main_window import MainWindow

    service, media = song_service
    session = ProjectionSession()
    shell = SimpleNamespace(projection_session=session, _background_song_service=service)
    unsubscribe = session.subscribe(lambda: MainWindow._sync_background_song_media_mute(shell))
    try:
        session.set_state({"type": "video", "is_audio": is_audio})
        _finish_media_fade(service)
        assert media.volume == 0.0

        # Transport changes do not end the projection's ownership of audio.
        for state in (SolinPlaybackState.PAUSED, SolinPlaybackState.STOPPED):
            media.state_changed.emit(state)
            session.update_state(title="Updated metadata")
            assert media.volume == 0.0
        session.set_state({"type": "video", "is_audio": not is_audio})
        assert media.volume == 0.0

        session.set_state({"type": "timer", "origin": "automatic_countdown"})
        _finish_media_fade(service)
        assert media.volume == pytest.approx(0.4)
        session.reset_state()
        assert media.volume == pytest.approx(0.4)
    finally:
        unsubscribe()


def test_delayed_stop_tick_uses_elapsed_time_instead_of_extending_the_stop(song_service):
    service, media = song_service
    service._fade_seconds = 1
    service.stop()
    # Simulate a delayed event-loop delivery past the fade's deadline.
    service._fade_clock = SimpleNamespace(elapsed=lambda: 1500)
    service._on_fade_tick()
    assert not media.is_playing
    assert not service.playback_requested


@pytest.mark.parametrize("fade_seconds", [0.0, 0.35])
def test_media_fade_duration_is_configurable_without_changing_the_meeting_fade(
    song_service, monkeypatch, request, fade_seconds,
):
    import solin.core.jw.background_song_service as service_module

    service, _media = song_service
    assert service._media_fade.duration() == 200
    monkeypatch.setattr(service_module, "BACKGROUND_SONG_MEDIA_FADE_SECONDS", fade_seconds)
    media = _Media()
    configured = BackgroundSongService(
        SimpleNamespace(), service._settings, service._schedule_source, _Songs(), media,
    )
    request.addfinalizer(configured.deleteLater)
    request.addfinalizer(configured.shutdown)
    assert configured._media_fade.duration() == round(fade_seconds * 1000)
    assert configured.fade_seconds == service.fade_seconds
    media.is_playing = True
    configured.set_media_muted(True)
    _finish_media_fade(configured)
    assert media.volume == 0.0
    configured.set_media_muted(False)
    _finish_media_fade(configured)
    assert media.volume == pytest.approx(0.4)
