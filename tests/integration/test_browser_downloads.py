from __future__ import annotations

from pathlib import Path
import threading

import solin.widgets.browser.widget as browser_widget
from solin.core.media.browser_downloads import BrowserDownloadService
from solin.core.media.download_storage import cached_path_for
from solin.core.network.http import HttpError
from solin.widgets.browser.downloads import BrowserDownloadsMixin


class _ByteStream:
    def __init__(self, chunks: tuple[bytes, ...]) -> None:
        self._chunks = chunks

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        del exc_type, exc, traceback

    def iter_bytes(self, chunk_size: int):
        assert chunk_size > 0
        yield from self._chunks


def test_browser_widget_uses_downloads_mixin():
    assert browser_widget.BrowserDownloadsMixin is BrowserDownloadsMixin
    assert issubclass(browser_widget.BrowserWidget, BrowserDownloadsMixin)
    assert (
        browser_widget.BrowserWidget._on_download_requested
        is BrowserDownloadsMixin._on_download_requested
    )


def test_browser_download_cache_path_is_stable_and_sanitized(tmp_path):
    service = BrowserDownloadService(tmp_path, notify_cached=lambda _url: None)
    url = "https://example.test/files/bad%3Aname.pdf?download=1"

    path = service.destination_cache_path(url, "ignored title", "pdf")

    assert path.parent == tmp_path
    assert path.suffix == ".pdf"
    assert "bad_name-" in path.name
    assert path == service.destination_cache_path(url, "ignored title", "pdf")


def test_browser_image_download_uses_renderable_extension(tmp_path):
    service = BrowserDownloadService(tmp_path, notify_cached=lambda _url: None)

    path = service.destination_cache_path(
        "https://example.test/image-handler.php?id=42",
        "Illustration",
        "image",
    )

    assert path.suffix == ".jpg"


def test_browser_download_service_persists_remote_playlist_file(tmp_path):
    url = "https://example.test/publication.pdf"
    notified = []
    ready = []
    completed = threading.Event()
    service = BrowserDownloadService(
        tmp_path,
        notify_cached=notified.append,
        stream_factory=lambda *_args, **_kwargs: _ByteStream((b"abc", b"def")),
    )

    service.download_file_for_destination(
        url,
        "Publication",
        "pdf",
        on_ready=lambda *args: ready.append(args) or completed.set(),
        on_failed=lambda *_args: completed.set(),
    )

    assert completed.wait(2)
    assert len(ready) == 1
    path = Path(ready[0][0])
    assert path.read_bytes() == b"abcdef"
    assert Path(f"{path}.done").read_text(encoding="utf-8") == url
    assert ready[0][1:] == ("Publication", "pdf")
    assert notified == [url]
    assert service.shutdown() == []


def test_browser_download_service_reuses_only_matching_completed_file(tmp_path):
    url = "https://example.test/publication.jwpub"
    service = BrowserDownloadService(
        tmp_path,
        notify_cached=lambda _url: None,
        stream_factory=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("cached file must not be downloaded")
        ),
    )
    path = service.destination_cache_path(url, "Publication", "jwpub")
    path.write_bytes(b"cached")
    Path(f"{path}.done").write_text(url, encoding="utf-8")
    ready = []

    service.download_file_for_destination(
        url,
        "Publication",
        "jwpub",
        on_ready=lambda *args: ready.append(args),
        on_failed=lambda *_args: None,
    )

    assert ready == [(str(path), "Publication", "jwpub")]


def test_browser_download_service_reports_failure_and_removes_temp_file(tmp_path):
    completed = threading.Event()
    failures = []

    def fail(*_args, **_kwargs):
        raise HttpError("offline")

    service = BrowserDownloadService(
        tmp_path,
        notify_cached=lambda _url: None,
        stream_factory=fail,
    )
    service.download_file_for_destination(
        "https://example.test/publication.pdf",
        "Publication",
        "pdf",
        on_ready=lambda *_args: completed.set(),
        on_failed=lambda *args: failures.append(args) or completed.set(),
    )

    assert completed.wait(2)
    assert failures == [("Publication", "offline")]
    assert list(tmp_path.glob("*.tmp")) == []
    assert list(tmp_path.glob(".*.tmp")) == []


def test_browser_download_service_rejects_new_work_after_shutdown(tmp_path):
    stream_called = False

    def stream(*_args, **_kwargs):
        nonlocal stream_called
        stream_called = True
        return _ByteStream((b"content",))

    service = BrowserDownloadService(
        tmp_path,
        notify_cached=lambda _url: None,
        stream_factory=stream,
    )
    assert service.shutdown() == []

    service.cache_media("https://example.test/media.mp4")

    assert stream_called is False
    assert list(tmp_path.iterdir()) == []


def test_browser_media_cache_uses_shared_hashed_storage_contract(tmp_path):
    url = "https://example.test/media/video.mp4?quality=720"
    notified = []
    completed = threading.Event()
    service = BrowserDownloadService(
        tmp_path,
        notify_cached=lambda value: notified.append(value) or completed.set(),
        stream_factory=lambda *_args, **_kwargs: _ByteStream((b"video",)),
    )

    service.cache_media(url)

    assert completed.wait(2)
    path = Path(cached_path_for(url, tmp_path))
    assert path.read_bytes() == b"video"
    assert Path(f"{path}.done").read_text(encoding="utf-8") == url
    assert notified == [url]
    assert service.shutdown() == []
