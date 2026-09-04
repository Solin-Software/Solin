"""Unit tests for VideoFrameConverter — offload, bound, format and fallback.

The converter exists because QVideoFrame.toImage() blocks the GUI thread when it
takes Qt's RHI path.  These tests pin the observable contract: a converted image
arrives in the requested format, only one frame is ever in flight, and every
accepted frame produces exactly one result signal so the caller knows the
converter is free again.
"""

from __future__ import annotations

import threading

from PySide6.QtCore import QCoreApplication, QSize
from PySide6.QtGui import QImage
from PySide6.QtMultimedia import QVideoFrame, QVideoFrameFormat

from solin.core.media.frame_conversion import VideoFrameConverter


_TARGET = QImage.Format.Format_ARGB32_Premultiplied


def _frame(width: int = 64, height: int = 48) -> QVideoFrame:
    frame = QVideoFrame(
        QVideoFrameFormat(
            QSize(width, height),
            QVideoFrameFormat.PixelFormat.Format_NV12,
        )
    )
    frame.map(QVideoFrame.MapMode.WriteOnly)
    frame.unmap()
    return frame


def _wait_for(predicate, timeout_ms: int = 5000) -> bool:
    """Pump the event loop until the queued conversion result is delivered."""
    for _ in range(timeout_ms // 5):
        if predicate():
            return True
        QCoreApplication.processEvents()
        threading.Event().wait(0.005)
    return predicate()


def test_delivers_the_converted_frame_in_the_target_format():
    converter = VideoFrameConverter(_TARGET)
    received: list[QImage] = []
    converter.image_ready.connect(received.append)
    try:
        converter.convert(_frame())
        assert _wait_for(lambda: bool(received))
    finally:
        converter.stop()

    image = received[0]
    assert image.format() == _TARGET
    assert (image.width(), image.height()) == (64, 48)


def test_only_one_frame_is_in_flight():
    converter = VideoFrameConverter(_TARGET)
    received: list[QImage] = []
    converter.image_ready.connect(received.append)
    try:
        converter.convert(_frame(64, 48))
        assert converter.busy is True
        # Frames arriving while a conversion is in flight are dropped, so the
        # converted images can never outrun the surfaces that paint them.
        converter.convert(_frame(16, 12))
        assert _wait_for(lambda: bool(received))
        assert converter.busy is False
        assert len(received) == 1
        assert (received[0].width(), received[0].height()) == (64, 48)
    finally:
        converter.stop()


def test_the_converter_accepts_frames_again_after_a_delivery():
    converter = VideoFrameConverter(_TARGET)
    received: list[QImage] = []
    converter.image_ready.connect(received.append)
    try:
        converter.convert(_frame(64, 48))
        assert _wait_for(lambda: len(received) == 1)
        converter.convert(_frame(16, 12))
        assert _wait_for(lambda: len(received) == 2)
        assert (received[1].width(), received[1].height()) == (16, 12)
    finally:
        converter.stop()


def test_reports_failure_so_the_caller_stops_waiting():
    converter = VideoFrameConverter(_TARGET)
    failures: list[int] = []
    converter.conversion_failed.connect(lambda: failures.append(1))
    try:
        converter._disable_offload()  # the worker could not convert
        converter.convert(QVideoFrame())  # invalid frame, nothing to convert
        assert failures  # one for the offload loss, one for the invalid frame
        assert converter.busy is False
    finally:
        converter.stop()


def test_falls_back_to_converting_on_the_calling_thread():
    converter = VideoFrameConverter(_TARGET)
    received: list[QImage] = []
    converter.image_ready.connect(received.append)
    try:
        converter._disable_offload()
        converter.convert(_frame())
        # No event loop turn: the fallback converts inline on the caller thread.
        assert len(received) == 1
        assert received[0].format() == _TARGET
    finally:
        converter.stop()


def test_stop_is_idempotent():
    converter = VideoFrameConverter(_TARGET)
    converter.stop()
    converter.stop()


def test_a_raising_frame_releases_the_caller_instead_of_wedging():
    """A conversion that throws must not leave the converter permanently busy."""

    class _RaisingFrame:
        def toImage(self):
            raise RuntimeError("boom")

    converter = VideoFrameConverter(_TARGET)
    failures: list[int] = []
    converter.conversion_failed.connect(lambda: failures.append(1))
    try:
        converter.convert(_RaisingFrame())
        assert _wait_for(lambda: bool(failures))
        assert converter.busy is False
        # The next frame still converts, now on the calling thread.
        received: list[QImage] = []
        converter.image_ready.connect(received.append)
        converter.convert(_frame())
        assert len(received) == 1
    finally:
        converter.stop()
