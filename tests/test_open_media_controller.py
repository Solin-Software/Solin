import os
from types import SimpleNamespace

import pytest

from solin.controllers.open_media_controller import OpenMediaController
from solin.core.foundation.runtime_paths import ProfilePaths
from solin.core.media.formats import mime_to_ext

_PROFILE_PATHS = ProfilePaths.from_roots(
    data_dir="data",
    cache_dir="cache",
    profile_id="test",
)


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

    def open_pdf_as_temp_playlist(self, items, stem):
        self.opened_pdf_playlists.append((items, stem))


class _WindowStub:
    def __init__(self):
        self._jwl_tmp_files = set()
        self._navigation = _NavigationStub()
        self._playlist_imports = _PlaylistImportStub()
        self.proj_bar = _ProjectionBarStub()
        self.playlist_widget = _PlaylistWidgetStub()
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
    controller = OpenMediaController(window, _PROFILE_PATHS)
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
    controller = OpenMediaController(window, _PROFILE_PATHS)
    video_path = str(tmp_path / "clip.mp4")
    image_path = str(tmp_path / "slide.png")

    controller.open_media_files([video_path, image_path])

    assert window.projected is None
    assert window._playlist_imports.temp_playlists == [[
        {"url": video_path, "title": "clip.mp4", "type": "video"},
        {"url": image_path, "title": "slide.png", "type": "image"},
    ]]
    assert window.proj_bar.expanded == 0


def test_open_media_files_sends_multiple_http_media_to_temp_playlist():
    window = _WindowStub()
    controller = OpenMediaController(window, _PROFILE_PATHS)

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
    controller = OpenMediaController(window, _PROFILE_PATHS)
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
    controller = OpenMediaController(window, _PROFILE_PATHS)
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
    controller = OpenMediaController(window, _PROFILE_PATHS)
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
    controller = OpenMediaController(window, _PROFILE_PATHS)

    controller.on_pdf_ready(["/tmp/page-1.png", "/tmp/page-2.png"], "document")

    items, stem = window.playlist_widget.opened_pdf_playlists[0]
    assert stem == "document"
    assert window._navigation.pages == [7]
    assert [(item["title"], item["url"], item["type"]) for item in items] == [
        ("document — p. 1", "/tmp/page-1.png", "image"),
        ("document — p. 2", "/tmp/page-2.png", "image"),
    ]


def test_expand_jwlplaylist_returns_jworg_and_local_items(monkeypatch):
    window = _WindowStub()
    controller = OpenMediaController(window, _PROFILE_PATHS)

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

    monkeypatch.setattr("solin.core.playlists.reader.read_jwlplaylist", _fake_read)

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
    controller = OpenMediaController(window, _PROFILE_PATHS)

    monkeypatch.setattr(
        "solin.core.playlists.reader.read_jwlplaylist",
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


def test_best_ext_prefers_filename_then_mime_type():
    controller = OpenMediaController(_WindowStub(), _PROFILE_PATHS)

    assert controller._best_ext({"filename": "video.mov"}, "video/mp4") == ".mov"
    assert controller._best_ext({"mime_type": "audio/ogg"}, "video/mp4") == ".ogg"
    assert controller._best_ext({}, "video/mp4") == ".mp4"
