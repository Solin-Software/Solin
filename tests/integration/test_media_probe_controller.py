from __future__ import annotations

import time

from PySide6.QtCore import QCoreApplication

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
