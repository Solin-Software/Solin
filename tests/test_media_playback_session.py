from __future__ import annotations

from pathlib import Path

from solin.core.media.playback_session import MediaPlaybackSession


def test_begin_playback_resets_media_state_and_keeps_deferred_switch_flag() -> None:
    session = MediaPlaybackSession()
    session.set_local_switch_deferred(True)
    session.mark_local_switch("old-temp.mp4", True)

    session.begin_playback("https://cdn.example/new.mp4")

    assert session.current_url == "https://cdn.example/new.mp4"
    assert session.local_path is None
    assert session.local_is_temp is False
    assert session.defer_local_switch is True
    assert session.requested_playing is True
    assert session.accepts_frame()


def test_deferred_persistent_download_notifies_cache_once_before_local_switch() -> None:
    session = MediaPlaybackSession()
    session.begin_playback("https://cdn.example/song.mp3")
    session.set_stream_persist(True)
    session.set_local_switch_deferred(True)

    decision = session.download_finished("cached-song.mp3")

    assert decision.deferred is True
    assert decision.notify_cache_now is True
    assert session.local_path == "cached-song.mp3"
    assert session.local_is_temp is False

    request = session.set_local_switch_deferred(False)

    assert request is not None
    assert request.local_path == "cached-song.mp3"
    assert request.local_is_temp is False
    assert request.notify_cache is False


def test_temporary_download_switches_without_cache_notification() -> None:
    session = MediaPlaybackSession()
    session.begin_playback("https://cdn.example/song.mp3")
    session.set_stream_persist(False)

    decision = session.download_finished("Solin_stream_song.mp3")

    assert decision.notify_cache_now is False
    assert decision.switch_request is not None
    assert decision.switch_request.local_is_temp is True
    assert session.current_temp_path() == "Solin_stream_song.mp3"
    assert not session.should_notify_cache(requested=True)


def test_stop_invalidates_frames_and_clears_paths_after_cleanup_window() -> None:
    session = MediaPlaybackSession()
    session.begin_playback("https://cdn.example/video.mp4")
    session.mark_local_switch("Solin_stream_video.mp4", True)

    session.begin_stop()

    assert not session.accepts_frame()
    assert session.current_temp_path() == "Solin_stream_video.mp4"

    session.finish_stop()

    assert session.current_url == ""
    assert session.local_path is None
    assert session.current_temp_path() is None


def test_media_playback_session_has_no_qt_or_downloader_dependency() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "solin"
        / "core"
        / "media"
        / "playback_session.py"
    ).read_text(encoding="utf-8")

    assert "PySide6" not in source
    assert "QMediaPlayer" not in source
    assert "QObject" not in source
    assert "SongDownloader" not in source
