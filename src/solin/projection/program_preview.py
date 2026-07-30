"""Operator-preview tap on the libobs program output (channel 0).

For content libobs owns and composites itself — the camera via ``v4l2_input`` —
the operator's projection-bar preview can't get frames from a Qt service (libobs
holds the device open). This taps the composited channel-0 output at a small
size and emits it as a ``QImage`` the preview widget can show.

Process-wide singleton: one shared channel-0 raw callback. The graphics thread
buffers the latest frame under a lock; a ~30fps GUI-thread timer converts and
emits it (coalescing dropped frames), so nothing blocks the render thread.
"""

from __future__ import annotations

import logging
import threading

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtGui import QImage

from ..core.media.obs_runtime import obs_runtime

log = logging.getLogger(__name__)

_PREVIEW_WIDTH = 640
_PREVIEW_HEIGHT = 360
_INTERVAL_MS = 33  # ~30 fps


class ProgramPreviewTap(QObject):
    """Emits the composited channel-0 output as small QImages while enabled."""

    frame_ready = Signal(object)  # QImage

    def __init__(self) -> None:
        super().__init__()
        self._runtime = obs_runtime()
        self._cb = None
        self._active = False
        self._lock = threading.Lock()
        self._latest: tuple[bytes, int, int, int] | None = None
        self._timer = QTimer(self)
        self._timer.setInterval(_INTERVAL_MS)
        self._timer.timeout.connect(self._emit)

    def set_enabled(self, enabled: bool) -> None:
        if enabled and not self._active:
            self._start()
        elif not enabled and self._active:
            self._stop()

    def _start(self) -> None:
        try:
            self._runtime.ensure_started()
            ob = self._runtime.ob
            self._cb = ob.add_raw_video_callback(
                self._on_raw_frame,
                format=int(ob.VideoFormat.BGRA),
                width=_PREVIEW_WIDTH,
                height=_PREVIEW_HEIGHT,
            )
        except Exception:  # noqa: BLE001 - libobs boundary
            log.debug("Could not start program preview tap", exc_info=True)
            return
        self._active = True
        self._timer.start()

    def _stop(self) -> None:
        self._timer.stop()
        if self._cb is not None:
            try:
                self._runtime.ob.remove_raw_video_callback(self._cb)
            except Exception:  # noqa: BLE001 - libobs boundary
                log.debug("Could not remove program preview tap", exc_info=True)
            self._cb = None
        with self._lock:
            self._latest = None
        self._active = False

    def _on_raw_frame(self, planes, linesizes, width, height, _fmt, _ts) -> None:
        # graphics thread: buffer the latest frame cheaply.
        data = planes[0] if planes else b""
        if not data or width <= 0 or height <= 0:
            return
        with self._lock:
            self._latest = (data, int(width), int(height), int(linesizes[0]))

    def _emit(self) -> None:
        with self._lock:
            latest = self._latest
            self._latest = None
        if latest is None:
            return
        data, width, height, stride = latest
        image = QImage(data, width, height, stride, QImage.Format.Format_ARGB32).copy()
        if not image.isNull():
            self.frame_ready.emit(image)


_tap: ProgramPreviewTap | None = None


def program_preview_tap() -> ProgramPreviewTap:
    """Return the process-wide :class:`ProgramPreviewTap` singleton."""
    global _tap
    if _tap is None:
        _tap = ProgramPreviewTap()
    return _tap


__all__ = ["ProgramPreviewTap", "program_preview_tap"]
