from __future__ import annotations

from PySide6.QtCore import QCoreApplication, QObject, Signal
from PySide6.QtMultimedia import QMediaPlayer

import solin.core.media.playback as playback_module
from solin.core.media.cache import MediaCacheManager
from solin.core.media.playback import MediaController


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


def test_play_url_uses_auto_download_setting_by_default(tmp_path):
    controller, downloader, played = _controller_with_downloader(
        tmp_path,
        auto_download=True,
    )

    controller.play_url("https://cdn.example/song.mp3")

    assert played == ["https://cdn.example/song.mp3"]
    assert downloader.started == [("https://cdn.example/song.mp3", True)]
    assert controller.stream_persist is True
    controller.stop()


def test_play_url_can_force_temporary_download(tmp_path):
    controller, downloader, played = _controller_with_downloader(
        tmp_path,
        auto_download=True,
    )

    controller.play_url("https://cdn.example/song.mp3", download_persist=False)

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

    controller.play_url("https://cdn.example/song.mp3")
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

    controller.play_url(str(local_media))
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

    controller.play_url("https://cdn.example/song.mp3")
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

    controller.play_url("https://cdn.example/song.mp3")
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

    controller.play_url("https://cdn.example/song.mp3")
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

    controller.play_url("https://cdn.example/song.mp3")
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

    controller.play_url("https://cdn.example/song.mp3")
    controller.player = player
    controller._last_playback_error = "Could not open media."
    controller._stream_recovering = True

    controller._restore_reconnect_position(
        source="https://cdn.example/song.mp3",
        saved_pos=42_000,
        reconnect_session=controller._session.session_id,
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

    controller.play_url("https://cdn.example/song.mp3")
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

    controller.play_url("https://akdd1.jw-cdn.org/media/video.mp4")
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

    controller.play_url("https://cdn.example/song.mp3")
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

    controller.play_url("https://cdn.example/song.mp3")
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

    controller.play_url("https://cdn.example/song.mp3")
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

    controller.play_url("https://cdn.example/song.mp3")
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

    controller.play_url("https://cdn.example/song.mp3")
    controller._on_error(None, "Error number -10054 occurred")
    controller.toggle_play_pause()

    assert controller._session.requested_playing is False
