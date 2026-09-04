from __future__ import annotations
import sys
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from threading import Event

import pytest
from PySide6.QtCore import QCoreApplication
from PySide6.QtGui import QColor, QImage

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
    NO_SIGNAL_SOURCE_ID,
    SceneDefinition,
    SceneDocument,
    TransitionKind,
    TransitionSpec,
    VideoPixelFormat,
)
from solin.core.scenes.native_engine import create_native_scene_engine
from solin.core.scenes.presets import SceneSeedNames, create_default_scene_document


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _wait_for(
    predicate: Callable[[], bool],
    *,
    application: QCoreApplication | None = None,
    timeout: float = 5.0,
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if application is not None:
            application.processEvents()
        if predicate():
            return True
        time.sleep(1 / 240)
    return False


def _bgra_pixel(frame: VideoFrame, x: int, y: int) -> bytes:
    offset = (y * frame.width + x) * 4
    return frame.pixels[offset : offset + 4]


def _nv12_pixel_is_green(frame: VideoFrame, x: int, y: int) -> bool:
    if frame.pixel_format is not VideoPixelFormat.NV12:
        return False
    luma = frame.pixels[y * frame.width + x]
    chroma_offset = frame.width * frame.height + (y // 2) * frame.width + (x // 2) * 2
    blue_chroma = frame.pixels[chroma_offset]
    red_chroma = frame.pixels[chroma_offset + 1]
    return 130 <= luma <= 190 and 65 <= blue_chroma <= 125 and 20 <= red_chroma <= 90


def _nv12_pixel_is_red(frame: VideoFrame, x: int, y: int) -> bool:
    if frame.pixel_format is not VideoPixelFormat.NV12:
        return False
    luma = frame.pixels[y * frame.width + x]
    chroma_offset = frame.width * frame.height + (y // 2) * frame.width + (x // 2) * 2
    blue_chroma = frame.pixels[chroma_offset]
    red_chroma = frame.pixels[chroma_offset + 1]
    return 45 <= luma <= 115 and 90 <= blue_chroma <= 140 and 170 <= red_chroma <= 245


@contextmanager
def _record_program_centers(
    subscriber: SharedMemoryVideoFrameSubscriber,
) -> Iterator[list[tuple[int, tuple[int, int, int]]]]:
    """Observe egress while the control thread waits for prepare/Take replies."""
    samples: list[tuple[int, tuple[int, int, int]]] = []
    started = Event()
    stopped = Event()

    def record() -> None:
        started.set()
        while not stopped.is_set():
            if not subscriber.wait_for_frame(20):
                continue
            frame = subscriber.read_latest()
            if frame is None:
                continue
            assert frame.pixel_format is VideoPixelFormat.NV12
            x, y = frame.width // 2, frame.height // 2
            chroma = frame.width * frame.height + (y // 2) * frame.width + (x // 2) * 2
            samples.append(
                (
                    frame.sequence,
                    (
                        frame.pixels[y * frame.width + x],
                        frame.pixels[chroma],
                        frame.pixels[chroma + 1],
                    ),
                )
            )

    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="program-frame-recorder") as executor:
        recording = executor.submit(record)
        assert started.wait(2), "Program recorder did not start"
        try:
            yield samples
        finally:
            stopped.set()
            recording.result(2)


def _wait_for_pixel(
    subscriber: SharedMemoryBgraFrameSubscriber,
    *,
    x: int,
    y: int,
    expected: bytes,
    after_sequence: int = 0,
    timeout: float = 5.0,
) -> VideoFrame | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            candidate = subscriber.read_latest()
        except TimeoutError:
            candidate = None
        if candidate is not None and candidate.sequence > after_sequence:
            if _bgra_pixel(candidate, x, y) == expected:
                return candidate
        time.sleep(1 / 120)
    return None


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
                replace(route, default_scene_id=content_scene.id) for route in document.outputs
            ),
        ),
        content_scene,
    )


@pytest.mark.skipif(sys.platform != "win32", reason="Windows native media pipeline")
@pytest.mark.parametrize(
    "retained_kind",
    ["image", "idle"],
    ids=["image-shm", "idle-shm"],
)
def test_return_to_cached_content_never_publishes_the_previous_presentation(
    tmp_path,
    retained_kind: str,
) -> None:
    engine = create_native_scene_engine(tmp_path, repository_root=REPOSITORY_ROOT)
    if engine is None:
        pytest.skip("Built native media engine is unavailable")
    ingress = ContentFrameIngressController(
        maximum_fps=30,
        canvas_width=1920,
        canvas_height=1080,
    )
    descriptor = ingress.descriptor
    preview = SharedMemoryBgraFrameSubscriber(1920, 1080)
    program = SharedMemoryVideoFrameSubscriber(1920, 1080)
    document, content_scene = _content_document()
    away_scene = next(
        scene
        for scene in document.scenes
        if len(scene.layers) == 1 and scene.layers[0].source_id == NO_SIGNAL_SOURCE_ID
    )
    retained = QImage(1280, 720, QImage.Format.Format_ARGB32)
    retained.fill(QColor("#20d020" if retained_kind == "idle" else "#e02020"))
    retained_frame = retained
    next_image = QImage(1280, 720, QImage.Format.Format_ARGB32)
    next_image.fill(QColor("#0000ff"))
    retained_match = _nv12_pixel_is_green if retained_kind == "idle" else _nv12_pixel_is_red
    events: list[object] = []
    unsubscribe = engine.subscribe(events.append)
    prepare_latencies: list[float] = []
    sequence = 1

    def prepare(
        scene_id: str,
        *,
        record_latency: bool = False,
        content_media_epoch: int | None = None,
    ):
        nonlocal sequence
        sequence += 1
        started_at = time.monotonic()
        prepared = engine.prepare_scene(
            BusId.VIRTUAL_CAMERA,
            scene_id,
            transition=TransitionSpec(TransitionKind.CUT, 0),
            document_revision=document.revision,
            request_id=f"cached-content-prepare-{sequence}",
            sequence=sequence,
            deadline_ms=10_000,
            content_media_epoch=content_media_epoch,
        ).result(15)
        if record_latency:
            prepare_latencies.append((time.monotonic() - started_at) * 1_000)
        return prepared

    def take(prepared) -> None:
        nonlocal sequence
        sequence += 1
        assert (
            engine.take_prepared(
                prepared,
                request_id=f"cached-content-take-{sequence}",
                sequence=sequence,
                deadline_ms=10_000,
            )
            .result(15)
            .applied
        )

    def program_matches(match: Callable[[VideoFrame, int, int], bool]) -> bool:
        frame = program.read_latest()
        return frame is not None and match(frame, frame.width // 2, frame.height // 2)

    def is_blue(yuv: tuple[int, int, int]) -> bool:
        luma, blue_chroma, red_chroma = yuv
        return 20 <= luma <= 80 and blue_chroma >= 180 and 80 <= red_chroma <= 160

    def is_black_to_blue(yuv: tuple[int, int, int]) -> bool:
        luma, blue_chroma, red_chroma = yuv
        return luma <= 80 and blue_chroma >= 120 and red_chroma <= 145

    try:
        capabilities = engine.start(
            session_id="native-cached-content-return", deadline_ms=10_000
        ).result(15)
        if not capabilities.local_cameras and not capabilities.rtsp_cameras:
            pytest.skip("Native GStreamer graph is unavailable")
        ingress.begin_presentation(1)
        ingress.submit_frame(retained_frame)
        publisher = ingress._publisher
        assert publisher is not None
        assert _wait_for(
            lambda: int(getattr(publisher, "_sequence", 0)) > 0
        ), "Initial retained presentation was not published"
        assert (
            engine.hydrate(
                SceneEngineSnapshot(
                    session_id="native-cached-content-return",
                    sequence=sequence,
                    document=document,
                    active_scenes=tuple(
                        (route.bus_id, content_scene.id) for route in document.outputs
                    ),
                    render_enabled=((BusId.MEDIA_WINDOWS, True), (BusId.VIRTUAL_CAMERA, True)),
                    output_enabled=((BusId.MEDIA_WINDOWS, True), (BusId.VIRTUAL_CAMERA, False)),
                    content_ingress=descriptor,
                    preview_egress=preview.descriptor,
                    program_egress=program.descriptor,
                ),
                request_id="cached-content-hydrate",
                deadline_ms=10_000,
            )
            .result(15)
            .applied
        )

        for cycle in range(3):
            if cycle:
                ingress.begin_presentation(cycle * 2 + 1)
                ingress.submit_frame(retained_frame)
            assert _wait_for(lambda: program_matches(retained_match)), events

            take(prepare(away_scene.id))
            assert _wait_for(
                lambda: program_matches(lambda frame, x, y: frame.pixels[y * frame.width + x] <= 24)
            ), events

            # Production auto switch starts preparing Program as soon as the
            # presentation identity changes. The first pixels are published by
            # the content controller in the same turn, but Raw still has to
            # retire the previous presentation before those pixels are safe to
            # expose. Waiting for Preview to finish that transition here hides
            # the warm-graph race seen by the application.
            ingress.begin_presentation(cycle * 2 + 2)
            ingress.submit_frame(next_image)
            assert ingress.descriptor == descriptor

            with _record_program_centers(program) as samples:
                # Delaying or draining the Qt loop here masks the race. Take
                # immediately after preparation, as production auto switch does.
                prepared = prepare(
                    content_scene.id,
                    record_latency=True,
                    content_media_epoch=cycle * 2 + 2,
                )
                take(prepared)
                assert _wait_for(lambda: any(is_blue(yuv) for _, yuv in tuple(samples))), (
                    cycle,
                    samples,
                    events,
                )
                time.sleep(0.1)
            assert samples, "Program emitted no frames during the cached return"
            assert all(is_black_to_blue(yuv) for _, yuv in samples), (
                f"cached {retained_kind} reappeared on return {cycle}: {samples!r}"
            )
            sequences = [frame_sequence for frame_sequence, _ in samples]
            assert sequences == sorted(set(sequences)), (
                f"Program egress sequence regressed or repeated: {sequences!r}"
            )
        assert prepare_latencies and max(prepare_latencies) < 1_500, prepare_latencies
    finally:
        ingress.close()
        preview.close()
        program.close()
        unsubscribe()
        engine.stop()


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
    events: list[object] = []
    unsubscribe = engine.subscribe(events.append)

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
            active_scenes=tuple((route.bus_id, content_scene.id) for route in document.outputs),
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
                offset = ((candidate.height // 2) * candidate.width + candidate.width // 2) * 4
                center_pixel = candidate.pixels[offset : offset + 4]
                if center_pixel == bytes((0x56, 0x34, 0x12, 0xFF)):
                    break
            time.sleep(1 / 60)

        assert latest is not None, events
        assert (latest.width, latest.height) == (1920, 1080)
        assert center_pixel == bytes((0x56, 0x34, 0x12, 0xFF))

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
        assert (
            _wait_for_pixel(
                egress,
                x=960,
                y=540,
                expected=bytes((0x56, 0x34, 0x12, 0xFF)),
            )
            is not None
        )

        four_by_three = QImage(800, 600, QImage.Format.Format_ARGB32)
        four_by_three.fill(QColor("#20d020"))
        ingress.submit_frame(four_by_three)
        latest = _wait_for_pixel(
            egress,
            x=960,
            y=540,
            expected=bytes((0x20, 0xD0, 0x20, 0xFF)),
        )
        publisher = ingress._publisher
        publisher_sequence = getattr(publisher, "_sequence", None)
        assert latest is not None, (publisher_sequence, events)
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

        # Image framing is authored once on the canonical Raw source. A 4:3
        # image normally has black side bars in the 16:9 scene; zooming it to
        # 2x must cover the edge in the composed output without republishing
        # transformed pixels from Python.
        ingress.begin_presentation(1)
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

        ingress.begin_presentation(2)
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

        # Arming a future presentation without supplying its first pixels must
        # preserve the currently published presentation. Identity becomes
        # visible atomically with a frame, so there is no black watchdog state
        # and no way to relabel the previous image as the future epoch.
        ingress.begin_presentation(3)
        time.sleep(0.25)
        try:
            still_preserved = egress.read_latest()
        except TimeoutError:
            still_preserved = None
        if still_preserved is not None:
            assert _bgra_pixel(still_preserved, 960, 540) == green

        # The first frame commits the armed epoch and starts its transition from
        # the preserved owner.
        ingress.submit_frame(sixteen_by_nine)
        late = _wait_for_pixel(
            egress,
            x=960,
            y=540,
            expected=yellow,
            timeout=8.0,
        )
        assert late is not None
        assert late.sequence > zoomed.sequence

        # Recovery and deferred replay must leave the logical owner healthy so
        # another playback request can still complete normally.
        ingress.set_image_transform(
            ImageTransform(1.0, 0.0, 0.0),
            media_epoch=4,
            canvas_width=1920,
            canvas_height=1080,
            animate=False,
        )
        ingress.begin_presentation(4)
        ingress.submit_frame(four_by_three)
        resumed = _wait_for_pixel(
            egress,
            x=960,
            y=540,
            expected=green,
            timeout=8.0,
        )
        assert resumed is not None
        assert resumed.sequence > late.sequence

    finally:
        ingress.close()
        egress.close()
        program_egress.close()
        unsubscribe()
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
                active_scenes=tuple((route.bus_id, content_scene.id) for route in document.outputs),
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
        ingress.begin_presentation(1)
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
