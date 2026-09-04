"""Off-GUI-thread conversion of ``QVideoFrame`` into a paint-ready ``QImage``.

``QVideoFrame.toImage()`` routes through Qt's RHI whenever an RHI exists on the
calling thread.  On GPUs the RHI cannot drive at video rates that conversion
costs over 100 ms per frame and blocks the GUI thread, dropping projected video
to a few frames per second.  A worker thread has no RHI, so Qt falls back to its
own CPU converter and returns the same frame in a few milliseconds.

Only one frame is ever in flight.  Converting is an order of magnitude cheaper
than painting, so without that bound the converted images outrun the surfaces
and pile up in the event queue until the process is wedged.  Frames that arrive
while a conversion is in flight are dropped, which is what live projection
wants anyway.

Every accepted frame produces exactly one result — :attr:`image_ready` or
:attr:`conversion_failed` — so a caller may rely on the signal to know the
converter is free again.  When the worker cannot convert at all (a frame that
only exists as a GPU texture) the converter falls back to converting on the GUI
thread, which is the behaviour that predates this class.
"""

from __future__ import annotations

import threading

from PySide6.QtCore import QObject, Qt, Signal, Slot
from PySide6.QtGui import QImage
from PySide6.QtMultimedia import QVideoFrame

from ..foundation.exception_logging import log_ignored_exception


class _ConversionState:
    """Hand-off between the GUI thread and the worker.

    Deliberately holds no Qt object: it is also the shutdown hook wired to the
    owner's ``destroyed`` signal, which fires while the Python wrapper of a
    ``QObject`` may already be unusable.
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.pending: QVideoFrame | None = None
        self.stopping = False
        self.thread: threading.Thread | None = None

    def submit(self, frame: QVideoFrame) -> None:
        with self.lock:
            self.pending = frame
        self.wake.set()

    def take(self) -> QVideoFrame | None:
        with self.lock:
            frame, self.pending = self.pending, None
        return frame

    def request_stop(self) -> None:
        """Stop the worker and wait for the conversion in flight to finish."""
        self.stopping = True
        with self.lock:
            self.pending = None
        self.wake.set()
        thread = self.thread
        if (
            thread is not None
            and thread.is_alive()
            and thread is not threading.current_thread()
        ):
            thread.join(timeout=1.0)


class VideoFrameConverter(QObject):
    """Deliver converted frames to the GUI thread without blocking it."""

    image_ready = Signal(QImage)
    conversion_failed = Signal()

    # Emitted on the worker thread; queued onto the thread that owns this object.
    _converted = Signal(QImage)
    _unavailable = Signal()

    def __init__(
        self,
        target_format: QImage.Format,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._target_format = target_format
        self._offloaded = True
        self._in_flight = False
        self._state = _ConversionState()
        self._converted.connect(
            self._on_converted, Qt.ConnectionType.QueuedConnection
        )
        self._unavailable.connect(
            self._disable_offload, Qt.ConnectionType.QueuedConnection
        )
        if parent is not None:
            parent.destroyed.connect(self._state.request_stop)
        self._state.thread = threading.Thread(
            target=self._convert_frames,
            name="video-frame-converter",
            daemon=True,
        )
        self._state.thread.start()

    @property
    def busy(self) -> bool:
        """Whether a frame is still on its way back to the owning thread."""
        return self._in_flight

    def convert(self, frame: QVideoFrame) -> None:
        """Convert a frame, unless one is already in flight."""
        if not self._offloaded:
            self._convert_here(frame)
            return
        if self._in_flight:
            return
        self._in_flight = True
        self._state.submit(frame)

    def stop(self) -> None:
        """Shut the conversion thread down."""
        self._state.request_stop()

    # ── Worker thread ─────────────────────────────────────────────────────

    def _convert_frames(self) -> None:
        state = self._state
        while not state.stopping:
            state.wake.wait()
            state.wake.clear()
            while not state.stopping:
                frame = state.take()
                if frame is None:
                    break
                image = self._to_image(frame)
                if state.stopping:
                    return
                if image.isNull():
                    # Nothing this thread can do with the frame. Hand the work
                    # back to the owning thread, which also releases the caller.
                    self._unavailable.emit()
                    return
                self._converted.emit(image)

    def _to_image(self, frame: QVideoFrame) -> QImage:
        """Convert one frame off the GUI thread; a null image gives the worker up."""
        try:
            image = frame.toImage()
        except Exception:  # noqa: BLE001 - Qt video-frame conversion boundary
            log_ignored_exception(__name__, "Could not convert a video frame")
            return QImage()
        if image.isNull():
            return image
        if image.format() != self._target_format:
            image = image.convertToFormat(self._target_format)
        return image

    # ── Owning thread ─────────────────────────────────────────────────────

    @Slot(QImage)
    def _on_converted(self, image: QImage) -> None:
        self._in_flight = False
        self.image_ready.emit(image)

    @Slot()
    def _disable_offload(self) -> None:
        """Give up on the worker; later frames convert on the owning thread."""
        self._offloaded = False
        self._state.request_stop()
        self._in_flight = False
        self.conversion_failed.emit()

    def _convert_here(self, frame: QVideoFrame) -> None:
        try:
            image = frame.toImage()
        except Exception:  # noqa: BLE001 - Qt video-frame conversion boundary
            log_ignored_exception(__name__, "Could not convert a video frame")
            self.conversion_failed.emit()
            return
        if image.isNull():
            self.conversion_failed.emit()
            return
        if image.format() != self._target_format:
            image = image.convertToFormat(self._target_format)
        self.image_ready.emit(image)
