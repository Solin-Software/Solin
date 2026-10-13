"""Qt delivery and cancellation for the domain media extraction boundary."""

from __future__ import annotations

import gc
import threading
import weakref

import pytest
import shiboken6
from PySide6.QtCore import QCoreApplication, QObject, QThread
from PySide6.QtGui import QPixmap

import solin.core.media.routed_metadata as routed_metadata
from solin.core.media.info_queue import MediaInfoFailure, MediaInfoFailureKind
from solin.core.media.media_extract import (
    ImagePolicy,
    MediaExtractCancelled,
    MediaExtractError,
    MediaExtractImage,
    MediaExtractResult,
)
from solin.core.media.routed_metadata import RoutedMediaMetadataExtractor


def _cover(*, kind="cover"):
    return MediaExtractImage(b"\x33\x66\xcc\xff" * 4, 2, 2, 8, kind)


@pytest.fixture(autouse=True)
def extraction_service(monkeypatch):
    """Keep every default routed request away from native processes."""
    calls = []

    class Service:
        def extract(self, request, *, cancel):
            calls.append((request, cancel))
            return MediaExtractResult(title="Default title", image=_cover())

    monkeypatch.setattr(routed_metadata, "get_media_extract_service", Service)
    return calls


@pytest.fixture
def make_extractor(qt_object_owner):
    def create(metadata_fn=None):
        pending = []
        extractor = RoutedMediaMetadataExtractor(
            qt_object_owner,
            metadata_fn=metadata_fn,
            runner=pending.append,
        )
        results = []
        extractor.metadata_ready.connect(
            lambda sid, title, cover: results.append((sid, title, cover))
        )
        return extractor, pending, results

    return create


def _run_in_thread(work):
    thread = threading.Thread(target=work)
    thread.start()
    thread.join(timeout=2)
    assert not thread.is_alive()


def test_default_service_gets_cover_only_title_without_duration(
    make_extractor,
    extraction_service,
):
    extractor, pending, results = make_extractor()
    path = "canção d'água – apresentação.mp3"
    extractor.request(7, path)
    pending.pop()()

    request, cancel = extraction_service[0]
    assert request.source == path
    assert request.image_policy is ImagePolicy.COVER_ONLY
    assert request.require_title is True
    assert request.require_duration is False
    assert (request.width, request.height) == (640, 360)
    assert not cancel.is_set()
    sid, title, cover = results[0]
    assert (sid, title) == (7, "Default title")
    assert isinstance(cover, QPixmap)
    assert cover.toImage().pixelColor(0, 0).getRgb() == (204, 102, 51, 255)


def test_title_only_yields_no_cover(make_extractor):
    extractor, pending, results = make_extractor(
        lambda request, *, cancel: MediaExtractResult(title="T"),
    )
    extractor.request(3, "/a.mp3")
    pending.pop()()
    assert results == [(3, "T", None)]


def test_invalid_cover_keeps_title(make_extractor, monkeypatch):
    def invalid(_image):
        raise ValueError("Invalid BGRA buffer")

    monkeypatch.setattr(routed_metadata, "media_extract_image_to_qimage", invalid)
    extractor, pending, results = make_extractor(
        lambda request, *, cancel: MediaExtractResult(title="T", image=_cover()),
    )
    extractor.request(4, "/a.mp3")
    pending.pop()()
    assert results == [(4, "T", None)]


def test_video_frame_is_not_used_as_cover(make_extractor):
    extractor, pending, results = make_extractor(
        lambda request, *, cancel: MediaExtractResult(title="Video", image=_cover(kind="frame")),
    )
    extractor.request(4, "/a.mp4")
    pending.pop()()
    assert results == [(4, "Video", None)]


@pytest.mark.parametrize("path", ["", "bad\0source"])
def test_empty_or_invalid_path_emits_without_scheduling(make_extractor, path):
    extractor, pending, results = make_extractor()
    extractor.request(1, path)
    assert results == [(1, "", None)]
    assert pending == []


@pytest.mark.parametrize("replacement", ["/b.mp3", ""])
def test_new_request_cancels_previous_queued_work(make_extractor, replacement):
    calls = []

    def metadata(request, *, cancel):
        calls.append(request.source)
        return MediaExtractResult(title=request.source)

    extractor, pending, results = make_extractor(metadata)
    extractor.request(1, "/a.mp3")
    previous_cancel = extractor._lifetime.cancellation
    extractor.request(2, replacement)
    assert previous_cancel.is_set()
    pending.pop(0)()
    assert calls == []
    if replacement:
        assert results == []
        assert extractor._lifetime.cancellation is not previous_cancel
        pending.pop(0)()
        assert calls == [replacement]
    assert results == [(2, replacement, None)]


def test_cancel_prevents_queued_extraction(make_extractor, extraction_service):
    extractor, pending, results = make_extractor()
    extractor.request(5, "/a.mp3")
    cancel = extractor._lifetime.cancellation
    extractor.cancel()
    pending.pop()()
    assert cancel.is_set()
    assert extraction_service == []
    assert results == []


@pytest.mark.parametrize("action", ["cancel", "replace"])
def test_active_extraction_gets_cancellation_and_cannot_deliver(make_extractor, action):
    started = threading.Event()
    release = threading.Event()
    cancellations = []

    def metadata(request, *, cancel):
        cancellations.append(cancel)
        started.set()
        assert release.wait(2)
        # A backend that finishes despite cancellation must still be suppressed.
        return MediaExtractResult(title="Late")

    extractor, pending, results = make_extractor(metadata)
    extractor.request(5, "/a.mp3")
    worker = threading.Thread(target=pending.pop())
    worker.start()
    try:
        assert started.wait(1)
        if action == "replace":
            extractor.request(5, "/b.mp3")
        else:
            extractor.cancel()
        assert cancellations[0].is_set()
    finally:
        release.set()
        worker.join(timeout=2)
    assert not worker.is_alive()
    QCoreApplication.processEvents()
    assert results == []
    if action == "replace":
        pending.pop()()
        assert results == [(5, "Late", None)]
        assert cancellations[1] is not cancellations[0]
        assert not cancellations[1].is_set()


@pytest.mark.parametrize("action", ["cancel", "replace"])
def test_queued_result_is_dropped_before_image_conversion(make_extractor, monkeypatch, action):
    extractor, pending, results = make_extractor()
    extractor.request(5, "/a.mp3")
    _run_in_thread(pending.pop())
    assert results == []
    if action == "replace":
        # Same session ID deliberately: only the request generation changed.
        extractor.request(5, "/b.mp3")
    else:
        extractor.cancel()

    def unexpected_conversion(_image):
        pytest.fail("Stale result reached Qt image conversion")

    monkeypatch.setattr(routed_metadata, "media_extract_image_to_qimage", unexpected_conversion)
    QCoreApplication.processEvents()
    assert results == []


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("Unexpected extraction failure"),
        MediaExtractError(MediaInfoFailure(MediaInfoFailureKind.PERMANENT, "Bad file", "format")),
    ],
)
def test_extraction_error_emits_empty(make_extractor, error):
    def metadata(request, *, cancel):
        raise error

    extractor, pending, results = make_extractor(metadata)
    extractor.request(9, "/a.mp3")
    pending.pop()()
    assert results == [(9, "", None)]


def test_native_cancellation_does_not_emit_empty_metadata(make_extractor):
    def metadata(request, *, cancel):
        raise MediaExtractCancelled()

    extractor, pending, results = make_extractor(metadata)
    extractor.request(9, "/a.mp3")
    pending.pop()()
    assert results == []


def test_pixmap_creation_and_delivery_run_on_qt_thread(make_extractor, monkeypatch):
    worker_threads = []
    image_threads = []
    pixmap_threads = []
    delivery_threads = []
    convert = routed_metadata.media_extract_image_to_qimage

    def metadata(request, *, cancel):
        worker_threads.append(QThread.isMainThread())
        return MediaExtractResult(title="Song", image=_cover())

    def convert_on_qt(image):
        image_threads.append(QThread.isMainThread())
        return convert(image)

    class Pixmaps:
        @staticmethod
        def fromImage(image):
            pixmap_threads.append(QThread.isMainThread())
            return QPixmap.fromImage(image)

    monkeypatch.setattr(routed_metadata, "media_extract_image_to_qimage", convert_on_qt)
    monkeypatch.setattr(routed_metadata, "QPixmap", Pixmaps)
    extractor, pending, results = make_extractor(metadata)
    extractor.metadata_ready.connect(lambda *_: delivery_threads.append(QThread.isMainThread()))
    extractor.request(7, "/a.mp3")
    _run_in_thread(pending.pop())
    assert results == []
    assert image_threads == pixmap_threads == []
    QCoreApplication.processEvents()
    assert worker_threads == [False]
    assert image_threads == pixmap_threads == delivery_threads == [True]
    assert results[0][:2] == (7, "Song")


def test_parent_destruction_cancels_queued_work(qt_object_owner, extraction_service):
    parent = QObject(qt_object_owner)
    pending = []
    extractor = RoutedMediaMetadataExtractor(parent, runner=pending.append)
    results = []
    extractor.metadata_ready.connect(lambda *args: results.append(args))
    extractor.request(7, "/a.mp3")
    cancel = extractor._lifetime.cancellation
    shiboken6.delete(parent)
    assert cancel.is_set()
    assert not shiboken6.isValid(extractor)
    pending.pop()()
    assert results == extraction_service == []


def test_queued_worker_does_not_keep_unparented_owner_alive(extraction_service):
    pending = []
    extractor = RoutedMediaMetadataExtractor(runner=pending.append)
    extractor.request(7, "/a.mp3")
    cancel = extractor._lifetime.cancellation
    owner_ref = weakref.ref(extractor)
    del extractor
    gc.collect()
    assert owner_ref() is None
    assert cancel.is_set()
    pending.pop()()
    assert extraction_service == []


def test_parent_destruction_cancels_active_work_and_stops_owned_pool(qt_object_owner):
    parent = QObject(qt_object_owner)
    started = threading.Event()
    release = threading.Event()
    cancellations = []

    def metadata(request, *, cancel):
        cancellations.append(cancel)
        started.set()
        assert release.wait(2)
        return MediaExtractResult(title="Late")

    extractor = RoutedMediaMetadataExtractor(parent, metadata_fn=metadata)
    pool = extractor._lifetime.workers
    results = []
    extractor.metadata_ready.connect(lambda *args: results.append(args))
    extractor.request(7, "/a.mp3")
    try:
        assert started.wait(1)
        assert pool.active_count == 1
        shiboken6.delete(parent)
        assert cancellations[0].is_set()
        assert pool.is_stopped
    finally:
        release.set()
        assert pool.shutdown(timeout=2) == ()
    assert pool.active_count == 0
    QCoreApplication.processEvents()
    assert results == []
