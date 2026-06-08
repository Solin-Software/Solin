from __future__ import annotations

from PySide6.QtCore import QCoreApplication

from app.core.media import playback as playback_module
from app.core.media.playback import MediaController


def _app():
    return QCoreApplication.instance() or QCoreApplication([])


class _Prefs:
    def __init__(self, auto_download: bool) -> None:
        self._auto_download = auto_download

    def value(self, _key, default=None, _type=None):
        return self._auto_download if _type is bool else default


class _Downloader:
    def __init__(self) -> None:
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


def _controller_with_downloader(monkeypatch, *, auto_download: bool):
    _app()
    monkeypatch.setattr(playback_module._ps, "prefs", lambda: _Prefs(auto_download))
    controller = MediaController()
    downloader = _Downloader()
    played: list[str] = []
    controller._downloader = downloader
    controller._play_source = played.append
    return controller, downloader, played


def test_play_url_uses_auto_download_setting_by_default(monkeypatch):
    controller, downloader, played = _controller_with_downloader(
        monkeypatch,
        auto_download=True,
    )

    controller.play_url("https://cdn.example/song.mp3")

    assert played == ["https://cdn.example/song.mp3"]
    assert downloader.started == [("https://cdn.example/song.mp3", True)]
    assert controller._stream_persist is True
    controller.stop()


def test_play_url_can_force_temporary_download(monkeypatch):
    controller, downloader, played = _controller_with_downloader(
        monkeypatch,
        auto_download=True,
    )

    controller.play_url("https://cdn.example/song.mp3", download_persist=False)

    assert played == ["https://cdn.example/song.mp3"]
    assert downloader.started == [("https://cdn.example/song.mp3", False)]
    assert controller._stream_persist is False
    controller.stop()
