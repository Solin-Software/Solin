from __future__ import annotations
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest
from PySide6.QtCore import QSize
from PySide6.QtGui import QColor, QImage
from PySide6.QtMultimedia import QVideoFrame, QVideoFrameFormat

from solin.controllers.content_frame_ingress_controller import (
    ContentFrameIngressController,
)
from solin.core.projection.image_framing import ImageTransform
from solin.core.scenes.engine import SceneEngineSnapshot
from solin.core.scenes.frame_channel import (
    SharedMemoryBgraFrameSubscriber,
    SharedMemoryVideoFrameSubscriber,
    VideoFrame,
)
from solin.core.scenes.model import (
    BusId,
    CONTENT_SOURCE_ID,
    SceneDefinition,
    SceneDocument,
    VideoPixelFormat,
)
from solin.core.scenes.native_engine import create_native_scene_engine
from solin.core.scenes.presets import SceneSeedNames, create_default_scene_document


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _bgra_pixel(frame: VideoFrame, x: int, y: int) -> bytes:
    offset = (y * frame.width + x) * 4
    return frame.pixels[offset : offset + 4]


def _wait_for_pixel(
    subscriber: SharedMemoryBgraFrameSubscriber,
    *,
    x: int,
    y: int,
    expected: bytes,
    timeout: float = 5.0,
) -> VideoFrame | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            candidate = subscriber.read_latest()
        except TimeoutError:
            candidate = None
        if candidate is not None:
            if _bgra_pixel(candidate, x, y) == expected:
                return candidate
        time.sleep(1 / 120)
    return None


def _wait_for_red_center(
    subscriber: SharedMemoryBgraFrameSubscriber,
    *,
    timeout: float = 5.0,
) -> VideoFrame | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            candidate = subscriber.read_latest()
        except TimeoutError:
            candidate = None
        if candidate is not None:
            blue, green, red, alpha = _bgra_pixel(
                candidate,
                candidate.width // 2,
                candidate.height // 2,
            )
            if red >= 220 and green <= 35 and blue <= 35 and alpha == 255:
                return candidate
        time.sleep(1 / 120)
    return None


def _padded_red_nv12_frame() -> QVideoFrame:
    width = 1278
    height = 720
    frame = QVideoFrame(
        QVideoFrameFormat(
            QSize(width, height),
            QVideoFrameFormat.PixelFormat.Format_NV12,
        )
    )
    assert frame.map(QVideoFrame.MapMode.WriteOnly)
    try:
        y_stride = frame.bytesPerLine(0)
        uv_stride = frame.bytesPerLine(1)
        assert y_stride > width
        y_plane = frame.bits(0)
        uv_plane = frame.bits(1)
        y_row = bytes((81,)) * width
        uv_row = bytes((90, 240)) * (width // 2)
        for row in range(height):
            start = row * y_stride
            y_plane[start : start + width] = y_row
        for row in range(height // 2):
            start = row * uv_stride
            uv_plane[start : start + width] = uv_row
    finally:
        frame.unmap()
    return frame


def _quadrant_image(width: int = 800, height: int = 600) -> QImage:
    image = QImage(width, height, QImage.Format.Format_ARGB32)
    half_width = width // 2
    half_height = height // 2
    for y in range(height):
        for x in range(width):
            if x < half_width and y < half_height:
                color = QColor("#e02020")
            elif x >= half_width and y < half_height:
                color = QColor("#20d020")
            elif x < half_width:
                color = QColor("#2040e0")
            else:
                color = QColor("#e0d020")
            image.setPixelColor(x, y, color)
    return image


def _wait_for_clean_aspect_transition(
    subscriber: SharedMemoryBgraFrameSubscriber,
    *,
    previous_center: bytes,
    previous_edge: bytes,
    next_center: bytes,
    next_edge: bytes,
    timeout: float = 5.0,
) -> VideoFrame:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            candidate = subscriber.read_latest()
        except TimeoutError:
            candidate = None
        if candidate is None:
            time.sleep(1 / 240)
            continue
        center = _bgra_pixel(candidate, candidate.width // 2, candidate.height // 2)
        edge = _bgra_pixel(candidate, 0, candidate.height // 2)
        if center == previous_center:
            assert edge == previous_edge, (
                "the previous raster was rendered with the next sample's geometry"
            )
        if center == next_center and edge == next_edge:
            return candidate
    raise AssertionError("the aspect-ratio transition did not reach its next frame")


def _content_document() -> tuple[SceneDocument, SceneDefinition]:
    document = create_default_scene_document(
        SceneSeedNames(
            content_source="Content",
            default_camera_source="Camera",
            no_signal_source="No signal",
            content_scene="Content scene",
            camera_scene="Camera scene",
            content_camera_pip_scene="Content and camera",
            no_signal_scene="No signal scene",
            content_layer="Content",
            camera_layer="Camera",
            background_layer="Background",
        ),
        document_id="native-content-pipeline-smoke",
        created_at="2026-01-01T00:00:00+00:00",
    )
    content_scene = next(
        scene
        for scene in document.scenes
        if len(scene.layers) == 1 and scene.layers[0].source_id == CONTENT_SOURCE_ID
    )
    return (
        replace(
            document,
            outputs=tuple(
                replace(route, default_scene_id=content_scene.id)
                for route in document.outputs
            ),
        ),
        content_scene,
    )


@pytest.mark.skipif(sys.platform != "win32", reason="Windows native media pipeline")
def test_actual_size_content_reaches_composed_native_output(tmp_path) -> None:
    engine = create_native_scene_engine(
        tmp_path,
        repository_root=REPOSITORY_ROOT,
    )
    if engine is None:
        pytest.skip("Built native media engine is unavailable")

    ingress = ContentFrameIngressController(
        maximum_fps=30,
        canvas_width=1920,
        canvas_height=1080,
    )
    egress = SharedMemoryBgraFrameSubscriber(1920, 1080)
    program_egress = SharedMemoryVideoFrameSubscriber(1920, 1080)
    document, content_scene = _content_document()
    image = QImage(1280, 720, QImage.Format.Format_ARGB32)
    image.fill(QColor("#123456"))
    latest = None
    center_pixel = b""

    try:
        capabilities = engine.start(
            session_id="native-content-smoke",
            deadline_ms=10_000,
        ).result(15)
        if not capabilities.local_cameras and not capabilities.rtsp_cameras:
            pytest.skip("Native GStreamer graph is unavailable")
        snapshot = SceneEngineSnapshot(
            session_id="native-content-smoke",
            sequence=1,
            document=document,
            active_scenes=tuple(
                (route.bus_id, content_scene.id) for route in document.outputs
            ),
            render_enabled=(
                (BusId.MEDIA_WINDOWS, True),
                (BusId.VIRTUAL_CAMERA, True),
            ),
            output_enabled=(
                (BusId.MEDIA_WINDOWS, True),
                (BusId.VIRTUAL_CAMERA, False),
            ),
            content_ingress=ingress.descriptor,
            preview_egress=egress.descriptor,
            program_egress=program_egress.descriptor,
        )
        acknowledged = engine.hydrate(
            snapshot,
            request_id="native-content-smoke-hydrate",
            deadline_ms=10_000,
        ).result(15)
        assert acknowledged.applied

        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            ingress.submit_frame(image)
            try:
                candidate = egress.read_latest()
            except TimeoutError:
                candidate = None
            if candidate is not None:
                latest = candidate
                offset = (
                    (candidate.height // 2) * candidate.width
                    + candidate.width // 2
                ) * 4
                center_pixel = candidate.pixels[offset : offset + 4]
                if center_pixel == bytes((0x56, 0x34, 0x12, 0xFF)):
                    break
            time.sleep(1 / 60)

        assert latest is not None
        assert (latest.width, latest.height) == (1920, 1080)
        assert center_pixel == bytes((0x56, 0x34, 0x12, 0xFF))

        # Qt commonly aligns NV12 rows beyond the visible width. This exercises
        # the production ingress contract end-to-end: preserve both strides,
        # bulk-copy each plane once, and let GstVideoMeta describe the padding.
        padded_nv12 = _padded_red_nv12_frame()
        padded_output = None
        padded_deadline = time.monotonic() + 5.0
        while padded_output is None and time.monotonic() < padded_deadline:
            ingress.submit_frame(padded_nv12)
            padded_output = _wait_for_red_center(egress, timeout=0.05)
        assert padded_output is not None

        program_frame = None
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            ingress.submit_frame(image)
            try:
                program_frame = program_egress.read_latest()
            except TimeoutError:
                program_frame = None
            if program_frame is not None:
                break
            time.sleep(1 / 60)
        assert program_frame is not None
        assert program_frame.pixel_format is VideoPixelFormat.NV12
        assert (program_frame.width, program_frame.height) == (1920, 1080)
        assert len(program_frame.pixels) == 1920 * 1080 * 3 // 2

        four_by_three = QImage(800, 600, QImage.Format.Format_ARGB32)
        four_by_three.fill(QColor("#20d020"))
        ingress.submit_frame(four_by_three)
        latest = _wait_for_pixel(
            egress,
            x=960,
            y=540,
            expected=bytes((0x20, 0xD0, 0x20, 0xFF)),
        )
        assert latest is not None
        assert _bgra_pixel(latest, 239, 540) == bytes((0, 0, 0, 0xFF))
        assert _bgra_pixel(latest, 240, 540) == bytes((0x20, 0xD0, 0x20, 0xFF))
        assert _bgra_pixel(latest, 1679, 540) == bytes((0x20, 0xD0, 0x20, 0xFF))
        assert _bgra_pixel(latest, 1680, 540) == bytes((0, 0, 0, 0xFF))

        sixteen_by_nine = QImage(1280, 720, QImage.Format.Format_ARGB32)
        sixteen_by_nine.fill(QColor("#d0d020"))
        green = bytes((0x20, 0xD0, 0x20, 0xFF))
        yellow = bytes((0x20, 0xD0, 0xD0, 0xFF))
        black = bytes((0, 0, 0, 0xFF))
        for _ in range(8):
            ingress.submit_frame(sixteen_by_nine)
            latest = _wait_for_clean_aspect_transition(
                egress,
                previous_center=green,
                previous_edge=black,
                next_center=yellow,
                next_edge=yellow,
            )
            assert _bgra_pixel(latest, 1919, 540) == yellow

            ingress.submit_frame(four_by_three)
            latest = _wait_for_clean_aspect_transition(
                egress,
                previous_center=yellow,
                previous_edge=yellow,
                next_center=green,
                next_edge=black,
            )
            assert _bgra_pixel(latest, 240, 540) == green

        ingress.submit_frame(sixteen_by_nine)
        latest = _wait_for_clean_aspect_transition(
            egress,
            previous_center=green,
            previous_edge=black,
            next_center=yellow,
            next_edge=yellow,
        )

        nv12 = QVideoFrame(
            QVideoFrameFormat(
                QSize(1280, 720),
                QVideoFrameFormat.PixelFormat.Format_NV12,
            )
        )
        assert nv12.map(QVideoFrame.MapMode.WriteOnly)
        nv12.bits(0)[:] = bytes((235,)) * nv12.mappedBytes(0)
        nv12.bits(1)[:] = bytes((128,)) * nv12.mappedBytes(1)
        nv12.unmap()
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            ingress.submit_frame(nv12)
            try:
                candidate = egress.read_latest()
            except TimeoutError:
                candidate = None
            if candidate is not None:
                offset = (
                    (candidate.height // 2) * candidate.width
                    + candidate.width // 2
                ) * 4
                center_pixel = candidate.pixels[offset : offset + 4]
                if len(center_pixel) == 4 and min(center_pixel[:3]) >= 245:
                    break
            time.sleep(1 / 60)
        assert min(center_pixel[:3]) >= 245

        # Image framing is authored once on the canonical Raw source. A 4:3
        # image normally has black side bars in the 16:9 scene; zooming it to
        # 2x must cover the edge in the composed output without republishing
        # transformed pixels from Python.
        ingress.set_media_epoch(1)
        ingress.set_image_transform(
            ImageTransform(1.0, 0.0, 0.0),
            media_epoch=1,
            canvas_width=1920,
            canvas_height=1080,
            animate=False,
        )
        ingress.submit_frame(four_by_three)
        framed_identity = _wait_for_pixel(
            egress,
            x=0,
            y=540,
            expected=black,
            timeout=8.0,
        )
        assert framed_identity is not None

        # A future target is retained but cannot mutate the outgoing epoch.
        ingress.set_image_transform(
            ImageTransform(2.0, 0.0, 0.0),
            media_epoch=2,
            canvas_width=1920,
            canvas_height=1080,
            animate=False,
        )
        time.sleep(0.1)
        try:
            still_identity = egress.read_latest()
        except TimeoutError:
            still_identity = None
        if still_identity is not None:
            assert _bgra_pixel(still_identity, 0, 540) == black

        ingress.set_media_epoch(2)
        ingress.submit_frame(four_by_three)
        zoomed = _wait_for_pixel(
            egress,
            x=0,
            y=540,
            expected=green,
            timeout=8.0,
        )
        assert zoomed is not None
        assert _bgra_pixel(zoomed, 1919, 540) == green

    finally:
        ingress.close()
        egress.close()
        program_egress.close()
        engine.stop()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows native media pipeline")
def test_native_image_framing_preserves_aspect_and_normalized_pan(tmp_path) -> None:
    engine = create_native_scene_engine(
        tmp_path,
        repository_root=REPOSITORY_ROOT,
    )
    if engine is None:
        pytest.skip("Built native media engine is unavailable")

    ingress = ContentFrameIngressController(
        maximum_fps=30,
        canvas_width=1920,
        canvas_height=1080,
    )
    egress = SharedMemoryBgraFrameSubscriber(1920, 1080)
    document, content_scene = _content_document()
    try:
        capabilities = engine.start(
            session_id="native-image-framing",
            deadline_ms=10_000,
        ).result(15)
        if not capabilities.local_cameras and not capabilities.rtsp_cameras:
            pytest.skip("Native GStreamer graph is unavailable")
        acknowledged = engine.hydrate(
            SceneEngineSnapshot(
                session_id="native-image-framing",
                sequence=1,
                document=document,
                active_scenes=tuple(
                    (route.bus_id, content_scene.id) for route in document.outputs
                ),
                render_enabled=(
                    (BusId.MEDIA_WINDOWS, True),
                    (BusId.VIRTUAL_CAMERA, True),
                ),
                output_enabled=(
                    (BusId.MEDIA_WINDOWS, True),
                    (BusId.VIRTUAL_CAMERA, False),
                ),
                content_ingress=ingress.descriptor,
                preview_egress=egress.descriptor,
            ),
            request_id="native-image-framing-hydrate",
            deadline_ms=10_000,
        ).result(15)
        assert acknowledged.applied

        ingress.set_image_transform(
            ImageTransform(2.0, 0.25, 0.25),
            media_epoch=1,
            canvas_width=1920,
            canvas_height=1080,
            animate=False,
        )
        ingress.set_media_epoch(1)
        quadrants = _quadrant_image()
        red = bytes((0x20, 0x20, 0xE0, 0xFF))
        green = bytes((0x20, 0xD0, 0x20, 0xFF))
        blue = bytes((0xE0, 0x40, 0x20, 0xFF))
        yellow = bytes((0x20, 0xD0, 0xE0, 0xFF))
        output = None
        deadline = time.monotonic() + 8.0
        while output is None and time.monotonic() < deadline:
            ingress.submit_frame(quadrants)
            output = _wait_for_pixel(
                egress,
                x=200,
                y=900,
                expected=blue,
                timeout=0.1,
            )

        assert output is not None
        samples = [
            (x, y, _bgra_pixel(output, x, y))
            for y in (0, 100, 200, 400, 539, 540, 700, 900, 1079)
            for x in (0, 100, 200, 500, 959, 960, 1400, 1700, 1919)
        ]
        assert _bgra_pixel(output, 200, 200) == red, samples
        assert _bgra_pixel(output, 1700, 200) == green
        assert _bgra_pixel(output, 200, 900) == blue
        assert _bgra_pixel(output, 1700, 900) == yellow
        assert _bgra_pixel(output, 1200, 700) == red

        # Retargeting the same retained image to a centered zoom exercises
        # negative X and Y simultaneously. It must crop proportionally instead
        # of stretching the bottom-right source edge over the destination.
        ingress.set_image_transform(
            ImageTransform(2.0, 0.0, 0.0),
            media_epoch=1,
            canvas_width=1920,
            canvas_height=1080,
            animate=True,
        )
        centered = _wait_for_pixel(
            egress,
            x=1200,
            y=200,
            expected=green,
            timeout=8.0,
        )
        assert centered is not None
        assert _bgra_pixel(centered, 200, 200) == red
        assert _bgra_pixel(centered, 200, 900) == blue
        assert _bgra_pixel(centered, 1200, 700) == yellow
        assert _bgra_pixel(centered, 1200, 900) == yellow
    finally:
        ingress.close()
        egress.close()
        engine.stop()
