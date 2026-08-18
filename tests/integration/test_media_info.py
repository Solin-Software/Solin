from __future__ import annotations

import json
from pathlib import Path
import struct
import threading
import time
from types import SimpleNamespace
import zlib

import pytest
from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QApplication

from solin.core.media.info_queue import MediaInfoFailure, MediaInfoFailureKind
from solin.core.foundation.thread_workers import ThreadedWorkerPool
from solin.ui import media_info as media_info_module
from solin.ui.media_info import (
    MediaInfoQueue,
    MediaInfoService,
    _audio_info_from_file,
    _embedded_image_is_complete,
    _id3v2_info_from_bytes,
)


_APP = QApplication.instance() or QApplication([])


class _NoRemoteWorkerPool:
    def submit(self, name, _target):
        if name.startswith("media-info-cache-"):
            return None
        raise AssertionError("this test should not start remote workers")


def _media_info_queue(tmp_path, parent=None):
    return MediaInfoQueue(
        tmp_path / "media",
        tmp_path / "thumbs",
        _NoRemoteWorkerPool(),
        parent,
    )


def _wait_until(predicate, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        QApplication.processEvents()
        if time.monotonic() >= deadline:
            raise AssertionError("Timed out waiting for media info work")
        time.sleep(0.002)
    QApplication.processEvents()


class _ManualSignal:
    def __init__(self) -> None:
        self._callbacks = []

    def connect(self, callback) -> None:
        self._callbacks.append(callback)

    def emit(self, *args) -> None:
        for callback in tuple(self._callbacks):
            callback(*args)


class _ManualExtractor:
    def __init__(self) -> None:
        self.info_ready = _ManualSignal()
        self.thumbnail_failed = _ManualSignal()
        self.duration_ready = _ManualSignal()

    def cancel(self) -> None:
        pass


class _NullPixmap:
    @staticmethod
    def isNull():  # noqa: N802 - Qt-style test double
        return True


def test_http_status_failure_classification_handles_permanent_and_cloud_errors():
    missing = media_info_module._failure_from_exception(
        media_info_module.HttpStatusError("https://example/missing", 404, "missing")
    )
    unavailable = media_info_module._failure_from_exception(
        media_info_module.HttpStatusError(
            "https://example/unavailable",
            503,
            "unavailable",
        )
    )
    locked = media_info_module._failure_from_exception(
        media_info_module.HttpStatusError("https://example/locked", 423, "locked")
    )
    conflict = media_info_module._failure_from_exception(
        media_info_module.HttpStatusError("https://example/conflict", 409, "conflict")
    )

    assert missing.kind is MediaInfoFailureKind.PERMANENT
    assert missing.code == "http-404"
    assert unavailable.kind is MediaInfoFailureKind.TRANSIENT
    assert locked.kind is MediaInfoFailureKind.TRANSIENT
    assert conflict.kind is MediaInfoFailureKind.TRANSIENT


def test_local_image_access_failure_is_transient_and_worker_routed(tmp_path):
    source = SimpleNamespace(
        _url=str(tmp_path / "cloud-placeholder.jpg"),
        _MAX_BYTES=media_info_module.LocalImageInfoExtractor._MAX_BYTES,
    )

    with pytest.raises(OSError) as captured:
        media_info_module.LocalImageInfoExtractor._fetch_info(source)

    failure = media_info_module._failure_from_exception(captured.value)
    assert failure.kind is MediaInfoFailureKind.TRANSIENT
    assert media_info_module._EXTRACTOR_FACTORIES[(False, "image")] is (
        media_info_module._local_image_factory
    )


def test_remote_audio_invalid_embedded_image_is_failure_not_absence(monkeypatch):
    class _DecodeFailurePixmap:
        @staticmethod
        def loadFromData(_data):  # noqa: N802 - Qt-style test double
            return False

        @staticmethod
        def isNull():  # noqa: N802 - Qt-style test double
            return True

    monkeypatch.setattr(media_info_module, "QPixmap", _DecodeFailurePixmap)
    extractor = media_info_module.RemoteAudioInfoExtractor(
        9,
        "https://example/audio.mp3",
        _NoRemoteWorkerPool(),
    )
    ready = []
    failed = []
    extractor.info_ready.connect(lambda *args: ready.append(args))
    extractor.thumbnail_failed.connect(lambda *args: failed.append(args))

    extractor._deliver_worker_result(b"not-a-decodable-image", "")

    assert ready == []
    assert len(failed) == 1
    assert failed[0][0] == 9
    assert failed[0][1].kind is MediaInfoFailureKind.FORMAT
    extractor.cancel()


def test_remote_title_only_result_does_not_start_qmedia_stream(monkeypatch):
    scheduled = []
    monkeypatch.setattr(
        media_info_module,
        "QTimer",
        SimpleNamespace(singleShot=lambda delay, callback: scheduled.append((delay, callback))),
    )
    ready = []
    streams = []
    source = SimpleNamespace(
        _done=False,
        _require_duration=False,
        _require_thumbnail=False,
        _page_title="",
        _page_pixmap=_NullPixmap(),
        info_ready=SimpleNamespace(emit=lambda *args: ready.append(args)),
        deleteLater=lambda: None,
        _start_stream=streams.append,
    )

    media_info_module._RemoteVideoMetaThenStream._on_page_ready(
        source,
        9,
        _NullPixmap(),
        "Resolved title",
    )

    assert ready == [(9, source._page_pixmap, "Resolved title")]
    assert streams == []
    assert len(scheduled) == 1


def test_remote_title_only_failure_retries_without_qmedia_stream(monkeypatch):
    scheduled = []
    monkeypatch.setattr(
        media_info_module,
        "QTimer",
        SimpleNamespace(singleShot=lambda delay, callback: scheduled.append((delay, callback))),
    )
    failures = []
    streams = []
    source = SimpleNamespace(
        _done=False,
        _require_duration=False,
        _require_thumbnail=False,
        thumbnail_failed=SimpleNamespace(emit=lambda *args: failures.append(args)),
        deleteLater=lambda: None,
        _start_stream=streams.append,
    )
    failure = MediaInfoFailure(
        MediaInfoFailureKind.TRANSIENT,
        "temporarily unavailable",
        "transport",
    )

    media_info_module._RemoteVideoMetaThenStream._on_page_failed(
        source,
        9,
        failure,
    )

    assert failures == [(9, failure)]
    assert streams == []
    assert len(scheduled) == 1


def test_local_audio_invalid_embedded_image_is_not_reported_as_absent(monkeypatch):
    class _DecodeFailurePixmap:
        @staticmethod
        def loadFromData(_data):  # noqa: N802 - Qt-style test double
            return False

        @staticmethod
        def isNull():  # noqa: N802 - Qt-style test double
            return True

    monkeypatch.setattr(media_info_module, "QPixmap", _DecodeFailurePixmap)
    extractor = media_info_module.LocalAudioInfoExtractor(
        10,
        "cloud-backed.mp3",
        _NoRemoteWorkerPool(),
    )
    ready = []
    failed = []
    extractor.info_ready.connect(lambda *args: ready.append(args))
    extractor.thumbnail_failed.connect(lambda *args: failed.append(args))

    extractor._deliver_worker_result(b"not-a-decodable-image", "")

    assert ready == []
    assert len(failed) == 1
    assert failed[0][1].kind is MediaInfoFailureKind.FORMAT
    assert media_info_module._EXTRACTOR_FACTORIES[(False, "audio")] is (
        media_info_module._local_audio_factory
    )
    extractor.cancel()


def test_local_audio_metadata_read_is_deferred_to_worker(monkeypatch, tmp_path):
    extractor = _ManualExtractor()
    monkeypatch.setattr(
        media_info_module,
        "_read_audio_info_from_file",
        lambda _path: pytest.fail("audio was read on the Qt thread"),
    )
    monkeypatch.setattr(
        media_info_module,
        "_create_extractor",
        lambda *_args: extractor,
    )
    queue = _media_info_queue(tmp_path)

    queue.request(10, str(tmp_path / "cloud-backed.mp3"), "audio")

    assert queue._extractors[10] is extractor
    queue.shutdown()


def test_local_audio_without_cover_finishes_as_authoritative_absence():
    finished = []
    failed = []
    source = SimpleNamespace(
        _done=False,
        _require_thumbnail=True,
        _metadata_pixmap=media_info_module.QPixmap(),
        _metadata_title="",
        _metadata_image_failed=False,
        _finish=lambda *args: finished.append(args),
        _on_player_failed=lambda *args: failed.append(args),
    )

    media_info_module._LocalAudioMetaThenPlayer._on_player_ready(
        source,
        10,
        media_info_module.QPixmap(),
        "",
    )

    assert len(finished) == 1
    assert finished[0][0] == 10
    assert finished[0][1].isNull()
    assert failed == []


def test_legacy_negative_cache_is_not_treated_as_authoritative_absence(tmp_path):
    queue = _media_info_queue(tmp_path)
    _image_path, metadata_path = media_info_module._media_info_cache_paths(
        queue._thumb_cache_dir,
        "clip.mp4",
    )
    Path(metadata_path).parent.mkdir(parents=True)
    Path(metadata_path).write_text(
        json.dumps({"has_thumb": False, "title": "", "duration_ms": 0}),
        encoding="utf-8",
    )

    result = media_info_module._load_media_info_disk_cache(
        queue._thumb_cache_dir,
        "clip.mp4",
        load_thumbnail=True,
    )

    assert not result.hit


def test_disk_cache_lookup_and_decode_run_outside_qt_thread(monkeypatch, tmp_path):
    workers = ThreadedWorkerPool()
    queue = MediaInfoQueue(tmp_path / "media", tmp_path / "thumbs", workers)
    gui_thread = threading.get_ident()
    lookup_threads = []
    ready = []

    def load_cache(_cache_dir, _url, *, load_thumbnail):
        lookup_threads.append((threading.get_ident(), load_thumbnail))
        image = media_info_module.QImage(
            2,
            2,
            media_info_module.QImage.Format.Format_RGB32,
        )
        image.fill(0xFF223344)
        return media_info_module._DiskCacheResult(
            True,
            image,
            'Episódio 2: "Este é meu Filho"',
            True,
            12_345,
        )

    monkeypatch.setattr(media_info_module, "_load_media_info_disk_cache", load_cache)
    monkeypatch.setattr(
        media_info_module,
        "_create_extractor",
        lambda *_args: pytest.fail("cache hit unexpectedly started extraction"),
    )
    queue.info_ready.connect(lambda *args: ready.append(args))

    queue.request(
        7,
        str(tmp_path / "gnj_T_02_r720P.mp4"),
        require_title=True,
        require_duration=True,
    )
    _wait_until(lambda: bool(ready))

    assert len(lookup_threads) == 1
    assert lookup_threads[0][0] != gui_thread
    assert lookup_threads[0][1] is True
    assert ready[0][2] == 'Episódio 2: "Este é meu Filho"'
    assert queue._duration_cache[7] == 12_345
    queue.shutdown()
    workers.shutdown()


def test_local_content_change_invalidates_media_info_disk_cache(tmp_path):
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"old")
    image = media_info_module.QImage(
        2,
        2,
        media_info_module.QImage.Format.Format_RGB32,
    )
    image.fill(0xFF223344)
    cache_dir = str(tmp_path / "thumbs")
    media_info_module._save_media_info_disk_cache(
        cache_dir,
        str(source),
        image,
        "Old title",
        title_resolved=True,
        duration_ms=1_000,
    )

    before = media_info_module._load_media_info_disk_cache(
        cache_dir,
        str(source),
        load_thumbnail=True,
    )
    source.write_bytes(b"new-content")
    after = media_info_module._load_media_info_disk_cache(
        cache_dir,
        str(source),
        load_thumbnail=True,
    )

    assert before.hit
    assert before.title == "Old title"
    assert not after.hit


def test_inflight_result_is_not_cached_under_a_new_local_revision(tmp_path):
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"old")
    source_identity = media_info_module._media_info_cache_source_identity(
        str(source)
    )
    old_image_path, old_metadata_path = media_info_module._media_info_cache_paths(
        str(tmp_path / "thumbs"),
        str(source),
        source_identity=source_identity,
    )
    image = media_info_module.QImage(
        2,
        2,
        media_info_module.QImage.Format.Format_RGB32,
    )
    image.fill(0xFF223344)

    source.write_bytes(b"new-content")
    saved = media_info_module._save_media_info_disk_cache(
        str(tmp_path / "thumbs"),
        str(source),
        image,
        "Old title",
        title_resolved=True,
        duration_ms=1_000,
        expected_source_identity=source_identity,
    )

    assert saved is False
    assert not Path(old_image_path).exists()
    assert not Path(old_metadata_path).exists()


def test_local_result_is_rejected_when_source_changes_during_extraction(
    monkeypatch,
    tmp_path,
):
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"old-content")
    extractors: list[_ManualExtractor] = []

    def factory(*_args):
        extractor = _ManualExtractor()
        extractors.append(extractor)
        return extractor

    monkeypatch.setattr(media_info_module, "_create_extractor", factory)
    workers = ThreadedWorkerPool()
    queue = MediaInfoQueue(tmp_path / "media", tmp_path / "thumbs", workers)
    ready = []
    durations = []
    failures = []
    queue.info_ready.connect(lambda *args: ready.append(args))
    queue.duration_ready.connect(lambda *args: durations.append(args))
    queue.request_failed.connect(lambda *args: failures.append(args))

    queue.request(
        7,
        str(source),
        require_duration=True,
        restart_on_source_change=False,
    )
    _wait_until(lambda: len(extractors) == 1)
    source.write_bytes(b"new-and-different-content")
    extractors[0].duration_ready.emit(7, 12_345)
    extractors[0].info_ready.emit(
        7,
        media_info_module.QPixmap(1, 1),
        "Stale title",
    )

    _wait_until(lambda: bool(failures))

    assert ready == []
    assert durations == []
    assert len(extractors) == 1
    assert failures[0][0] == 7
    assert failures[0][1].code == "source-changed"
    assert queue.get_cached(7) == (None, "")
    queue.shutdown()
    workers.shutdown()


def test_generic_consumer_restarts_when_source_changes_during_extraction(
    monkeypatch,
    tmp_path,
):
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"old-content")
    extractors: list[_ManualExtractor] = []

    def factory(*_args):
        extractor = _ManualExtractor()
        extractors.append(extractor)
        return extractor

    monkeypatch.setattr(media_info_module, "_create_extractor", factory)
    workers = ThreadedWorkerPool()
    queue = MediaInfoQueue(tmp_path / "media", tmp_path / "thumbs", workers)
    failures = []
    queue.request_failed.connect(lambda *args: failures.append(args))

    queue.request(7, str(source))
    _wait_until(lambda: len(extractors) == 1)
    source.write_bytes(b"new-and-different-content")
    extractors[0].info_ready.emit(
        7,
        media_info_module.QPixmap(1, 1),
        "Stale title",
    )

    _wait_until(lambda: len(extractors) == 2)

    assert failures == []
    assert queue.get_cached(7) == (None, "")
    queue.shutdown()
    workers.shutdown()


def test_generic_source_change_retry_is_bounded_and_backed_off(
    monkeypatch,
    tmp_path,
):
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"0")
    extractors: list[_ManualExtractor] = []

    def factory(*_args):
        extractor = _ManualExtractor()
        extractors.append(extractor)
        return extractor

    monkeypatch.setattr(media_info_module, "_create_extractor", factory)
    monkeypatch.setattr(
        media_info_module,
        "_SOURCE_CHANGE_RETRY_DELAYS_MS",
        (1, 1, 1),
    )
    workers = ThreadedWorkerPool()
    queue = MediaInfoQueue(tmp_path / "media", tmp_path / "thumbs", workers)
    failures = []
    queue.request_failed.connect(lambda *args: failures.append(args))

    queue.request(7, str(source))
    for revision in range(1, 5):
        _wait_until(lambda expected=revision: len(extractors) >= expected)
        source.write_bytes(b"x" * (revision + 1))
        extractors[revision - 1].info_ready.emit(
            7,
            media_info_module.QPixmap(1, 1),
            f"Stale title {revision}",
        )
    _wait_until(lambda: bool(failures))

    assert len(extractors) == 4
    assert failures[0][1].code == "source-changed"
    assert 7 not in queue._request_states
    assert 7 not in queue._retry_timers
    queue.shutdown()
    workers.shutdown()


def test_validated_local_duration_survives_thumbnail_format_failure(
    monkeypatch,
    tmp_path,
):
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"stable-content")
    extractor = _ManualExtractor()
    monkeypatch.setattr(
        media_info_module,
        "_create_extractor",
        lambda *_args: extractor,
    )
    workers = ThreadedWorkerPool()
    queue = MediaInfoQueue(tmp_path / "media", tmp_path / "thumbs", workers)
    ready = []
    durations = []
    failures = []
    queue.info_ready.connect(lambda *args: ready.append(args))
    queue.duration_ready.connect(lambda *args: durations.append(args))
    queue.request_failed.connect(lambda *args: failures.append(args))

    queue.request(7, str(source), require_duration=True)
    _wait_until(lambda: queue._extractors.get(7) is extractor)
    extractor.duration_ready.emit(7, 12_345)
    extractor.info_ready.emit(7, media_info_module.QPixmap(), "Metadata title")
    _wait_until(lambda: bool(failures))

    assert ready == []
    assert durations == [(7, 12_345)]
    assert failures[0][1].kind is MediaInfoFailureKind.FORMAT
    queue.shutdown()
    workers.shutdown()


def test_permanent_failure_does_not_emit_ready_or_write_negative_cache(
    monkeypatch,
    tmp_path,
):
    extractor = _ManualExtractor()
    monkeypatch.setattr(
        media_info_module,
        "_create_extractor",
        lambda *_args: extractor,
    )
    queue = _media_info_queue(tmp_path)
    ready = []
    failed = []
    queue.info_ready.connect(lambda *args: ready.append(args))
    queue.request_failed.connect(lambda *args: failed.append(args))

    queue.request(7, "missing.mp4", "video")
    failure = MediaInfoFailure(
        MediaInfoFailureKind.PERMANENT,
        "not found",
        "missing",
    )
    extractor.thumbnail_failed.emit(7, failure)

    _image_path, metadata_path = media_info_module._media_info_cache_paths(
        queue._thumb_cache_dir,
        "missing.mp4",
    )
    assert ready == []
    assert failed == [(7, failure)]
    assert queue.get_cached(7) == (None, "")
    assert not Path(metadata_path).exists()


def test_empty_video_result_is_a_failure_not_authoritative_absence(
    monkeypatch,
    tmp_path,
):
    extractor = _ManualExtractor()
    monkeypatch.setattr(
        media_info_module,
        "_create_extractor",
        lambda *_args: extractor,
    )
    queue = _media_info_queue(tmp_path)
    ready = []
    failed = []
    queue.info_ready.connect(lambda *args: ready.append(args))
    queue.request_failed.connect(lambda *args: failed.append(args))

    queue.request(4, "clip.mp4", "video")
    extractor.info_ready.emit(4, _NullPixmap(), "Metadata title")

    assert ready == []
    assert len(failed) == 1
    assert failed[0][0] == 4
    assert failed[0][1].kind is MediaInfoFailureKind.FORMAT
    assert queue.get_cached(4) == (None, "")


def test_transient_failure_remains_pending_until_scheduled_retry(
    monkeypatch,
    tmp_path,
):
    created: list[_ManualExtractor] = []

    def factory(*_args):
        extractor = _ManualExtractor()
        created.append(extractor)
        return extractor

    monkeypatch.setattr(media_info_module, "_create_extractor", factory)
    monkeypatch.setattr(
        media_info_module,
        "retry_delay_seconds",
        lambda _failure, _attempt: 30.0,
    )
    queue = _media_info_queue(tmp_path)
    failed = []
    queue.request_failed.connect(lambda *args: failed.append(args))

    queue.request(5, "cloud-placeholder.mp4", "video")
    created[0].thumbnail_failed.emit(
        5,
        MediaInfoFailure(
            MediaInfoFailureKind.TRANSIENT,
            "temporarily locked",
            "locked",
        ),
    )

    assert failed == []
    assert 5 in queue._request_states
    assert 5 in queue._retry_timers
    assert queue.get_cached(5) == (None, "")


def test_remote_404_uses_completed_local_media_only_after_origin_failure(
    monkeypatch,
    tmp_path,
):
    remote_url = "https://cdn.example/missing.mp4"
    local_fallback = str(tmp_path / "media" / "cached.mp4")
    created: list[tuple[str, _ManualExtractor]] = []

    def factory(_index, target, _media_type, _worker_pool, _parent):
        extractor = _ManualExtractor()
        created.append((target, extractor))
        return extractor

    monkeypatch.setattr(media_info_module, "_create_extractor", factory)
    monkeypatch.setattr(
        media_info_module,
        "completed_cached_path",
        lambda url, _cache_dir: local_fallback if url == remote_url else None,
    )
    queue = _media_info_queue(tmp_path)

    queue.request(3, remote_url, "video")

    assert [target for target, _extractor in created] == [remote_url]

    origin_failure = MediaInfoFailure(
        MediaInfoFailureKind.PERMANENT,
        "not found",
        "http-404",
    )
    created[0][1].thumbnail_failed.emit(3, origin_failure)

    assert [target for target, _extractor in created] == [
        remote_url,
        local_fallback,
    ]
    assert queue._request_states[3].origin_failure is origin_failure


def test_remote_404_without_local_media_stops_without_retry(monkeypatch, tmp_path):
    extractor = _ManualExtractor()
    monkeypatch.setattr(
        media_info_module,
        "_create_extractor",
        lambda *_args: extractor,
    )
    monkeypatch.setattr(
        media_info_module,
        "completed_cached_path",
        lambda *_args: None,
    )
    queue = _media_info_queue(tmp_path)
    failures = []
    queue.request_failed.connect(lambda *args: failures.append(args))

    queue.request(9, "https://cdn.example/missing.mp4", "video")
    failure = MediaInfoFailure(
        MediaInfoFailureKind.PERMANENT,
        "not found",
        "http-404",
    )
    extractor.thumbnail_failed.emit(9, failure)

    assert failures == [(9, failure)]
    assert 9 not in queue._request_states
    assert 9 not in queue._retry_timers


def test_remote_404_is_memoized_without_becoming_thumbnail_absence(
    monkeypatch,
    tmp_path,
):
    created: list[_ManualExtractor] = []

    def factory(*_args):
        extractor = _ManualExtractor()
        created.append(extractor)
        return extractor

    monkeypatch.setattr(media_info_module, "_create_extractor", factory)
    monkeypatch.setattr(
        media_info_module,
        "completed_cached_path",
        lambda *_args: None,
    )
    queue = _media_info_queue(tmp_path)
    remote_url = "https://cdn.example/missing.mp4"

    queue.request(1, remote_url, "video")
    failure = MediaInfoFailure(
        MediaInfoFailureKind.PERMANENT,
        "not found",
        "http-404",
    )
    created[0].thumbnail_failed.emit(1, failure)
    queue.clear()
    queue.request(2, remote_url, "video")

    assert len(created) == 1
    assert queue._terminal_source_failures[remote_url] is failure
    _image_path, metadata_path = media_info_module._media_info_cache_paths(
        queue._thumb_cache_dir,
        remote_url,
    )
    assert not Path(metadata_path).exists()


def test_memoized_remote_404_uses_local_media_when_it_becomes_available(
    monkeypatch,
    tmp_path,
):
    remote_url = "https://cdn.example/missing.mp4"
    local_fallback = str(tmp_path / "media" / "cached.mp4")
    local_is_available = False
    created: list[tuple[str, _ManualExtractor]] = []

    def factory(_index, target, _media_type, _worker_pool, _parent):
        extractor = _ManualExtractor()
        created.append((target, extractor))
        return extractor

    def completed_path(_url, _cache_dir):
        return local_fallback if local_is_available else None

    monkeypatch.setattr(media_info_module, "_create_extractor", factory)
    monkeypatch.setattr(media_info_module, "completed_cached_path", completed_path)
    queue = _media_info_queue(tmp_path)

    queue.request(1, remote_url, "video")
    failure = MediaInfoFailure(
        MediaInfoFailureKind.PERMANENT,
        "not found",
        "http-404",
    )
    created[0][1].thumbnail_failed.emit(1, failure)
    local_is_available = True
    queue.request(2, remote_url, "video")

    assert [target for target, _extractor in created] == [
        remote_url,
        local_fallback,
    ]
    assert queue._request_states[2].origin_failure is failure


def _png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    checksum = zlib.crc32(chunk_type + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + chunk_type + data + struct.pack(">I", checksum)


def _large_valid_png() -> bytes:
    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    pixels = zlib.compress(b"\x00\x00\x00\x00")
    padding = b"cover\x00" + (b"x" * (600 * 1024))
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"tEXt", padding)
        + _png_chunk(b"IDAT", pixels)
        + _png_chunk(b"IEND", b"")
    )


def _syncsafe(value: int) -> bytes:
    return bytes(
        (
            (value >> 21) & 0x7F,
            (value >> 14) & 0x7F,
            (value >> 7) & 0x7F,
            value & 0x7F,
        )
    )


def _mp3_with_cover(cover: bytes) -> bytes:
    apic_data = b"\x00image/png\x00\x03\x00" + cover
    frame = b"APIC" + struct.pack(">I", len(apic_data)) + b"\x00\x00" + apic_data
    return b"ID3\x03\x00\x00" + _syncsafe(len(frame)) + frame + b"\xff\xfb"


def test_audio_info_reads_complete_id3_tag_larger_than_default_prefix(tmp_path):
    cover = _large_valid_png()
    path = tmp_path / "large-cover.mp3"
    path.write_bytes(_mp3_with_cover(cover))

    extracted_cover, _title = _audio_info_from_file(str(path))

    assert len(cover) > 512 * 1024
    assert extracted_cover == cover
    assert _embedded_image_is_complete(extracted_cover)


def test_truncated_id3_frame_does_not_expose_partial_cover():
    data = _mp3_with_cover(_large_valid_png())

    cover, _title = _id3v2_info_from_bytes(data[: 512 * 1024])

    assert cover is None


def test_truncated_png_is_rejected_before_native_decoder():
    cover = _large_valid_png()

    assert _embedded_image_is_complete(cover)
    assert not _embedded_image_is_complete(cover[:-12])


def test_media_info_queue_ignores_extractors_from_cleared_generation(
    monkeypatch,
    tmp_path,
):
    class _Signal:
        def __init__(self):
            self.callback = None

        def connect(self, callback):
            self.callback = callback

        def emit(self, *args):
            assert self.callback is not None
            self.callback(*args)

    class _Extractor:
        def __init__(self):
            self.info_ready = _Signal()
            self.thumbnail_failed = _Signal()
            self.duration_ready = _Signal()
            self.cancelled = False

        def cancel(self):
            self.cancelled = True

        def deleteLater(self):
            pass

    extractors: list[_Extractor] = []

    def _factory(_index, _url, _media_type, _worker_pool, _parent):
        extractor = _Extractor()
        extractors.append(extractor)
        return extractor

    monkeypatch.setattr(media_info_module, "_create_extractor", _factory)
    queue = _media_info_queue(tmp_path)

    queue.request(0, str(tmp_path / "first.mp4"))
    first = extractors[0]
    queue.clear()
    queue.request(0, str(tmp_path / "second.mp4"))

    assert first.cancelled is True
    assert len(extractors) == 2

    first.info_ready.emit(0, object(), "stale")
    first.thumbnail_failed.emit(0)
    first.duration_ready.emit(0, 90_000)

    assert queue.get_cached(0) == (None, "")

    second = extractors[1]
    queue.invalidate(0)
    second.info_ready.emit(0, object(), "retired")

    assert second.cancelled is True
    assert queue.get_cached(0) == (None, "")


def test_media_info_queue_rejects_fast_path_callback_after_invalidation(tmp_path):
    queue = _media_info_queue(tmp_path)
    version = queue._scheduler.version_for(0)
    old_pixmap = object()
    new_pixmap = object()
    queue._cache[0] = (old_pixmap, "old")

    queue.invalidate(0)
    queue._cache[0] = (new_pixmap, "new")

    assert not queue._emit_info_if_current(version, 0, old_pixmap, "old")
    assert queue.get_cached(0) == (new_pixmap, "new")


def test_media_info_queue_extracts_missing_duration_even_when_info_is_cached(
    monkeypatch,
    tmp_path,
):
    class _Signal:
        def connect(self, _callback):
            pass

    class _Extractor:
        def __init__(self, index):
            self.index = index
            self.info_ready = _Signal()
            self.thumbnail_failed = _Signal()
            self.duration_ready = _Signal()
            self.require_duration = False

        def set_require_duration(self, required):
            self.require_duration = required

    created: list[_Extractor] = []

    def _factory(index, _url, _media_type, _worker_pool, _parent):
        extractor = _Extractor(index)
        created.append(extractor)
        return extractor

    monkeypatch.setattr(media_info_module, "_create_extractor", _factory)
    queue = _media_info_queue(tmp_path)
    queue._cache[7] = (object(), "Cached title")

    queue.request(7, str(tmp_path / "clip.mp4"), require_duration=True)

    assert [extractor.index for extractor in created] == [7]
    assert created[0].require_duration is True
    assert queue._scheduler.is_scheduled(7)


def test_media_info_queue_keeps_cached_info_when_duration_is_not_required(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(
        media_info_module,
        "_create_extractor",
        lambda *_args: (_ for _ in ()).throw(AssertionError("unexpected extractor")),
    )
    queue = _media_info_queue(tmp_path)
    queue._cache[7] = (object(), "Cached title")

    queue.request(7, str(tmp_path / "clip.mp4"))

    assert not queue._scheduler.is_scheduled(7)


def test_duration_only_request_does_not_cache_or_emit_opportunistic_media(
    monkeypatch,
    tmp_path,
):
    extractor = _ManualExtractor()
    monkeypatch.setattr(
        media_info_module,
        "_create_extractor",
        lambda *_args: extractor,
    )
    queue = _media_info_queue(tmp_path)
    disk_writes = []
    ready = []
    durations = []
    monkeypatch.setattr(
        queue,
        "_schedule_disk_cache_save",
        lambda *args: disk_writes.append(args),
    )
    queue.info_ready.connect(lambda *args: ready.append(args))
    queue.duration_ready.connect(lambda *args: durations.append(args))

    queue.request(
        7,
        str(tmp_path / "clip.mp4"),
        require_thumbnail=False,
        require_title=False,
        require_duration=True,
    )
    extractor.duration_ready.emit(7, 12_345)
    extractor.info_ready.emit(7, media_info_module.QPixmap(1, 1), "Unexpected title")

    assert durations == [(7, 12_345)]
    assert len(ready) == 1
    assert ready[0][0] == 7
    assert ready[0][1].isNull()
    assert ready[0][2] == ""
    assert disk_writes == []


def test_remote_audio_duration_requirement_falls_back_to_qmedia(
    monkeypatch,
    tmp_path,
):
    class _IntentExtractor(_ManualExtractor):
        def __init__(self) -> None:
            super().__init__()
            self.intent = None

        def set_request_intent(self, **intent):
            self.intent = intent

    header_extractor = _ManualExtractor()
    duration_extractor = _IntentExtractor()
    monkeypatch.setattr(
        media_info_module,
        "_create_extractor",
        lambda *_args: header_extractor,
    )
    monkeypatch.setattr(
        media_info_module,
        "MediaInfoExtractor",
        lambda *_args: duration_extractor,
    )
    queue = _media_info_queue(tmp_path)
    ready = []
    durations = []
    queue.info_ready.connect(lambda *args: ready.append(args))
    queue.duration_ready.connect(lambda *args: durations.append(args))
    cover = media_info_module.QPixmap(1, 1)

    queue.request(
        7,
        "https://example.test/audio.mp3",
        "audio",
        require_duration=True,
    )
    header_extractor.info_ready.emit(7, cover, "Remote title")

    assert ready == []
    assert duration_extractor.intent == {
        "require_thumbnail": False,
        "require_title": False,
        "require_duration": True,
    }

    duration_extractor.duration_ready.emit(7, 12_345)
    duration_extractor.info_ready.emit(
        7,
        media_info_module.QPixmap(),
        "",
    )

    assert durations == [(7, 12_345)]
    assert len(ready) == 1
    assert not ready[0][1].isNull()
    assert ready[0][2] == "Remote title"


def test_media_info_queue_refills_capacity_after_extractor_factory_failure(
    monkeypatch,
    tmp_path,
):
    class _Signal:
        def connect(self, _callback):
            pass

    class _Extractor:
        def __init__(self, index):
            self.index = index
            self.info_ready = _Signal()
            self.thumbnail_failed = _Signal()
            self.duration_ready = _Signal()

    class _NullPixmap:
        def isNull(self):
            return True

    created: list[int] = []

    def _factory(index, _url, _media_type, _worker_pool, _parent):
        if index == 0:
            raise RuntimeError("factory failed")
        created.append(index)
        return _Extractor(index)

    monkeypatch.setattr(media_info_module, "_create_extractor", _factory)
    monkeypatch.setattr(media_info_module, "QPixmap", _NullPixmap)
    queue = _media_info_queue(tmp_path)
    terminal_results: list[int] = []
    queue.info_ready.connect(
        lambda index, _pixmap, _title: terminal_results.append(index)
    )
    queue._scheduler.enqueue(0, "failed.mp4", "video")
    queue._scheduler.enqueue(1, "second.mp4", "video")
    queue._scheduler.enqueue(2, "third.mp4", "video")

    queue._pump()

    assert created == [1, 2]
    assert terminal_results == []
    assert set(queue._scheduler.active) == {1, 2}
    assert not queue._scheduler.pending


def test_media_info_service_owns_queue_created_by_injected_factory(tmp_path):
    queues = []

    def _queue_factory(parent):
        queue = MediaInfoQueue(
            tmp_path / "media",
            tmp_path / "thumbs",
            _NoRemoteWorkerPool(),
            parent,
        )
        queues.append(queue)
        return queue

    service = MediaInfoService(_queue_factory)

    assert queues == [service._queue]
    assert service._queue.parent() is service


class _MediaInfoServiceQueueStub(QObject):
    info_ready = Signal(int, QPixmap, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.cache: dict[int, tuple[QPixmap, str]] = {}
        self.requests: list[tuple[int, str, str, bool, bool]] = []
        self.invalidated: list[int] = []

    def request(
        self,
        index,
        path,
        media_type,
        *,
        require_thumbnail,
        require_title,
    ):
        self.requests.append((index, path, media_type, require_thumbnail, require_title))

    def get_cached(self, index):
        return self.cache.get(index, (None, ""))

    def invalidate(self, index):
        self.invalidated.append(index)
        self.cache.pop(index, None)

    def clear(self):
        self.cache.clear()


def test_media_info_service_reemits_cached_title_without_thumbnail():
    queue = _MediaInfoServiceQueueStub()
    service = MediaInfoService(lambda parent: queue)
    ready: list[tuple[str, str]] = []
    service.info_ready.connect(lambda path, _pixmap, title: ready.append((path, title)))
    path = "C:/media/song.m4a"
    service.request(path, "audio", require_thumbnail=False)
    queue.cache[0] = (QPixmap(), "Metadata title")
    ready.clear()

    service.request(path, "audio", require_thumbnail=False)

    assert ready == [(path, "Metadata title")]
    assert len(queue.requests) == 1
    assert queue.invalidated == []


def test_media_info_service_upgrades_cached_title_to_thumbnail_request():
    queue = _MediaInfoServiceQueueStub()
    service = MediaInfoService(lambda parent: queue)
    path = "C:/media/video.mp4"
    service.request(path, "video", require_thumbnail=False)
    queue.cache[0] = (QPixmap(), "Metadata title")

    service.request(path, "video", require_thumbnail=True)

    assert queue.invalidated == [0]
    assert queue.requests[-1] == (0, path, "video", True, True)


def test_media_info_service_bounds_metadata_retention():
    queue = _MediaInfoServiceQueueStub()
    service = MediaInfoService(lambda parent: queue, capacity=2)

    service.request("C:/media/one.mp4")
    service.request("C:/media/two.mp4")
    service.request("C:/media/three.mp4")

    assert queue.invalidated == [0]
    assert service.get_cached("C:/media/one.mp4") == (None, "")
    assert set(service._path_to_idx) == {
        "C:/media/two.mp4",
        "C:/media/three.mp4",
    }
