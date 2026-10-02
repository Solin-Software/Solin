"""Tests for libobs-routed media title/cover extraction (ffprobe-backed)."""
from __future__ import annotations

import solin.core.media.routed_metadata as routed_metadata
from PySide6.QtCore import QCoreApplication

from solin.core.media.routed_metadata import RoutedMediaMetadataExtractor


def _app():
    return QCoreApplication.instance() or QCoreApplication([])


# These fakes stand in for QImage/QPixmap so the delivery logic is exercised
# without a real native image decode. That decode is Qt's responsibility, and
# QImageReader is known to segfault when a real image is decoded very late in a
# large single-process suite (pre-existing environment fragility, unrelated to
# this code); mocking it keeps the unit test isolated to the extractor's logic.
class _FakeImage:
    def __init__(self, null: bool) -> None:
        self._null = null

    def isNull(self) -> bool:
        return self._null


class _FakePixmap:
    def __init__(self) -> None:
        self.from_image = None

    def isNull(self) -> bool:
        return False


def _install_fake_decode(monkeypatch, *, valid: bool):
    class FakeQImage:
        @staticmethod
        def fromData(_data) -> _FakeImage:
            return _FakeImage(null=not valid)

    class FakeQPixmap:
        @staticmethod
        def fromImage(image) -> _FakePixmap:
            px = _FakePixmap()
            px.from_image = image
            return px

    monkeypatch.setattr(routed_metadata, "QImage", FakeQImage)
    monkeypatch.setattr(routed_metadata, "QPixmap", FakeQPixmap)


def _extractor(metadata_fn):
    """An extractor with a synchronous runner and injected metadata function."""
    _app()
    pending: list = []
    extractor = RoutedMediaMetadataExtractor(
        metadata_fn=metadata_fn,
        runner=lambda work: pending.append(work),
    )
    results: list = []
    extractor.metadata_ready.connect(
        lambda sid, title, cover: results.append((sid, title, cover))
    )
    return extractor, pending, results


def test_title_and_cover_bytes_emit_a_pixmap(monkeypatch):
    _install_fake_decode(monkeypatch, valid=True)
    extractor, pending, results = _extractor(lambda path: ("Song", b"cover-bytes"))
    extractor.request(7, "/a.mp3")
    pending.pop(0)()  # run the worker synchronously
    assert len(results) == 1
    sid, title, px = results[0]
    assert sid == 7 and title == "Song"
    assert isinstance(px, _FakePixmap) and not px.isNull()


def test_title_only_yields_no_cover():
    extractor, pending, results = _extractor(lambda path: ("T", None))
    extractor.request(3, "/a.mp3")
    pending.pop(0)()
    assert results == [(3, "T", None)]


def test_invalid_cover_bytes_yield_no_cover(monkeypatch):
    _install_fake_decode(monkeypatch, valid=False)  # decoded image is null
    extractor, pending, results = _extractor(lambda path: ("T", b"not-an-image"))
    extractor.request(4, "/a.mp3")
    pending.pop(0)()
    sid, title, px = results[0]
    assert sid == 4 and title == "T" and px is None


def test_empty_path_emits_immediately_without_running():
    extractor, pending, results = _extractor(lambda path: ("x", None))
    extractor.request(1, "")
    assert results == [(1, "", None)]
    assert pending == []  # no worker scheduled


def test_request_supersedes_previous():
    calls: list[str] = []

    def metadata_fn(path):
        calls.append(path)
        return (path, None)

    extractor, pending, results = _extractor(metadata_fn)
    extractor.request(1, "/a.mp3")
    extractor.request(2, "/b.mp3")
    # Running the first (superseded) worker must not emit — its generation is stale.
    pending.pop(0)()
    assert results == []
    pending.pop(0)()
    assert results == [(2, "/b.mp3", None)]


def test_cancel_prevents_emit():
    extractor, pending, results = _extractor(lambda path: ("Song", b"cover-bytes"))
    extractor.request(5, "/a.mp3")
    extractor.cancel()
    pending.pop(0)()  # the worker still runs, but its result is dropped
    assert results == []


def test_extraction_error_emits_empty():
    def boom(path):
        raise RuntimeError("ffprobe blew up")

    extractor, pending, results = _extractor(boom)
    extractor.request(9, "/a.mp3")
    pending.pop(0)()
    assert results == [(9, "", None)]
