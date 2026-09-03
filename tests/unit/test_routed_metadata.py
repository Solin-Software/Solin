"""Tests for engine-routed media title/cover extraction."""
from __future__ import annotations

from PySide6.QtCore import QCoreApplication, QObject, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtMultimedia import QMediaMetaData, QMediaPlayer

from solin.core.media.routed_metadata import (
    RoutedMediaMetadataExtractor,
    read_playback_metadata,
)


def _app():
    return QCoreApplication.instance() or QCoreApplication([])


def _image() -> QImage:
    img = QImage(4, 4, QImage.Format.Format_RGB32)
    img.fill(0xFF00FF00)
    return img


class _FakeMeta:
    def __init__(self, values: dict) -> None:
        self._values = values

    def value(self, key):
        return self._values.get(key)


# ── read_playback_metadata (pure) ─────────────────────────────────────────────


def test_reads_title_and_cover_image():
    _app()
    meta = _FakeMeta({
        QMediaMetaData.Key.Title: "  Song  ",
        QMediaMetaData.Key.CoverArtImage: _image(),
    })
    title, cover = read_playback_metadata(meta)
    assert title == "Song"
    assert isinstance(cover, QPixmap) and not cover.isNull()


def test_title_only_yields_no_cover():
    _app()
    title, cover = read_playback_metadata(_FakeMeta({QMediaMetaData.Key.Title: "T"}))
    assert title == "T" and cover is None


def test_blank_title_is_empty():
    _app()
    title, cover = read_playback_metadata(_FakeMeta({QMediaMetaData.Key.Title: "   "}))
    assert title == "" and cover is None


def test_cover_pixmap_is_passed_through():
    _app()
    px = QPixmap.fromImage(_image())
    title, cover = read_playback_metadata(_FakeMeta({QMediaMetaData.Key.CoverArtImage: px}))
    assert title == "" and cover is px


def test_thumbnail_used_when_no_cover_art():
    _app()
    meta = _FakeMeta({QMediaMetaData.Key.ThumbnailImage: _image()})
    _title, cover = read_playback_metadata(meta)
    assert isinstance(cover, QPixmap) and not cover.isNull()


def test_null_cover_image_is_ignored():
    _app()
    meta = _FakeMeta({QMediaMetaData.Key.CoverArtImage: QImage()})  # null image
    _title, cover = read_playback_metadata(meta)
    assert cover is None


# ── RoutedMediaMetadataExtractor (fake player) ────────────────────────────────


class _FakePlayer(QObject):
    metaDataChanged = Signal()
    mediaStatusChanged = Signal(object)
    errorOccurred = Signal(object, str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.meta = _FakeMeta({})
        self.sources: list = []
        self.stopped = 0

    def setSource(self, url):
        self.sources.append(url)

    def stop(self):
        self.stopped += 1

    def metaData(self):
        return self.meta


def _extractor():
    _app()
    players: list[_FakePlayer] = []

    def factory(parent):
        player = _FakePlayer(parent)
        players.append(player)
        return player

    extractor = RoutedMediaMetadataExtractor(player_factory=factory)
    results: list = []
    extractor.metadata_ready.connect(lambda sid, title, cover: results.append((sid, title, cover)))
    return extractor, players, results


def test_cover_at_loaded_emits_immediately():
    extractor, players, results = _extractor()
    extractor.request(7, "/a.mp3")
    players[-1].meta = _FakeMeta({
        QMediaMetaData.Key.Title: "Song",
        QMediaMetaData.Key.CoverArtImage: _image(),
    })
    players[-1].mediaStatusChanged.emit(QMediaPlayer.MediaStatus.LoadedMedia)
    assert len(results) == 1
    sid, title, cover = results[0]
    assert sid == 7 and title == "Song"
    assert isinstance(cover, QPixmap) and not cover.isNull()


def test_no_cover_waits_then_emits_none_on_grace():
    extractor, players, results = _extractor()
    extractor.request(3, "/a.mp3")
    players[-1].meta = _FakeMeta({QMediaMetaData.Key.Title: "Song"})
    players[-1].mediaStatusChanged.emit(QMediaPlayer.MediaStatus.LoadedMedia)
    assert results == []  # holding for a late cover
    extractor._finish()  # simulate the grace-timer expiry
    assert results == [(3, "Song", None)]


def test_late_cover_via_metadata_changed_settles():
    extractor, players, results = _extractor()
    extractor.request(9, "/a.mp3")
    player = players[-1]
    player.meta = _FakeMeta({QMediaMetaData.Key.Title: "Song"})
    player.mediaStatusChanged.emit(QMediaPlayer.MediaStatus.LoadedMedia)
    assert results == []
    player.meta = _FakeMeta({
        QMediaMetaData.Key.Title: "Song",
        QMediaMetaData.Key.CoverArtImage: _image(),
    })
    player.metaDataChanged.emit()
    assert len(results) == 1 and results[0][0] == 9
    assert isinstance(results[0][2], QPixmap) and not results[0][2].isNull()


def test_invalid_media_emits_empty():
    extractor, players, results = _extractor()
    extractor.request(1, "/bad.mp3")
    players[-1].mediaStatusChanged.emit(QMediaPlayer.MediaStatus.InvalidMedia)
    assert results == [(1, "", None)]


def test_error_emits():
    extractor, players, results = _extractor()
    extractor.request(2, "/bad.mp3")
    players[-1].errorOccurred.emit(QMediaPlayer.Error.ResourceError, "boom")
    assert results == [(2, "", None)]


def test_request_supersedes_previous():
    extractor, players, results = _extractor()
    extractor.request(1, "/a.mp3")
    extractor.request(2, "/b.mp3")
    # Late signals on the superseded player must never touch the new session — not
    # a stale LoadedMedia, and (the review's finding) not a stale error/InvalidMedia
    # that would otherwise read the not-yet-loaded new player and emit empty tags.
    players[0].mediaStatusChanged.emit(QMediaPlayer.MediaStatus.LoadedMedia)
    players[0].mediaStatusChanged.emit(QMediaPlayer.MediaStatus.InvalidMedia)
    players[0].errorOccurred.emit(QMediaPlayer.Error.ResourceError, "stale")
    assert results == []
    players[1].meta = _FakeMeta({QMediaMetaData.Key.Title: "B"})
    players[1].mediaStatusChanged.emit(QMediaPlayer.MediaStatus.LoadedMedia)
    extractor._finish()
    assert results == [(2, "B", None)]


def test_cancel_prevents_emit():
    extractor, players, results = _extractor()
    extractor.request(5, "/a.mp3")
    extractor.cancel()
    players[-1].mediaStatusChanged.emit(QMediaPlayer.MediaStatus.LoadedMedia)
    assert results == []
