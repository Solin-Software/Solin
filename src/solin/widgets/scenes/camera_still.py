"""Grab one still of a camera's own picture, for a preset's thumbnail.

Opens the camera's ``camera-still`` feed, keeps the latest frame, and finishes
once the picture has had a moment to settle: the first frames of a camera that
was not showing anywhere can be black or mid-exposure. The feed closes as soon
as the still is taken, so a capture costs a second or so of rendering.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtGui import QImage

from solin.controllers.scene_runtime_controller import SceneRuntimeController
from solin.controllers.scene_thumbnail_egress import SceneThumbnailEgressController
from solin.core.scenes.engine import camera_still_channel

STILL_SIZE = (320, 180)
_SETTLE_MS = 600
_TIMEOUT_MS = 6000


class CameraStillCapture(QObject):
    """One capture: emits ``captured`` with the still, or ``failed``, then is done."""

    captured = Signal(str, QImage)  # camera_id, still
    failed = Signal(str)  # camera_id

    def __init__(
        self,
        controller: SceneRuntimeController,
        camera_source_id: str,
        *,
        settle_ms: int = _SETTLE_MS,
        timeout_ms: int = _TIMEOUT_MS,
        feed_factory: Callable[[str, QObject], SceneThumbnailEgressController] | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.camera_source_id = camera_source_id
        self._controller = controller
        self._channel_id = camera_still_channel(camera_source_id)
        # The feed is injectable so the capture's own logic can be tested without
        # shared memory and a reader thread; the reader has its own tests.
        factory = feed_factory or (
            lambda channel_id, parent: SceneThumbnailEgressController(
                channel_id=channel_id, parent=parent,
            )
        )
        self._feed = factory(self._channel_id, self)
        self._feed.thumbnail_ready.connect(self._on_frame)
        self._latest: QImage | None = None
        self._finished = False
        self._settle = QTimer(self)
        self._settle.setSingleShot(True)
        self._settle.setInterval(settle_ms)
        self._settle.timeout.connect(self._finish)
        self._timeout = QTimer(self)
        self._timeout.setSingleShot(True)
        self._timeout.setInterval(timeout_ms)
        self._timeout.timeout.connect(self._finish)

    @property
    def finished(self) -> bool:
        return self._finished

    def start(self) -> None:
        width, height = STILL_SIZE
        self._feed.reconfigure((self.camera_source_id,), width, height)
        descriptor = self._feed.descriptor
        if descriptor is None or not self._controller.set_thumbnail_egress(
            descriptor, (self.camera_source_id,), width, height, channel_id=self._channel_id,
        ):
            self._finish()
            return
        self._timeout.start()

    def cancel(self) -> None:
        """Stop without reporting: whoever cancels no longer wants the answer."""
        self._finish(report=False)

    def _on_frame(self, camera_source_id: str, image: QImage) -> None:
        if self._finished or camera_source_id != self.camera_source_id or image.isNull():
            return
        self._latest = image
        if not self._settle.isActive():
            self._settle.start()

    def _finish(self, *, report: bool = True) -> None:
        if self._finished:
            return
        self._finished = True
        self._settle.stop()
        self._timeout.stop()
        self._feed.close()
        self._controller.set_thumbnail_egress(None, (), 0, 0, channel_id=self._channel_id)
        if not report:
            return
        if self._latest is not None:
            self.captured.emit(self.camera_source_id, self._latest)
        else:
            self.failed.emit(self.camera_source_id)
