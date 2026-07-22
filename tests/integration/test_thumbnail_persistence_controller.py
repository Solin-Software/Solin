from __future__ import annotations

import time

from PySide6.QtCore import QCoreApplication
from PySide6.QtGui import QColor, QImage

from solin.controllers.media_operation_coordinator import MediaOperationCoordinator
from solin.controllers.thumbnail_persistence_controller import (
    ThumbnailPersistenceController,
)
from solin.core.media.thumbnail_store import ThumbnailStore


_APP = QCoreApplication.instance() or QCoreApplication([])


def _wait_until(predicate, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        QCoreApplication.processEvents()
        if time.monotonic() >= deadline:
            raise AssertionError("Timed out waiting for thumbnail persistence")
        time.sleep(0.002)
    QCoreApplication.processEvents()


def test_image_is_encoded_and_atomically_published_off_the_gui_thread(tmp_path) -> None:
    coordinator = MediaOperationCoordinator()
    controller = ThumbnailPersistenceController(coordinator)
    store = ThumbnailStore(tmp_path / "thumbs")
    image = QImage(32, 18, QImage.Format.Format_RGB32)
    image.fill(QColor("#336699"))
    stored: list[tuple[str, str]] = []
    controller.thumbnailStored.connect(lambda owner, node: stored.append((owner, node)))

    assert controller.save_image(
        owner_id="playlist:1",
        node_id="media-1",
        storage_id="media-1",
        store=store,
        image=image,
        source_signature="100:200",
    )

    _wait_until(lambda: bool(stored))

    assert stored == [("playlist:1", "media-1")]
    assert store.path("media-1").read_bytes().startswith(b"\xff\xd8")
    assert store.source_signature_path("media-1").read_text(encoding="utf-8") == (
        "100:200"
    )
    coordinator.shutdown()
