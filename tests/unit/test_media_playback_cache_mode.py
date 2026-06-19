from __future__ import annotations

from PySide6.QtCore import QCoreApplication, QObject, Signal
from PySide6.QtMultimedia import QMediaPlayer

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


class _ReconnectTimer:
    def __init__(self) -> None:
        self.started = 0
        self.stopped = 0

    def start(self) -> None:
        self.started += 1

    def stop(self) -> None:
        self.stopped += 1


def test_network_playback_errors_become_terminal_after_bounded_retries(tmp_path):
    controller, downloader, _played = _controller_with_downloader(
        tmp_path,
        auto_download=True,
    )
    reconnect_timer = _ReconnectTimer()
    controller._reconnect_timer = reconnect_timer
    controller._max_reconnect_attempts = 2
    errors: list[str] = []
    controller.error_occurred.connect(errors.append)

    controller.play_url("https://cdn.example/song.mp3")
    controller._on_error(None, "Error number -10054 occurred")
    controller._on_error(None, "Error number -10054 occurred")

    assert reconnect_timer.started == 2
    assert errors == []

    controller._on_error(None, "Error number -10054 occurred")

    assert errors == ["Error number -10054 occurred"]
    assert controller.current_url == ""
    assert downloader.cancel_count >= 1


def test_qt_network_error_without_message_fails_with_user_safe_detail(tmp_path):
    controller, _downloader, _played = _controller_with_downloader(
        tmp_path,
        auto_download=True,
    )
    controller._max_reconnect_attempts = 0
    errors: list[str] = []
    controller.error_occurred.connect(errors.append)

    controller.play_url("https://cdn.example/song.mp3")
    controller._on_error(QMediaPlayer.Error.NetworkError, "")

    assert errors == ["The media could not be opened."]
    assert controller.current_url == ""
