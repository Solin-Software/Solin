from __future__ import annotations

from PySide6.QtCore import QCoreApplication, QObject, Signal
from PySide6.QtMultimedia import QMediaPlayer

import solin.core.media.playback as playback_module
from solin.core.media.cache import MediaCacheManager
from solin.core.media.playback import MediaController
from solin.core.media.playback_request import (
    MediaPlaybackRequest,
    MediaTrim,
    PlaybackCachePolicy,
)


def _app():
    return QCoreApplication.instance() or QCoreApplication([])


class _MediaSettings:
    def __init__(self, auto_download: bool) -> None:
        self._auto_download = auto_download

    def auto_download_on_play(self) -> bool:
        return self._auto_download


class _Downloader(QObject):
    progress = Signal(int, int)
    finished = Signal(str)
    error = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.started: list[tuple[str, bool]] = []
        self.cancel_count = 0
        self.cleanup_count = 0

    def get_cached_path(self, _url: str):
        return None

    def start(self, url: str, persist: bool = True) -> None:
        self.started.append((url, persist))

    def cancel(self) -> None:
        self.cancel_count += 1

    def cleanup_temp(self) -> None:
        self.cleanup_count += 1


def _controller_with_downloader(tmp_path, *, auto_download: bool):
    _app()
    downloader = _Downloader()
    cache_manager = MediaCacheManager(
        tmp_path,
        downloader_factory=lambda _parent: _Downloader(),
    )
    controller = MediaController(
        _MediaSettings(auto_download),
        cache_manager,
        downloader_factory=lambda _parent: downloader,
    )
    played: list[str] = []
    controller._play_source = played.append
    return controller, downloader, played


def _start(controller, url: str, *, temporary: bool = False) -> None:
    policy = (
        PlaybackCachePolicy.TEMPORARY
        if temporary
        else PlaybackCachePolicy.PROFILE_DEFAULT
    )
    controller.start_playback(MediaPlaybackRequest(url, cache_policy=policy))


def test_playback_request_uses_auto_download_setting_by_default(tmp_path):
    controller, downloader, played = _controller_with_downloader(
        tmp_path,
        auto_download=True,
    )

    _start(controller, "https://cdn.example/song.mp3")

    assert played == ["https://cdn.example/song.mp3"]
    assert downloader.started == [("https://cdn.example/song.mp3", True)]
    assert controller.stream_persist is True
    controller.stop()


def test_playback_request_can_force_temporary_download(tmp_path):
    controller, downloader, played = _controller_with_downloader(
        tmp_path,
        auto_download=True,
    )

    _start(controller, "https://cdn.example/song.mp3", temporary=True)

    assert played == ["https://cdn.example/song.mp3"]
    assert downloader.started == [("https://cdn.example/song.mp3", False)]
    assert controller.stream_persist is False
    controller.stop()


def test_download_error_clears_buffer_and_reports_streaming_fallback(tmp_path):
    controller, downloader, _played = _controller_with_downloader(
        tmp_path,
        auto_download=True,
    )
    buffer_events: list[tuple[int, int]] = []
    failures: list[tuple[str, str, bool]] = []
    controller.buffer_progress.connect(
        lambda downloaded, total: buffer_events.append((downloaded, total))
    )
    controller.playback_download_failed.connect(
        lambda url, message, persist: failures.append((url, message, persist))
    )

    _start(controller, "https://cdn.example/song.mp3")
    downloader.progress.emit(50, 100)
    downloader.error.emit("[Errno 28] No space left on device")

    assert buffer_events[-2:] == [(50, 100), (0, 0)]
    assert failures == [
        (
            "https://cdn.example/song.mp3",
            "[Errno 28] No space left on device",
            True,
        )
    ]
    assert controller.current_url == "https://cdn.example/song.mp3"


def test_local_media_plays_without_cache_download_or_streaming_fallback(tmp_path):
    local_media = tmp_path / "local-video.mp4"
    local_media.write_bytes(b"local")
    controller, downloader, played = _controller_with_downloader(
        tmp_path,
        auto_download=True,
    )
    buffer_events: list[tuple[int, int]] = []
    failures: list[tuple[str, str, bool]] = []
    source_states: list[bool] = []
    controller.buffer_progress.connect(
        lambda downloaded, total: buffer_events.append((downloaded, total))
    )
    controller.playback_download_failed.connect(
        lambda url, message, persist: failures.append((url, message, persist))
    )
    controller.playback_source_changed.connect(source_states.append)

    _start(controller, str(local_media))
    downloader.error.emit("No connection adapters were found")

    assert played == [str(local_media)]
    assert downloader.started == []
    assert failures == []
    assert buffer_events == [(0, 0)]
    assert source_states == [True]
    assert controller.current_url == str(local_media)
    assert controller.local_path == str(local_media)
    assert controller.stream_persist is False
    controller.stop()


class _ReconnectTimer:
    def __init__(self) -> None:
        self.started = 0
        self.stopped = 0
        self.intervals: list[int] = []

    def setInterval(self, value: int) -> None:  # noqa: N802 - Qt-style test double
        self.intervals.append(value)

    def start(self) -> None:
        self.started += 1

    def stop(self) -> None:
        self.stopped += 1


class _ReconnectPlayer:
    def __init__(self) -> None:
        self.sources: list[str] = []
        self.positions: list[int] = []
        self.played = 0
        self.paused = 0
        self.stopped = 0

    def position(self) -> int:
        return 0

    def duration(self) -> int:
        return 120_000

    def playbackState(self):
        return QMediaPlayer.PlaybackState.StoppedState

    def mediaStatus(self):
        return QMediaPlayer.MediaStatus.LoadedMedia

    def stop(self) -> None:
        self.stopped += 1

    def setSource(self, url) -> None:  # noqa: N802 - Qt-style test double
        self.sources.append(url.toString())

    def setPosition(self, position: int) -> None:  # noqa: N802 - Qt-style test double
        self.positions.append(position)

    def play(self) -> None:
        self.played += 1

    def pause(self) -> None:
        self.paused += 1


class _NoMediaReconnectPlayer(_ReconnectPlayer):
    def duration(self) -> int:
        return 0

    def mediaStatus(self):
        return QMediaPlayer.MediaStatus.NoMedia


class _TrimPlayer(_ReconnectPlayer):
    def __init__(self, *, seekable: bool = True) -> None:
        super().__init__()
        self._position = 0
        self._seekable = seekable
        self._state = QMediaPlayer.PlaybackState.StoppedState
        self._source = playback_module.QUrl()

    def position(self) -> int:
        return self._position

    def setPosition(self, position: int) -> None:  # noqa: N802
        self._position = position
        self.positions.append(position)

    def setSource(self, source) -> None:  # noqa: N802
        self._source = source
        self.sources.append(source.toString())

    def source(self):
        return self._source

    def isSeekable(self) -> bool:  # noqa: N802
        return self._seekable

    def playbackState(self):  # noqa: N802
        return self._state

    def play(self) -> None:
        self.played += 1
        self._state = QMediaPlayer.PlaybackState.PlayingState

    def pause(self) -> None:
        self.paused += 1
        self._state = QMediaPlayer.PlaybackState.PausedState


def test_trimmed_request_confirms_start_before_exposing_relative_timeline(
    monkeypatch,
    tmp_path,
):
    controller, _downloader, _played = _controller_with_downloader(
        tmp_path,
        auto_download=False,
    )
    monkeypatch.setattr(
        playback_module.QTimer,
        "singleShot",
        lambda _delay, callback: callback(),
    )
    player = _TrimPlayer()
    controller.player = player
    controller._play_source = MediaController._play_source.__get__(controller)
    durations: list[int] = []
    positions: list[int] = []
    controller.duration_changed.connect(durations.append)
    controller.position_changed.connect(positions.append)
    source = tmp_path / "bounded.mp4"
    source.write_bytes(b"media")

    controller.start_playback(
        MediaPlaybackRequest(
            str(source),
            trim=MediaTrim(
                start_trim_ticks=10_000 * 10_000,
                end_trim_ticks=20_000 * 10_000,
                base_duration_ticks=120_000 * 10_000,
            ),
        )
    )

    assert player.positions == [10_000]
    assert durations == [90_000]
    assert positions == [0]
    assert controller.duration == 90_000
    assert controller.position == 0
    assert player.played >= 2


def test_trimmed_duration_keeps_source_metadata_separate_from_effective_timeline(
    tmp_path,
):
    controller, _downloader, _played = _controller_with_downloader(
        tmp_path,
        auto_download=False,
    )
    controller._request = MediaPlaybackRequest(
        "clip.mp4",
        trim=MediaTrim(
            start_trim_ticks=10_000 * 10_000,
            end_trim_ticks=20_000 * 10_000,
        ),
    )
    controller._playback_range = controller._request.trim.resolve(120_000)
    source_durations: list[int] = []
    effective_durations: list[int] = []
    controller.source_duration_changed.connect(source_durations.append)
    controller.duration_changed.connect(effective_durations.append)

    controller._on_duration(120_000)

    assert source_durations == [120_000]
    assert effective_durations == [90_000]


def test_trimmed_request_fails_closed_when_source_is_not_seekable(
    monkeypatch,
    tmp_path,
):
    controller, _downloader, _played = _controller_with_downloader(
        tmp_path,
        auto_download=False,
    )
    monkeypatch.setattr(
        playback_module.QTimer,
        "singleShot",
        lambda _delay, callback: callback(),
    )
    controller.player = _TrimPlayer(seekable=False)
    controller._play_source = MediaController._play_source.__get__(controller)
    errors: list[str] = []
    controller.error_occurred.connect(errors.append)
    source = tmp_path / "unseekable.mp4"
    source.write_bytes(b"media")

    controller.start_playback(
        MediaPlaybackRequest(
            str(source),
            trim=MediaTrim(start_trim_ticks=10_000 * 10_000),
        )
    )

    assert errors and "reliable seeking" in errors[-1]
    assert controller.current_url == ""


def test_custom_end_emits_once_and_replay_returns_to_custom_start(tmp_path):
    controller, _downloader, _played = _controller_with_downloader(
        tmp_path,
        auto_download=False,
    )
    player = _TrimPlayer()
    controller.player = player
    controller._playback_range = MediaTrim(
        start_trim_ticks=10_000 * 10_000,
        end_trim_ticks=20_000 * 10_000,
    ).resolve(120_000)
    controller._trim_gate_open = True
    ended: list[bool] = []
    controller.media_ended.connect(lambda: ended.append(True))

    controller._on_position(100_000)
    controller._on_position(100_000)
    controller.replay()

    assert ended == [True]
    assert player.positions[-1] == 10_000


def test_local_handoff_restarts_unresolved_trim_preparation(
    monkeypatch,
    tmp_path,
):
    controller, _downloader, _played = _controller_with_downloader(
        tmp_path,
        auto_download=False,
    )
    monkeypatch.setattr(
        playback_module.QTimer,
        "singleShot",
        lambda _delay, callback: callback(),
    )
    player = _TrimPlayer()
    remote = "https://cdn.example/bounded.mp4"
    player.setSource(playback_module.QUrl(remote))
    controller.player = player
    controller._request = MediaPlaybackRequest(
        remote,
        trim=MediaTrim(start_trim_ticks=10_000_000),
    )
    controller._session.begin_playback(remote)
    calls: list[tuple[int, int]] = []
    controller._prepare_trimmed_source = (
        lambda session_id, generation: calls.append((session_id, generation))
    )
    local = tmp_path / "bounded.mp4"
    local.write_bytes(b"media")

    controller._switch_to_local(str(local), local_is_temp=False)

    assert calls == [
        (controller._session.session_id, controller._source_generation)
    ]
    assert controller._trim_gate_open is False


def test_reconnect_restarts_unresolved_trim_preparation(monkeypatch, tmp_path):
    controller, _downloader, _played = _controller_with_downloader(
        tmp_path,
        auto_download=False,
    )
    monkeypatch.setattr(
        playback_module.QTimer,
        "singleShot",
        lambda _delay, callback: callback(),
    )
    player = _TrimPlayer()
    controller.player = player
    remote = "https://cdn.example/bounded.mp4"
    controller._request = MediaPlaybackRequest(
        remote,
        trim=MediaTrim(start_trim_ticks=10_000_000),
    )
    controller._session.begin_playback(remote)
    calls: list[tuple[int, int]] = []
    controller._prepare_trimmed_source = (
        lambda session_id, generation: calls.append((session_id, generation))
    )

    controller._restore_reconnect_position(
        source=remote,
        saved_pos=0,
        reconnect_session=controller._session.session_id,
        source_generation=controller._source_generation,
    )

    assert calls == [
        (controller._session.session_id, controller._source_generation)
    ]


def test_network_playback_errors_keep_projection_state_and_continue_retrying(tmp_path):
    controller, downloader, _played = _controller_with_downloader(
        tmp_path,
        auto_download=True,
    )
    reconnect_timer = _ReconnectTimer()
    controller._reconnect_timer = reconnect_timer
    errors: list[str] = []
    interruptions: list[tuple[str, str]] = []
    controller.error_occurred.connect(errors.append)
    controller.playback_interrupted.connect(
        lambda url, message: interruptions.append((url, message))
    )

    _start(controller, "https://cdn.example/song.mp3")
    controller._on_error(None, "Error number -10054 occurred")
    controller._on_error(None, "Error number -10054 occurred")
    controller._on_error(None, "Error number -10054 occurred")

    assert reconnect_timer.started == 3
    assert reconnect_timer.intervals == [3000, 3000, 3000]
    assert errors == []
    assert interruptions == [
        ("https://cdn.example/song.mp3", "Error number -10054 occurred")
    ]
    assert controller.current_url == "https://cdn.example/song.mp3"
    assert downloader.cancel_count == 0


def test_recovery_changed_signal_wraps_remote_reconnect_state(tmp_path):
    controller, _downloader, _played = _controller_with_downloader(
        tmp_path,
        auto_download=True,
    )
    controller._reconnect_timer = _ReconnectTimer()
    recovery_states: list[bool] = []
    controller.playback_recovery_changed.connect(recovery_states.append)

    _start(controller, "https://cdn.example/song.mp3")
    controller._on_position(42_000)
    controller._on_error(None, "Error number -10054 occurred")
    controller._on_error(None, "Error number -10054 occurred")
    controller._on_status(QMediaPlayer.MediaStatus.LoadedMedia)

    assert recovery_states == [True, False]


def test_reconnect_flushes_source_restores_position_and_resumes(monkeypatch, tmp_path):
    controller, _downloader, _played = _controller_with_downloader(
        tmp_path,
        auto_download=True,
    )
    monkeypatch.setattr(
        playback_module.QTimer,
        "singleShot",
        lambda _delay, callback: callback(),
    )
    player = _ReconnectPlayer()

    _start(controller, "https://cdn.example/song.mp3")
    controller._on_position(42_000)
    controller.player = player
    controller._do_reconnect()

    assert player.stopped == 1
    assert player.sources == ["", "https://cdn.example/song.mp3"]
    assert player.positions == [42_000]
    assert player.played == 1
    assert player.paused == 0


def test_reconnect_resume_respects_pause_requested_while_loading(monkeypatch, tmp_path):
    controller, _downloader, _played = _controller_with_downloader(
        tmp_path,
        auto_download=True,
    )
    delayed_callbacks = []

    def fake_single_shot(delay, callback):
        if delay == 50:
            callback()
            return
        delayed_callbacks.append(callback)

    monkeypatch.setattr(playback_module.QTimer, "singleShot", fake_single_shot)
    player = _ReconnectPlayer()

    _start(controller, "https://cdn.example/song.mp3")
    controller._on_position(42_000)
    controller.player = player
    controller._do_reconnect()
    controller.pause()
    for callback in delayed_callbacks:
        callback()

    assert player.positions == [42_000]
    assert player.played == 0
    assert player.paused == 2
    assert controller._session.requested_playing is False


def test_reconnect_no_media_status_schedules_next_remote_retry(monkeypatch, tmp_path):
    controller, _downloader, _played = _controller_with_downloader(
        tmp_path,
        auto_download=True,
    )
    monkeypatch.setattr(
        playback_module.QTimer,
        "singleShot",
        lambda _delay, callback: callback(),
    )
    reconnect_timer = _ReconnectTimer()
    controller._reconnect_timer = reconnect_timer
    player = _NoMediaReconnectPlayer()

    _start(controller, "https://cdn.example/song.mp3")
    controller.player = player
    controller._last_playback_error = "Could not open media."
    controller._stream_recovering = True

    controller._restore_reconnect_position(
        source="https://cdn.example/song.mp3",
        saved_pos=42_000,
        reconnect_session=controller._session.session_id,
        source_generation=controller._source_generation,
    )

    assert reconnect_timer.started == 1
    assert reconnect_timer.intervals == [3000]
    assert player.played == 0
    assert player.paused == 0
    assert controller.current_url == "https://cdn.example/song.mp3"


def test_end_of_media_after_network_error_does_not_close_projection(tmp_path):
    controller, _downloader, _played = _controller_with_downloader(
        tmp_path,
        auto_download=True,
    )
    controller._reconnect_timer = _ReconnectTimer()
    ended = []
    controller.media_ended.connect(lambda: ended.append("ended"))

    _start(controller, "https://cdn.example/song.mp3")
    controller._on_position(937937)
    controller._on_error(None, "Error number -10054 occurred")
    controller._on_status(QMediaPlayer.MediaStatus.EndOfMedia)

    assert ended == []
    assert controller.current_url == "https://cdn.example/song.mp3"


def test_offline_reconnect_open_failure_keeps_retrying_without_closing(tmp_path):
    controller, _downloader, _played = _controller_with_downloader(
        tmp_path,
        auto_download=True,
    )
    reconnect_timer = _ReconnectTimer()
    controller._reconnect_timer = reconnect_timer
    errors: list[str] = []
    interruptions: list[tuple[str, str]] = []
    controller.error_occurred.connect(errors.append)
    controller.playback_interrupted.connect(
        lambda url, message: interruptions.append((url, message))
    )

    _start(controller, "https://akdd1.jw-cdn.org/media/video.mp4")
    controller._on_position(42_000)
    controller._on_error(None, "Error number -10054 occurred")
    controller._on_position(0)
    controller._on_error(
        None,
        "Could not open media. FFmpeg error description: I/O error",
    )

    assert reconnect_timer.started == 2
    assert reconnect_timer.intervals == [3000, 3000]
    assert errors == []
    assert interruptions == [
        (
            "https://akdd1.jw-cdn.org/media/video.mp4",
            "Error number -10054 occurred",
        )
    ]
    assert controller.current_url == "https://akdd1.jw-cdn.org/media/video.mp4"
    assert controller._last_known_position == 42_000


def test_unknown_duration_remote_end_is_normal_completion(tmp_path):
    controller, _downloader, _played = _controller_with_downloader(
        tmp_path,
        auto_download=True,
    )
    reconnect_timer = _ReconnectTimer()
    controller._reconnect_timer = reconnect_timer
    ended = []
    interruptions: list[tuple[str, str]] = []
    controller.media_ended.connect(lambda: ended.append("ended"))
    controller.playback_interrupted.connect(
        lambda url, message: interruptions.append((url, message))
    )

    _start(controller, "https://cdn.example/song.mp3")
    controller._on_position(42_000)
    controller._on_status(QMediaPlayer.MediaStatus.EndOfMedia)

    assert ended == ["ended"]
    assert reconnect_timer.started == 0
    assert interruptions == []
    assert controller._session.requested_playing is False


def test_unexpected_remote_end_before_error_starts_recovery(tmp_path):
    controller, _downloader, _played = _controller_with_downloader(
        tmp_path,
        auto_download=True,
    )
    reconnect_timer = _ReconnectTimer()
    controller._reconnect_timer = reconnect_timer
    ended = []
    interruptions: list[tuple[str, str]] = []
    controller.media_ended.connect(lambda: ended.append("ended"))
    controller.playback_interrupted.connect(
        lambda url, message: interruptions.append((url, message))
    )

    _start(controller, "https://cdn.example/song.mp3")
    controller._on_position(42_000)
    controller.player = _ReconnectPlayer()
    controller._on_status(QMediaPlayer.MediaStatus.EndOfMedia)

    assert ended == []
    assert reconnect_timer.started == 1
    assert interruptions == [
        ("https://cdn.example/song.mp3", "The media stream was interrupted.")
    ]
    assert controller.current_url == "https://cdn.example/song.mp3"


def test_network_reconnect_uses_fixed_retry_interval(tmp_path):
    controller, _downloader, _played = _controller_with_downloader(
        tmp_path,
        auto_download=True,
    )
    reconnect_timer = _ReconnectTimer()
    controller._reconnect_timer = reconnect_timer

    _start(controller, "https://cdn.example/song.mp3")
    for _ in range(8):
        controller._on_error(None, "Error number -10054 occurred")

    assert reconnect_timer.intervals == [3000] * 8


def test_qt_network_error_without_message_keeps_reconnecting(tmp_path):
    controller, _downloader, _played = _controller_with_downloader(
        tmp_path,
        auto_download=True,
    )
    controller._reconnect_timer = _ReconnectTimer()
    errors: list[str] = []
    interruptions: list[tuple[str, str]] = []
    controller.error_occurred.connect(errors.append)
    controller.playback_interrupted.connect(
        lambda url, message: interruptions.append((url, message))
    )

    _start(controller, "https://cdn.example/song.mp3")
    controller._on_error(QMediaPlayer.Error.NetworkError, "")

    assert errors == []
    assert interruptions == [
        ("https://cdn.example/song.mp3", "The media stream was interrupted.")
    ]
    assert controller.current_url == "https://cdn.example/song.mp3"


def test_toggle_during_stream_recovery_pauses_requested_playback(tmp_path):
    controller, _downloader, _played = _controller_with_downloader(
        tmp_path,
        auto_download=True,
    )
    controller._reconnect_timer = _ReconnectTimer()

    _start(controller, "https://cdn.example/song.mp3")
    controller._on_error(None, "Error number -10054 occurred")
    controller.toggle_play_pause()

    assert controller._session.requested_playing is False
