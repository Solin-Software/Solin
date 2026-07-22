from __future__ import annotations

import time

from PySide6.QtCore import QCoreApplication
from PySide6.QtGui import QImage

from solin.controllers.media_operation_coordinator import MediaOperationCoordinator
from solin.controllers.media_probe_controller import MediaProbeController
from solin.core.media.presentation_probe import (
    MediaPresentationProbeResult,
    ProbedMediaAvailability,
)
from solin.ui.qml.media_tree.state import MediaAvailability, MediaStateRegistry


_APP = QCoreApplication.instance() or QCoreApplication([])


def _wait_until(predicate, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        QCoreApplication.processEvents()
        if time.monotonic() >= deadline:
            raise AssertionError("Timed out waiting for media probe")
        time.sleep(0.002)
    QCoreApplication.processEvents()


def test_controller_rejects_slow_completion_for_a_replaced_source(
    tmp_path,
    monkeypatch,
) -> None:
    coordinator = MediaOperationCoordinator(max_workers=2)
    registry = MediaStateRegistry()
    controller = MediaProbeController(
        coordinator=coordinator,
        registry=registry,
        media_cache_dir=tmp_path / "cache",
    )

    def probe(request):
        if request.source == "old.mp4":
            time.sleep(0.12)
            return MediaPresentationProbeResult(ProbedMediaAvailability.MISSING)
        return MediaPresentationProbeResult(
            ProbedMediaAvailability.AVAILABLE,
            local_path="new.mp4",
        )

    monkeypatch.setattr(
        "solin.controllers.media_probe_controller.probe_media_presentation",
        probe,
    )
    controller.request(owner_id="playlist:1", node_id="media-1", source="old.mp4")
    controller.request(owner_id="playlist:1", node_id="media-1", source="new.mp4")

    _wait_until(lambda: coordinator.active_count == 0)

    state = registry.state("playlist:1", "media-1")
    assert state.availability == MediaAvailability.AVAILABLE
    assert state.local_path == "new.mp4"
    controller.shutdown()
    coordinator.shutdown()


def test_controller_retries_temporary_cloud_lock_without_ui_polling(
    tmp_path,
    monkeypatch,
) -> None:
    coordinator = MediaOperationCoordinator()
    registry = MediaStateRegistry()
    controller = MediaProbeController(
        coordinator=coordinator,
        registry=registry,
        media_cache_dir=tmp_path / "cache",
    )
    attempts = 0

    def probe(_request):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return MediaPresentationProbeResult(
                ProbedMediaAvailability.TEMPORARILY_UNAVAILABLE,
                error="locked",
            )
        return MediaPresentationProbeResult(
            ProbedMediaAvailability.AVAILABLE,
            local_path="hydrated.mp4",
        )

    monkeypatch.setattr(
        "solin.controllers.media_probe_controller.probe_media_presentation",
        probe,
    )
    monkeypatch.setattr(
        "solin.controllers.media_probe_controller._RETRY_DELAYS_MS",
        (1,),
    )
    controller.request(owner_id="playlist:1", node_id="media-1", source="cloud.mp4")

    _wait_until(
        lambda: attempts >= 2
        and registry.state("playlist:1", "media-1").availability
        == MediaAvailability.AVAILABLE
    )

    assert registry.state("playlist:1", "media-1").local_path == "hydrated.mp4"
    controller.shutdown()
    _wait_until(lambda: coordinator.active_count == 0)
    coordinator.shutdown()


def test_controller_retries_existing_thumbnail_until_it_decodes(
    tmp_path,
    monkeypatch,
) -> None:
    coordinator = MediaOperationCoordinator()
    registry = MediaStateRegistry()
    controller = MediaProbeController(
        coordinator=coordinator,
        registry=registry,
        media_cache_dir=tmp_path / "cache",
    )
    reads = 0
    ready = []

    monkeypatch.setattr(
        "solin.controllers.media_probe_controller.probe_media_presentation",
        lambda _request: MediaPresentationProbeResult(
            ProbedMediaAvailability.AVAILABLE,
            local_path="video.mp4",
            thumbnail_exists=True,
            thumbnail_source_signature="10:20",
            source_signature="10:20",
        ),
    )

    def read_thumbnail(_path):
        nonlocal reads
        reads += 1
        if reads == 1:
            return None
        image = QImage(2, 2, QImage.Format.Format_RGB32)
        image.fill(0xFF223344)
        return image

    monkeypatch.setattr(
        "solin.controllers.media_probe_controller._read_thumbnail",
        read_thumbnail,
    )
    monkeypatch.setattr(
        "solin.controllers.media_probe_controller._RETRY_DELAYS_MS",
        (1,),
    )
    controller.thumbnailReady.connect(lambda *_args: ready.append(_args[2]))

    controller.request(
        owner_id="playlist:1",
        node_id="media-1",
        source="video.mp4",
        thumbnail_path=tmp_path / "thumb.jpg",
        thumbnail_source="image://playlistthumbs/media-1",
    )
    _wait_until(
        lambda: reads >= 2
        and registry.state("playlist:1", "media-1").thumbnail_source
        == "image://playlistthumbs/media-1"
    )

    assert ready[0] is None
    assert isinstance(ready[-1], QImage)
    controller.shutdown()
    _wait_until(lambda: coordinator.active_count == 0)
    coordinator.shutdown()


def test_controller_rejects_stale_thumbnail_when_same_path_content_changes(
    tmp_path,
    monkeypatch,
) -> None:
    coordinator = MediaOperationCoordinator()
    registry = MediaStateRegistry()
    controller = MediaProbeController(
        coordinator=coordinator,
        registry=registry,
        media_cache_dir=tmp_path / "cache",
    )
    signatures = iter(("10:20", "11:30"))
    ready = []

    monkeypatch.setattr(
        "solin.controllers.media_probe_controller.probe_media_presentation",
        lambda _request: (
            lambda signature: MediaPresentationProbeResult(
                ProbedMediaAvailability.AVAILABLE,
                local_path="video.mp4",
                thumbnail_exists=True,
                thumbnail_source_signature="10:20",
                source_signature=signature,
            )
        )(next(signatures)),
    )

    def read_thumbnail(_path):
        image = QImage(2, 2, QImage.Format.Format_RGB32)
        image.fill(0xFF223344)
        return image

    monkeypatch.setattr(
        "solin.controllers.media_probe_controller._read_thumbnail",
        read_thumbnail,
    )
    controller.thumbnailReady.connect(lambda *_args: ready.append(_args[2]))

    request = {
        "owner_id": "playlist:1",
        "node_id": "media-1",
        "source": "video.mp4",
        "thumbnail_path": tmp_path / "thumb.jpg",
        "thumbnail_source": "image://playlistthumbs/media-1",
    }
    controller.request(**request)
    _wait_until(
        lambda: registry.state("playlist:1", "media-1").source_signature == "10:20"
    )
    controller.request(**request)
    _wait_until(
        lambda: registry.state("playlist:1", "media-1").source_signature == "11:30"
    )

    assert isinstance(ready[0], QImage)
    assert ready[-1] is None
    assert registry.state("playlist:1", "media-1").thumbnail_source == ""
    controller.shutdown()
    _wait_until(lambda: coordinator.active_count == 0)
    coordinator.shutdown()


def test_controller_stops_retrying_a_permanently_invalid_thumbnail(
    tmp_path,
    monkeypatch,
) -> None:
    coordinator = MediaOperationCoordinator()
    registry = MediaStateRegistry()
    controller = MediaProbeController(
        coordinator=coordinator,
        registry=registry,
        media_cache_dir=tmp_path / "cache",
    )
    reads = 0

    monkeypatch.setattr(
        "solin.controllers.media_probe_controller.probe_media_presentation",
        lambda _request: MediaPresentationProbeResult(
            ProbedMediaAvailability.AVAILABLE,
            local_path="video.mp4",
            thumbnail_exists=True,
            thumbnail_source_signature="10:20",
            source_signature="10:20",
        ),
    )

    def unreadable(_path):
        nonlocal reads
        reads += 1
        return None

    monkeypatch.setattr(
        "solin.controllers.media_probe_controller._read_thumbnail",
        unreadable,
    )
    monkeypatch.setattr(
        "solin.controllers.media_probe_controller._RETRY_DELAYS_MS",
        (1,),
    )
    monkeypatch.setattr(
        "solin.controllers.media_probe_controller._THUMBNAIL_DECODE_GRACE_ATTEMPTS",
        2,
    )

    controller.request(
        owner_id="playlist:1",
        node_id="media-1",
        source="video.mp4",
        thumbnail_path=tmp_path / "thumb.jpg",
        thumbnail_source="image://playlistthumbs/media-1",
    )
    _wait_until(lambda: reads == 3 and coordinator.active_count == 0)

    assert registry.state("playlist:1", "media-1").thumbnail_source == ""
    QCoreApplication.processEvents()
    time.sleep(0.01)
    QCoreApplication.processEvents()
    assert reads == 3
    controller.shutdown()
    coordinator.shutdown()


def test_controller_stabilizes_missing_local_media_before_confirming_absence(
    tmp_path,
    monkeypatch,
) -> None:
    coordinator = MediaOperationCoordinator()
    registry = MediaStateRegistry()
    controller = MediaProbeController(
        coordinator=coordinator,
        registry=registry,
        media_cache_dir=tmp_path / "cache",
    )
    attempts = 0

    def missing(_request):
        nonlocal attempts
        attempts += 1
        return MediaPresentationProbeResult(ProbedMediaAvailability.MISSING)

    monkeypatch.setattr(
        "solin.controllers.media_probe_controller.probe_media_presentation",
        missing,
    )
    monkeypatch.setattr(
        "solin.controllers.media_probe_controller._RETRY_DELAYS_MS",
        (1,),
    )
    monkeypatch.setattr(
        "solin.controllers.media_probe_controller._MISSING_GRACE_ATTEMPTS",
        2,
    )
    controller.request(owner_id="playlist:1", node_id="media-1", source="cloud.mp4")

    _wait_until(
        lambda: attempts >= 3
        and registry.state("playlist:1", "media-1").availability
        == MediaAvailability.MISSING
    )

    controller.shutdown()
    _wait_until(lambda: coordinator.active_count == 0)
    coordinator.shutdown()
