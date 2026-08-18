from __future__ import annotations

from types import SimpleNamespace

from PySide6.QtCore import QCoreApplication

from solin.core.media.cache import MediaCacheManager
from solin.core.media.download_storage import cached_path_for
from solin.widgets.library_widget import LibraryWidget


def _app():
    return QCoreApplication.instance() or QCoreApplication([])


def _fake_library(
    *,
    section: str,
    audio_mode: bool,
    items: list[dict],
    cache_manager: MediaCacheManager,
    supports_audio: bool = True,
):
    catalogs = {
        "songs": SimpleNamespace(items=[], audio_mode=audio_mode),
        "clips": SimpleNamespace(items=[], audio_mode=False),
    }
    catalogs[section].items = items
    fake = SimpleNamespace(
        _active_section=section,
        _catalogs=catalogs,
        _cache_manager=cache_manager,
        tr=lambda text: text,
    )
    fake._ordered_items = lambda current: LibraryWidget._ordered_items(fake, current)
    fake._songs_support_audio = lambda: supports_audio
    return fake


def test_pending_download_urls_uses_audio_mode_items(tmp_path):
    _app()
    cache_manager = MediaCacheManager(
        tmp_path,
        downloader_factory=lambda _parent: None,
    )
    cached_url = "https://cdn.example/song-003.mp3"
    cached_path = cached_path_for(cached_url, cache_manager.media_cache_dir)
    with open(cached_path, "w", encoding="utf-8") as handle:
        handle.write("cached")
    with open(cached_path + ".done", "w", encoding="utf-8") as handle:
        handle.write(cached_url)

    library = _fake_library(
        section="songs",
        audio_mode=True,
        cache_manager=cache_manager,
        items=[
            {"number": 2, "url": "https://cdn.example/song-002.mp3"},
            {"number": 1, "url": "https://cdn.example/song-001.mp3"},
            {"number": 4, "url": "file:///local/song-004.mp3"},
            {"number": 2, "url": "https://cdn.example/song-002.mp3"},
            {"number": 3, "url": cached_url},
        ],
    )

    pending = LibraryWidget._pending_download_urls(library, "songs")

    assert pending == [
        "https://cdn.example/song-001.mp3",
        "https://cdn.example/song-002.mp3",
    ]


def test_pending_download_urls_supports_music_video_catalog(tmp_path):
    cache_manager = MediaCacheManager(
        tmp_path,
        downloader_factory=lambda _parent: None,
    )
    library = _fake_library(
        section="clips",
        audio_mode=False,
        cache_manager=cache_manager,
        items=[
            {"title": "One", "url": "https://cdn.example/one.mp4"},
            {"title": "Two", "url": "https://cdn.example/two.mp3"},
        ],
    )

    assert LibraryWidget._pending_download_urls(library, "clips") == [
        "https://cdn.example/one.mp4",
        "https://cdn.example/two.mp3",
    ]


def test_download_all_text_changes_with_collection_and_media_mode(tmp_path):
    cache_manager = MediaCacheManager(
        tmp_path,
        downloader_factory=lambda _parent: None,
    )
    audio_library = _fake_library(
        section="songs",
        audio_mode=True,
        items=[],
        cache_manager=cache_manager,
    )
    clips_library = _fake_library(
        section="clips",
        audio_mode=False,
        items=[],
        cache_manager=cache_manager,
    )

    assert (
        LibraryWidget._download_all_title(audio_library, "songs", "audio")
        == "Download all audio songs"
    )
    assert (
        LibraryWidget._download_all_title(clips_library, "clips", "clips")
        == "Download all music videos"
    )
    assert (
        LibraryWidget._download_all_confirmation(clips_library, "clips", "clips", 3)
        == "Download 3 music videos for offline playback?"
    )
