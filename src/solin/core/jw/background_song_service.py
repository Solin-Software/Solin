"""Independent background-song playback service."""

from __future__ import annotations

import random
from datetime import datetime, timedelta
from typing import Any

from PySide6.QtCore import QObject, QTimer, Signal, Slot
from PySide6.QtMultimedia import QMediaPlayer

from solin.core.jw.background_song_settings import (
    DEFAULT_BACKGROUND_SONG_FADE_SECONDS,
    DEFAULT_BACKGROUND_SONG_STOP_BEFORE_SECONDS,
    DEFAULT_BACKGROUND_SONG_VOLUME,
    BackgroundSongSettingsStore,
    clamp_background_song_fade_seconds,
    clamp_background_song_stop_before_seconds,
    clamp_background_song_volume,
)
from solin.core.jw.background_song_status import (
    STATUS_CONFIGURE_MEETING,
    STATUS_DISABLED,
    STATUS_ENABLE_IN_SETTINGS,
    STATUS_LOAD_FAILED,
    STATUS_LOADING_AUDIO,
    STATUS_NO_AUDIO,
    STATUS_PLAYING,
    STATUS_READY,
    STATUS_SIGN_LANGUAGE_UNAVAILABLE,
    STATUS_STOPPED,
    STATUS_STOPPED_BEFORE_MEETING,
    STATUS_STOPPED_FOR_MEETING,
    STATUS_STOPPING,
    STATUS_WAITING_FOR_MEETING,
)
from solin.core.jw.language_context import jw_media_language_context
from solin.core.jw.songs import JWSongsStore
from solin.core.media.playback import MediaController
from solin.core.meetings.schedule import (
    MeetingOccurrence,
)
from solin.core.meetings.schedule_settings import MeetingScheduleSettingsStore

_SCHEDULE_POLL_MS = 30_000
_FADE_TICK_MS = 50


def _display_title(item: dict[str, Any]) -> str:
    try:
        number = int(item.get("number") or 0)
    except (TypeError, ValueError):
        number = 0
    title = str(item.get("title") or "").strip()
    return f"{number}. {title}" if number else title


def _scheduled_stop_delays_ms(
    occurrence: MeetingOccurrence,
    now: datetime,
    *,
    fade_seconds: int,
    stop_before_seconds: int,
) -> tuple[int, int]:
    """Return delays until fade start and full stop for one occurrence."""

    until_meeting_ms = occurrence.milliseconds_until_start(now)
    stop_delay_ms = max(0, until_meeting_ms - stop_before_seconds * 1000)
    fade_start_delay_ms = max(0, stop_delay_ms - fade_seconds * 1000)
    return fade_start_delay_ms, stop_delay_ms


class BackgroundSongService(QObject):
    """Plays JW audio songs independently from projection playback."""

    enabled_changed = Signal(bool)
    playback_changed = Signal(bool)
    current_song_changed = Signal(str)
    status_changed = Signal(str)
    volume_changed = Signal(int)
    fade_seconds_changed = Signal(int)
    stop_before_seconds_changed = Signal(int)
    availability_changed = Signal(bool)

    def __init__(
        self,
        lang_manager: object,
        settings: BackgroundSongSettingsStore,
        schedule_settings: MeetingScheduleSettingsStore,
        songs_store: JWSongsStore,
        media_controller: MediaController,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._lang = lang_manager
        self._settings = settings
        self._schedule_settings = schedule_settings
        self._store = songs_store

        self._media = media_controller

        self._enabled = False
        self._volume_percent = DEFAULT_BACKGROUND_SONG_VOLUME
        self._fade_seconds = DEFAULT_BACKGROUND_SONG_FADE_SECONDS
        self._stop_before_seconds = DEFAULT_BACKGROUND_SONG_STOP_BEFORE_SECONDS
        self._songs: list[dict[str, Any]] = []
        self._queue: list[dict[str, Any]] = []
        self._active_key = ""
        self._loading = False
        self._desired_playing = False
        self._manual_session = False
        self._current_title = ""
        self._status_text = ""
        self._active_occurrence: MeetingOccurrence | None = None
        self._scheduled_fade_deadline: datetime | None = None
        self._suppressed_slot_id = ""
        self._started = False

        self._schedule_timer = QTimer(self)
        self._schedule_timer.setInterval(_SCHEDULE_POLL_MS)
        self._schedule_timer.timeout.connect(self.evaluate_auto_playback)

        self._auto_stop_timer = QTimer(self)
        self._auto_stop_timer.setSingleShot(True)
        self._auto_stop_timer.timeout.connect(self._begin_scheduled_stop)

        self._retry_timer = QTimer(self)
        self._retry_timer.setSingleShot(True)
        self._retry_timer.setInterval(400)
        self._retry_timer.timeout.connect(self._retry_after_error)

        self._fade_timer = QTimer(self)
        self._fade_timer.setInterval(_FADE_TICK_MS)
        self._fade_timer.timeout.connect(self._on_fade_tick)
        self._fade_target = 0.0
        self._fade_start = 0.0
        self._fade_elapsed_ms = 0
        self._fade_duration_ms = 0
        self._stop_after_fade = False

        self._media.media_ended.connect(self._on_media_ended)
        self._media.state_changed.connect(self._on_playback_state)
        self._media.error_occurred.connect(self._on_player_error)
        self._store.songs_ready.connect(self._on_songs_ready)
        self._store.songs_failed.connect(self._on_songs_failed)
        self._store.loading_changed.connect(self._on_loading_changed)

        jw_lang_service = getattr(self._lang, "jw_lang_service", None)
        if jw_lang_service is not None:
            jw_lang_service.media_language_changed.connect(self._on_language_changed)
        lang_changed = getattr(self._lang, "language_changed", None)
        if lang_changed is not None:
            lang_changed.connect(self._on_language_changed)

        self.reload_settings()

    @property
    def is_enabled(self) -> bool:
        return self._enabled

    @property
    def is_playing(self) -> bool:
        return self._media.is_playing

    @property
    def current_title(self) -> str:
        return self._current_title

    @property
    def status_text(self) -> str:
        return self._status_text

    @property
    def volume_percent(self) -> int:
        return self._volume_percent

    @property
    def fade_seconds(self) -> int:
        return self._fade_seconds

    @property
    def stop_before_seconds(self) -> int:
        return self._stop_before_seconds

    @property
    def has_songs(self) -> bool:
        return bool(self._songs)

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._schedule_timer.start()
        self.evaluate_auto_playback()

    def shutdown(self) -> None:
        self._schedule_timer.stop()
        self._auto_stop_timer.stop()
        self._retry_timer.stop()
        self._fade_timer.stop()
        self._desired_playing = False
        self._manual_session = False
        self._scheduled_fade_deadline = None
        self._media.stop()

    @Slot()
    def reload_settings(self) -> None:
        enabled = self._settings.is_enabled()
        volume = self._settings.volume_percent()
        fade_seconds = self._settings.fade_seconds()
        stop_before_seconds = self._settings.stop_before_seconds()

        enabled_changed = enabled != self._enabled
        self._enabled = bool(enabled)
        self._set_volume_percent(volume, persist=False)
        self._set_fade_seconds(fade_seconds, persist=False)
        self._set_stop_before_seconds(stop_before_seconds, persist=False)
        if enabled_changed:
            self.enabled_changed.emit(self._enabled)
        self.availability_changed.emit(self._enabled)

        if not self._enabled:
            self.stop(immediate=True)
            self._set_status(STATUS_DISABLED)
            return

        self._ensure_songs_loaded()
        if self._started:
            self.evaluate_auto_playback()

    def set_enabled(self, enabled: bool) -> None:
        if self._enabled == bool(enabled):
            return
        self._settings.set_enabled(enabled)
        self.reload_settings()

    def set_volume_percent(self, value: int) -> None:
        self._set_volume_percent(value, persist=True)

    def set_fade_seconds(self, value: int) -> None:
        self._set_fade_seconds(value, persist=True)
        self.evaluate_auto_playback()

    def set_stop_before_seconds(self, value: int) -> None:
        self._set_stop_before_seconds(value, persist=True)
        self.evaluate_auto_playback()

    @Slot()
    def toggle_playback(self) -> None:
        if self._desired_playing or self.is_playing:
            self.stop(manual=True)
            return
        self.start_manual()

    @Slot()
    def start_manual(self) -> None:
        if not self._enabled:
            self._set_status(STATUS_ENABLE_IN_SETTINGS)
            return
        self._suppressed_slot_id = ""
        self._desired_playing = True
        self._manual_session = True
        if not self._can_start_new_track():
            return
        self._ensure_songs_loaded()
        if self._songs:
            self._play_next()

    @Slot()
    def skip(self) -> None:
        if not self._enabled:
            return
        self._manual_session = True
        self._desired_playing = True
        self._ensure_songs_loaded()
        if self._songs:
            self._play_next()

    def stop(self, *, manual: bool = False, immediate: bool = False) -> None:
        if manual:
            occurrence = self._current_pre_meeting_occurrence()
            if occurrence is not None:
                self._suppressed_slot_id = occurrence.slot_id
        self._desired_playing = False
        self._manual_session = False
        self._scheduled_fade_deadline = None
        self._auto_stop_timer.stop()
        self._retry_timer.stop()
        if immediate or self._fade_seconds <= 0 or not self.is_playing:
            self._finish_stop()
            return
        self._fade_to(0.0, self._fade_seconds, stop_after=True)
        self._set_status(STATUS_STOPPING)

    @Slot()
    def evaluate_auto_playback(self) -> None:
        if not self._enabled:
            return

        schedule = self._schedule_settings.load()
        if not schedule.has_configured_slot:
            self._auto_stop_timer.stop()
            if not self.is_playing:
                self._set_status(STATUS_CONFIGURE_MEETING)
            return

        occurrence = schedule.pre_meeting_occurrence(datetime.now().astimezone())
        self._active_occurrence = occurrence
        if occurrence is None:
            self._scheduled_fade_deadline = None
            self._auto_stop_timer.stop()
            if not self.is_playing:
                self._set_status(STATUS_WAITING_FOR_MEETING)
            return

        if self._suppressed_slot_id == occurrence.slot_id:
            self._scheduled_fade_deadline = None
            self._auto_stop_timer.stop()
            if not self.is_playing:
                self._set_status(STATUS_STOPPED_FOR_MEETING)
            return

        fade_start_delay_ms, stop_delay_ms = _scheduled_stop_delays_ms(
            occurrence,
            datetime.now().astimezone(),
            fade_seconds=self._fade_seconds,
            stop_before_seconds=self._stop_before_seconds,
        )
        if stop_delay_ms <= 0:
            self._scheduled_fade_deadline = None
            self._auto_stop_timer.stop()
            self._suppressed_slot_id = occurrence.slot_id
            if self.is_playing or self._desired_playing:
                self.stop(immediate=True)
            self._set_status(STATUS_STOPPED_BEFORE_MEETING)
            return

        if fade_start_delay_ms <= 0:
            self._auto_stop_timer.stop()
            self._scheduled_fade_deadline = occurrence.starts_at - timedelta(
                seconds=self._stop_before_seconds
            )
            if self.is_playing:
                self._start_scheduled_fade()
                return
            if not self._desired_playing:
                self._manual_session = False
                self._desired_playing = True
                self._ensure_songs_loaded()
            if self._songs and self._desired_playing:
                self._play_next()
            return

        self._scheduled_fade_deadline = None
        self._auto_stop_timer.start(fade_start_delay_ms)
        if self.is_playing or self._desired_playing:
            return

        self._manual_session = False
        self._desired_playing = True
        self._ensure_songs_loaded()
        if self._songs:
            self._play_next()

    def _ensure_songs_loaded(self, *, force: bool = False) -> None:
        context = jw_media_language_context(self._lang)
        if context.is_sign_language:
            self._songs = []
            self._queue = []
            self._active_key = ""
            self.stop(immediate=True)
            self._set_status(STATUS_SIGN_LANGUAGE_UNAVAILABLE)
            return

        request = self._store.request_for(
            api_code=context.api_code,
            fallback_code=context.fallback_code,
            is_sign_language=False,
            audio_mode=True,
        )
        self._active_key = request.key
        self._store.ensure_loaded(request, force=force)
        snapshot = self._store.snapshot(request.key)
        self._loading = snapshot.is_loading
        if snapshot.items:
            self._set_songs(snapshot.items)
        elif snapshot.error:
            self._set_status(snapshot.error)
        elif snapshot.is_loading:
            self._set_status(STATUS_LOADING_AUDIO)

    @Slot(str)
    def _on_language_changed(self, *_args) -> None:
        self._songs = []
        self._queue = []
        self._active_key = ""
        if self._enabled:
            self._ensure_songs_loaded(force=True)
            self.evaluate_auto_playback()

    @Slot(str, list, str, float, bool)
    def _on_songs_ready(
        self,
        key: str,
        items: list,
        _pub_name: str,
        _fetched_at: float,
        _from_cache: bool,
    ) -> None:
        if key != self._active_key:
            return
        self._loading = False
        self._set_songs(items)
        # ``ensure_loaded`` schedules a ``songs_ready`` even when the list is
        # already cached, so a manual start/skip that already issued
        # ``_play_next`` synchronously would otherwise be restarted here. A
        # second ``_play_next`` reshuffles the queue and fires a fresh
        # ``play_url`` racing the first, which desyncs the popup title from the
        # audio. Only auto-start from here when no track is in flight yet — the
        # genuine "waiting for the list to load" case (``_current_title`` is
        # still empty because nothing has been handed to the player).
        track_in_flight = bool(self._current_title)
        if self._desired_playing and not self.is_playing and not track_in_flight:
            self._play_next()
        elif not self.is_playing and not track_in_flight:
            self._set_status(STATUS_READY)

    @Slot(str, str)
    def _on_songs_failed(self, key: str, error: str) -> None:
        if key != self._active_key:
            return
        self._loading = False
        self._set_status(error or STATUS_LOAD_FAILED)

    @Slot(str, bool)
    def _on_loading_changed(self, key: str, loading: bool) -> None:
        if key != self._active_key:
            return
        self._loading = bool(loading)
        if loading:
            self._set_status(STATUS_LOADING_AUDIO)

    def _set_songs(self, items: list) -> None:
        songs = [
            dict(item)
            for item in items or []
            if isinstance(item, dict) and str(item.get("url") or "").strip()
        ]
        self._songs = sorted(songs, key=lambda item: int(item.get("number") or 0))
        self._queue = []
        self.availability_changed.emit(bool(self._songs))

    def _play_next(self) -> None:
        if not self._enabled or not self._desired_playing:
            return
        if not self._can_start_new_track():
            return
        self._retry_timer.stop()
        if not self._songs:
            self._set_status(
                STATUS_NO_AUDIO
                if not self._loading
                else STATUS_LOADING_AUDIO
            )
            return
        self._fade_timer.stop()
        self._media.audio_output.setVolume(self._volume_percent / 100.0)
        if not self._queue:
            self._queue = list(self._songs)
            random.shuffle(self._queue)
        item = self._queue.pop(0)
        url = str(item.get("url") or "")
        self._current_title = _display_title(item)
        self.current_song_changed.emit(self._current_title)
        self._media.stop()
        self._media.audio_output.setVolume(self._volume_percent / 100.0)
        self._media.play_url(url, download_persist=False)
        if self._scheduled_fade_deadline is not None:
            self._start_scheduled_fade()
        else:
            self._set_status(STATUS_PLAYING)

    def _can_start_new_track(self) -> bool:
        now = datetime.now().astimezone()
        occurrence = self._schedule_settings.load().pre_meeting_occurrence(now)
        if occurrence is None:
            active = self._active_occurrence
            if not self._manual_session and active is not None and now >= active.starts_at:
                self._suppressed_slot_id = active.slot_id
                self._desired_playing = False
                self._set_status(STATUS_STOPPED_BEFORE_MEETING)
                return False
            return True

        fade_start_delay_ms, stop_delay_ms = _scheduled_stop_delays_ms(
            occurrence,
            now,
            fade_seconds=self._fade_seconds,
            stop_before_seconds=self._stop_before_seconds,
        )
        self._active_occurrence = occurrence

        if fade_start_delay_ms > 0:
            self._scheduled_fade_deadline = None
            self._auto_stop_timer.start(fade_start_delay_ms)
            return True

        if stop_delay_ms <= 0:
            self._scheduled_fade_deadline = None
            self._auto_stop_timer.stop()
            self._suppressed_slot_id = occurrence.slot_id
            self._desired_playing = False
            self._manual_session = False
            self._set_status(STATUS_STOPPED_BEFORE_MEETING)
            return False

        self._auto_stop_timer.stop()
        self._scheduled_fade_deadline = occurrence.starts_at - timedelta(
            seconds=self._stop_before_seconds
        )
        return True

    def _start_scheduled_fade(self) -> None:
        deadline = self._scheduled_fade_deadline
        if deadline is None:
            return

        remaining_seconds = (deadline - datetime.now().astimezone()).total_seconds()
        if remaining_seconds <= 0:
            self.stop(immediate=True)
            self._set_status(STATUS_STOPPED_BEFORE_MEETING)
            return

        self._desired_playing = False
        self._manual_session = False
        self._fade_to(0.0, remaining_seconds, stop_after=True)
        self._set_status(STATUS_STOPPING)

    @Slot()
    def _on_media_ended(self) -> None:
        if self._desired_playing:
            self._play_next()

    @Slot(QMediaPlayer.PlaybackState)
    def _on_playback_state(self, state: QMediaPlayer.PlaybackState) -> None:
        if state == QMediaPlayer.PlaybackState.PlayingState:
            self._retry_timer.stop()
        self.playback_changed.emit(state == QMediaPlayer.PlaybackState.PlayingState)

    def _on_player_error(self, error_string: str) -> None:
        if error_string:
            self._set_status(error_string)
        if self._enabled and self._desired_playing:
            self._retry_timer.start()

    def _retry_after_error(self) -> None:
        if not self._enabled or not self._desired_playing:
            return
        self._play_next()

    def _begin_scheduled_stop(self) -> None:
        self._suppressed_slot_id = (
            self._active_occurrence.slot_id if self._active_occurrence else ""
        )
        self.stop()

    def _current_pre_meeting_occurrence(self) -> MeetingOccurrence | None:
        return self._schedule_settings.load().pre_meeting_occurrence(
            datetime.now().astimezone()
        )

    def _set_volume_percent(self, value: int, *, persist: bool) -> None:
        value = clamp_background_song_volume(value)
        if persist:
            self._settings.set_volume_percent(value)
        changed = value != self._volume_percent
        self._volume_percent = value
        if not self._fade_timer.isActive():
            self._media.audio_output.setVolume(value / 100.0)
        if changed:
            self.volume_changed.emit(value)

    def _set_fade_seconds(self, value: int, *, persist: bool) -> None:
        value = clamp_background_song_fade_seconds(value)
        if persist:
            self._settings.set_fade_seconds(value)
        if value != self._fade_seconds:
            self._fade_seconds = value
            self.fade_seconds_changed.emit(value)

    def _set_stop_before_seconds(self, value: int, *, persist: bool) -> None:
        value = clamp_background_song_stop_before_seconds(value)
        if persist:
            self._settings.set_stop_before_seconds(value)
        if value != self._stop_before_seconds:
            self._stop_before_seconds = value
            self.stop_before_seconds_changed.emit(value)

    def _fade_to(self, target: float, seconds: float, *, stop_after: bool) -> None:
        self._fade_timer.stop()
        self._fade_target = max(0.0, min(1.0, target))
        self._fade_start = self._media.audio_output.volume()
        self._fade_elapsed_ms = 0
        self._fade_duration_ms = max(1, int(seconds * 1000))
        self._stop_after_fade = stop_after
        self._fade_timer.start()

    def _on_fade_tick(self) -> None:
        self._fade_elapsed_ms += _FADE_TICK_MS
        progress = min(1.0, self._fade_elapsed_ms / self._fade_duration_ms)
        value = self._fade_start + (self._fade_target - self._fade_start) * progress
        self._media.audio_output.setVolume(max(0.0, min(1.0, value)))
        if progress >= 1.0:
            self._fade_timer.stop()
            if self._stop_after_fade:
                self._finish_stop()

    def _finish_stop(self) -> None:
        self._fade_timer.stop()
        self._scheduled_fade_deadline = None
        self._media.stop()
        self._media.audio_output.setVolume(self._volume_percent / 100.0)
        self._current_title = ""
        self.current_song_changed.emit("")
        self.playback_changed.emit(False)
        self._set_status(STATUS_STOPPED)

    def _set_status(self, text: str) -> None:
        if text == self._status_text:
            return
        self._status_text = text
        self.status_changed.emit(text)


__all__ = [
    "DEFAULT_BACKGROUND_SONG_FADE_SECONDS",
    "DEFAULT_BACKGROUND_SONG_STOP_BEFORE_SECONDS",
    "DEFAULT_BACKGROUND_SONG_VOLUME",
    "BackgroundSongService",
]
