import os
from types import SimpleNamespace

import pytest

from solin.controllers.open_media_controller import (
    OpenMediaContext,
    OpenMediaController,
    OpenMediaHandlers,
)
from solin.core.foundation.qt_threads import OwnedQThreadRegistry
from solin.core.media.formats import mime_to_ext


class _NavigationStub:
    def __init__(self):
        self.pages = []

    def switch_page(self, index):
        self.pages.append(index)


class _PlaylistImportStub:
    def __init__(self):
        self.temp_playlists = []

    def send_to_temp_playlist(self, playlist):
        self.temp_playlists.append(playlist)


class _ProjectionBarStub:
    def __init__(self):
        self.expanded = 0

    def expand_overlay(self):
        self.expanded += 1


class _PlaylistWidgetStub:
    def __init__(self):
        self.opened_pdf_playlists = []
        self.opened_named_playlists = []
        self.appended = []
        self.native_imports = []

    def open_pdf_as_temp_playlist(self, items, stem):
        self.opened_pdf_playlists.append((items, stem))
        return "pdf-session"

    def open_temp_playlist(self, items, _lang, *, name=None):
        self.opened_named_playlists.append((items, name))
        return "named-session"

    def append_temp_playlist_items(self, playlist_id, items):
        self.appended.append((playlist_id, items))
        return True

    def import_native_playlists(self, paths, open_after=False):
        self.native_imports.append((paths, open_after))


class _NotificationsStub:
    def __init__(self):
        self.successes = []

    def success(self, message):
        self.successes.append(message)


class _SignalStub:
    def __init__(self):
        self.callbacks = []

    def connect(self, callback):
        self.callbacks.append(callback)
        return callback


class _ThreadStub:
    def __init__(self):
        self.items_ready = _SignalStub()
        self.failed = _SignalStub()
        self.pages_ready = _SignalStub()
        self.conversion_failed = _SignalStub()
        self.finished = _SignalStub()
        self.started = False

    def start(self):
        self.started = True


class _JwpubImportThreadFactoryStub:
    def __init__(self):
        self.calls = []
        self.threads = []

    def create(self, path, **kwargs):
        thread = _ThreadStub()
        self.calls.append((path, kwargs))
        self.threads.append(thread)
        return thread


class _DocumentConversionServiceStub:
    def __init__(self):
        self.pdf_pages = None
        self.office_pages = None
        self.pdf_calls = []
        self.office_calls = []
        self.threads = []

    def office_conversion_available(self):
        return True

    def cached_pdf_pages(self, path):
        return self.pdf_pages

    def create_pdf_thread(self, path, **kwargs):
        thread = _ThreadStub()
        self.pdf_calls.append((path, kwargs))
        self.threads.append(thread)
        return thread

    def cached_office_pages(self, path):
        return self.office_pages

    def create_office_thread(self, path, **kwargs):
        thread = _ThreadStub()
        self.office_calls.append((path, kwargs))
        self.threads.append(thread)
        return thread


class _WindowStub:
    def __init__(self):
        self._jwl_tmp_files = set()
        self._navigation = _NavigationStub()
        self._playlist_imports = _PlaylistImportStub()
        self.proj_bar = _ProjectionBarStub()
        self.playlist_widget = _PlaylistWidgetStub()
        self.notifications = _NotificationsStub()
        self.lang = SimpleNamespace(
            api_code="E",
            jw_lang_service=SimpleNamespace(media_api_code="T"),
        )
        self.projected = None

    def tr(self, text):
        return text

    def _project_media_at_index(
        self,
        playlist,
        index=0,
        keep_expanded=False,
        playback_order=None,
    ):
        self.projected = (playlist, index, keep_expanded, playback_order)


def _controller(
    window,
    jwpub_import_thread_factory=None,
    document_conversion_service=None,
):
    return OpenMediaController(
        OpenMediaContext(
            dialog_parent=window,
            document_conversion_service=(
                document_conversion_service
                or _DocumentConversionServiceStub()
            ),
            jwpub_import_thread_factory=(
                jwpub_import_thread_factory
                or _JwpubImportThreadFactoryStub()
            ),
            language_manager=window.lang,
            notifications=window.notifications,
            thread_registry=OwnedQThreadRegistry(),
            temp_files=window._jwl_tmp_files,
            translate=window.tr,
        ),
        OpenMediaHandlers(
            switch_to_playlist=lambda: window._navigation.switch_page(7),
            project_media_at_index=window._project_media_at_index,
            expand_projection_overlay=window.proj_bar.expand_overlay,
            send_to_temp_playlist=window._playlist_imports.send_to_temp_playlist,
            open_pdf_temp_playlist=window.playlist_widget.open_pdf_as_temp_playlist,
            open_named_temp_playlist=lambda items, name: (
                window.playlist_widget.open_temp_playlist(
                    items,
                    window.lang,
                    name=name,
                )
            ),
            append_temp_playlist_items=(
                window.playlist_widget.append_temp_playlist_items
            ),
            import_native_playlists=window.playlist_widget.import_native_playlists,
        ),
    )


def test_mime_to_ext_maps_known_and_safe_fallbacks():
    assert mime_to_ext("image/jpeg") == ".jpg"
    assert mime_to_ext("video/quicktime") == ".mov"
    assert mime_to_ext("audio/x-flac") == ".flac"
    assert mime_to_ext("audio/vnd.custom") == ".mp3"
    assert mime_to_ext("video/vnd.custom") == ".mp4"
    assert mime_to_ext("application/octet-stream") == ".jpg"


@pytest.mark.parametrize(
    ("filename", "media_type"),
    [
        ("talk.mp3", "audio"),
        ("clip.mp4", "video"),
        ("slide.webp", "image"),
    ],
)
def test_open_media_files_projects_single_local_media_and_expands(
    monkeypatch,
    tmp_path,
    filename,
    media_type,
):
    window = _WindowStub()
    controller = _controller(window)
    media_path = str(tmp_path / filename)
    timers = []
    monkeypatch.setattr(
        "solin.controllers.open_media_controller.QTimer",
        SimpleNamespace(
            singleShot=lambda ms, callback: timers.append(ms) or callback()
        ),
    )

    controller.open_media_files([media_path])

    playlist, index, keep_expanded, playback_order = window.projected
    assert playlist == [{"url": media_path, "title": filename, "type": media_type}]
    assert index == 0
    assert keep_expanded is False
    assert playback_order is None
    assert timers == [50]
    assert window.proj_bar.expanded == 1
    assert window._playlist_imports.temp_playlists == []
    assert window._navigation.pages == []


def test_open_media_files_sends_multiple_media_items_to_temp_playlist(tmp_path):
    window = _WindowStub()
    controller = _controller(window)
    video_path = str(tmp_path / "clip.mp4")
    image_path = str(tmp_path / "slide.png")

    controller.open_media_files([video_path, image_path])

    assert window.projected is None
    assert window._playlist_imports.temp_playlists == [[
        {"url": video_path, "title": "clip.mp4", "type": "video"},
        {"url": image_path, "title": "slide.png", "type": "image"},
    ]]
    assert window.proj_bar.expanded == 0


def test_open_media_files_routes_native_playlist_to_dedicated_import(tmp_path):
    window = _WindowStub()
    controller = _controller(window)
    package = str(tmp_path / "complete.solinplaylist")

    controller.open_media_files([package])

    assert window.playlist_widget.native_imports == [([package], True)]
    assert window.projected is None
    assert window._playlist_imports.temp_playlists == []


def test_open_media_files_sends_multiple_http_media_to_temp_playlist():
    window = _WindowStub()
    controller = _controller(window)

    controller.open_media_files([
        "https://example.test/media/opening.mp4?token=abc",
        "https://example.test/media/song.m4a",
    ])

    assert window.projected is None
    assert window._playlist_imports.temp_playlists == [[
        {
            "url": "https://example.test/media/opening.mp4?token=abc",
            "title": "opening.mp4",
            "type": "video",
        },
        {
            "url": "https://example.test/media/song.m4a",
            "title": "song.m4a",
            "type": "audio",
        },
    ]]
    assert window.proj_bar.expanded == 0


def test_open_media_files_classifies_http_audio_by_url_extension(monkeypatch):
    window = _WindowStub()
    controller = _controller(window)
    monkeypatch.setattr(
        "solin.controllers.open_media_controller.QTimer",
        SimpleNamespace(singleShot=lambda _ms, callback: callback()),
    )

    controller.open_media_files(["https://example.test/media/song.mp3?download=1"])

    playlist, *_ = window.projected
    assert playlist == [{
        "url": "https://example.test/media/song.mp3?download=1",
        "title": "song.mp3",
        "type": "audio",
    }]


def test_open_media_files_routes_pdf_and_jwpub_to_conversion_methods(tmp_path):
    window = _WindowStub()
    controller = _controller(window)
    routed = []
    controller.open_pdf_as_temp = lambda path: routed.append(("pdf", path))
    controller.open_jwpub_as_temp = lambda path: routed.append(("jwpub", path))
    pdf_path = str(tmp_path / "document.pdf")
    jwpub_path = str(tmp_path / "publication.jwpub")

    controller.open_media_files([pdf_path, jwpub_path])

    assert routed == [("jwpub", jwpub_path), ("pdf", pdf_path)]
    assert window.projected is None
    assert window._playlist_imports.temp_playlists == []


def test_open_media_files_routes_conversions_without_blocking_media_playlist(tmp_path):
    window = _WindowStub()
    controller = _controller(window)
    routed = []
    controller.open_pdf_as_temp = lambda path: routed.append(("pdf", path))
    controller.open_jwpub_as_temp = lambda path: routed.append(("jwpub", path))
    video_path = str(tmp_path / "clip.mp4")
    pdf_path = str(tmp_path / "document.pdf")
    audio_path = str(tmp_path / "song.mp3")
    jwpub_path = str(tmp_path / "publication.jwpub")

    controller.open_media_files([video_path, pdf_path, audio_path, jwpub_path])

    assert routed == [("jwpub", jwpub_path), ("pdf", pdf_path)]
    assert window.projected is None
    assert window._playlist_imports.temp_playlists == [[
        {"url": video_path, "title": "clip.mp4", "type": "video"},
        {"url": audio_path, "title": "song.mp3", "type": "audio"},
    ]]


def test_on_pdf_ready_builds_temp_playlist_and_switches_page():
    window = _WindowStub()
    controller = _controller(window)

    controller.on_pdf_ready(["/tmp/page-1.png", "/tmp/page-2.png"], "document")

    items, stem = window.playlist_widget.opened_pdf_playlists[0]
    assert stem == "document"
    assert window._navigation.pages == [7]
    assert [(item["title"], item["url"], item["type"]) for item in items] == [
        ("document — p. 1", "/tmp/page-1.png", "image"),
        ("document — p. 2", "/tmp/page-2.png", "image"),
    ]


def test_open_pdf_uses_injected_document_conversion_service():
    window = _WindowStub()
    service = _DocumentConversionServiceStub()
    controller = _controller(
        window,
        document_conversion_service=service,
    )

    controller.open_pdf_as_temp("document.pdf")

    assert service.pdf_calls == [("document.pdf", {"parent": window})]
    assert service.threads[0].started is True
    assert controller._context.thread_registry.active_count == 1
    assert window._navigation.pages == [7]


def test_open_jwpub_uses_injected_worker_factory(monkeypatch):
    window = _WindowStub()
    factory = _JwpubImportThreadFactoryStub()
    controller = _controller(
        window,
        jwpub_import_thread_factory=factory,
    )
    monkeypatch.setattr(
        controller,
        "_media_language_context",
        lambda: SimpleNamespace(api_code="T"),
    )

    controller.open_jwpub_as_temp("publication.jwpub")

    assert factory.calls == [
        (
            "publication.jwpub",
            {"lang": "T", "parent": window},
        )
    ]
    assert factory.threads[0].started is True
    assert controller._context.thread_registry.active_count == 1
    assert window._navigation.pages == [7]
    assert window.playlist_widget.opened_named_playlists == [
        ([], "📖  publication")
    ]


def test_expand_jwlplaylist_returns_jworg_and_local_items(monkeypatch):
    window = _WindowStub()
    controller = _controller(window)

    def _fake_read(path, fallback_lang_code):
        assert path == "playlist.jwlplaylist"
        assert fallback_lang_code == "E"
        return {
            "items": [
                {
                    "source": "jworg",
                    "type": "video",
                    "title": "JW Video",
                    "jworg_url": "https://cdn.example/video.mp4",
                },
                {
                    "source": "local",
                    "type": "audio",
                    "title": "Local Audio",
                    "url": "song.mp3",
                },
            ]
        }

    monkeypatch.setattr("solin.core.playlists.jwl_files.read_jwlplaylist", _fake_read)

    assert controller.expand_jwlplaylist("playlist.jwlplaylist") == [
        {
            "url": "https://cdn.example/video.mp4",
            "title": "JW Video",
            "type": "video",
        },
        {"url": "song.mp3", "title": "Local Audio", "type": "audio"},
    ]


def test_expand_jwlplaylist_writes_embedded_media(monkeypatch):
    window = _WindowStub()
    controller = _controller(window)

    monkeypatch.setattr(
        "solin.core.playlists.jwl_files.read_jwlplaylist",
        lambda _path, fallback_lang_code: {
            "items": [
                {
                    "source": "embedded",
                    "type": "image",
                    "title": "Cover",
                    "filename": "cover.webp",
                    "data": b"image-bytes",
                }
            ]
        },
    )

    try:
        result = controller.expand_jwlplaylist("playlist.jwlplaylist")

        assert len(result) == 1
        assert result[0]["title"] == "Cover"
        assert result[0]["type"] == "image"
        assert result[0]["_tmp"] is True
        assert result[0]["url"].endswith(".webp")
        assert result[0]["url"] in window._jwl_tmp_files
        with open(result[0]["url"], "rb") as handle:
            assert handle.read() == b"image-bytes"
    finally:
        for tmp_file in list(window._jwl_tmp_files):
            try:
                os.unlink(tmp_file)
            except OSError:
                pass


def test_expand_jwlplaylist_uses_mime_type_for_embedded_suffix(monkeypatch):
    window = _WindowStub()
    controller = _controller(window)
    monkeypatch.setattr(
        "solin.core.playlists.jwl_files.read_jwlplaylist",
        lambda _path, fallback_lang_code: {
            "items": [
                {
                    "source": "embedded",
                    "type": "audio",
                    "title": "Audio",
                    "filename": "audio",
                    "mime_type": "audio/ogg",
                    "data": b"audio-bytes",
                }
            ]
        },
    )

    try:
        result = controller.expand_jwlplaylist("playlist.jwlplaylist")

        assert len(result) == 1
        assert result[0]["url"].endswith(".ogg")
    finally:
        for tmp_file in list(window._jwl_tmp_files):
            try:
                os.unlink(tmp_file)
            except OSError:
                pass


def test_open_media_controller_uses_explicit_dependencies():
    controller = _controller(_WindowStub())

    assert not hasattr(controller, "_window")
