from __future__ import annotations

from types import SimpleNamespace

from PySide6.QtCore import QCoreApplication

from solin.core.media.cache import MediaCacheManager
from solin.core.media.download_storage import cached_path_for
from solin.widgets.media_library_widget import MediaLibraryWidget


def _app():
    return QCoreApplication.instance() or QCoreApplication([])


def _fake_library(
    *,
    audio_mode: bool,
    items: list[dict],
    cache_manager: MediaCacheManager,
    supports_audio: bool = True,
):
    fake = SimpleNamespace(
        kind="songs",
        _audio_mode=audio_mode,
        items=items,
        _cache_manager=cache_manager,
        bridge=SimpleNamespace(supports_audio=supports_audio),
        tr=lambda text: text,
    )
    fake._ordered_items = lambda values: MediaLibraryWidget._ordered_items(fake, values)
    fake._download_all_mode = lambda: MediaLibraryWidget._download_all_mode(fake)
    return fake


def test_pending_download_all_urls_uses_audio_mode_items(tmp_path):
    _app()
    cache_manager = MediaCacheManager(tmp_path)
    cached_url = "https://cdn.example/song-003.mp3"
    cached_path = cached_path_for(cached_url, cache_manager.media_cache_dir)
    with open(cached_path, "w", encoding="utf-8") as handle:
        handle.write("cached")
    with open(cached_path + ".done", "w", encoding="utf-8") as handle:
        handle.write(cached_url)

    library = _fake_library(
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

    pending = MediaLibraryWidget._pending_download_all_urls(library)

    assert pending == [
        "https://cdn.example/song-001.mp3",
        "https://cdn.example/song-002.mp3",
    ]


def test_download_all_text_changes_with_media_mode(tmp_path):
    cache_manager = MediaCacheManager(tmp_path)
    audio_library = _fake_library(
        audio_mode=True,
        items=[],
        cache_manager=cache_manager,
    )
    video_library = _fake_library(
        audio_mode=False,
        items=[],
        cache_manager=cache_manager,
    )

    assert MediaLibraryWidget._download_all_title(audio_library) == "Download all audio songs"
    assert MediaLibraryWidget._download_all_title(video_library) == "Download all video songs"
    assert (
        MediaLibraryWidget._download_all_confirm_text(audio_library, "audio", 3)
        == "Download 3 audio songs for offline playback?"
    )
    assert (
        MediaLibraryWidget._download_all_complete_text(audio_library, "audio")
        == "All audio songs downloaded"
    )
