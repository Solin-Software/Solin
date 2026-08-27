from __future__ import annotations
import ctypes
import mmap
import os
import struct
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import replace
from pathlib import Path

import pytest
from PySide6.QtCore import QCoreApplication, QSize, Qt, QUrl
from PySide6.QtGui import QColor, QImage
from PySide6.QtMultimedia import QMediaPlayer, QVideoFrame, QVideoFrameFormat, QVideoSink

from solin.controllers.content_frame_ingress_controller import (
    ContentFrameIngressController,
)
from solin.core.projection.image_framing import ImageTransform
from solin.core.scenes.engine import FrameChannelTransport, SceneEngineSnapshot
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


@contextmanager
def _stale_d3d11_reader_leases(handle_token: str) -> Iterator[None]:
    """Model retained frames from the previous texture generation."""
    if sys.platform != "win32":
        yield
        return

    import ctypes.wintypes as wintypes

    mapping = mmap.mmap(
        -1,
        640,
        tagname=f"Local\\SolinD3D11Frame.{handle_token}",
        access=mmap.ACCESS_WRITE,
    )
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenMutexW.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)
    kernel32.OpenMutexW.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.ReleaseMutex.argtypes = (wintypes.HANDLE,)
    kernel32.ReleaseMutex.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.GetProcessTimes.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    )
    kernel32.GetProcessTimes.restype = wintypes.BOOL

    mutex = kernel32.OpenMutexW(
        0x00100001,
        False,
        f"Local\\SolinD3D11FrameMutex.{handle_token}",
    )
    if not mutex:
        mapping.close()
        raise ctypes.WinError(ctypes.get_last_error())

    creation = wintypes.FILETIME()
    exit_time = wintypes.FILETIME()
    kernel_time = wintypes.FILETIME()
    user_time = wintypes.FILETIME()
    if not kernel32.GetProcessTimes(
        wintypes.HANDLE(-1),
        ctypes.byref(creation),
        ctypes.byref(exit_time),
        ctypes.byref(kernel_time),
        ctypes.byref(user_time),
    ):
        kernel32.CloseHandle(mutex)
        mapping.close()
        raise ctypes.WinError(ctypes.get_last_error())
    process_creation = creation.dwLowDateTime | (creation.dwHighDateTime << 32)
    process_id = os.getpid()
    protocol_version = struct.unpack_from("<H", mapping, 8)[0]
    resource_generation = struct.unpack_from("<Q", mapping, 64)[0]
    if protocol_version != 2 or resource_generation == 0:
        kernel32.CloseHandle(mutex)
        mapping.close()
        raise RuntimeError("D3D11 channel protocol is not initialized")
    stale_reader_state = (resource_generation << 2) | 1
    available_state = resource_generation << 2

    def write_stale_leases() -> None:
        if kernel32.WaitForSingleObject(mutex, 1_000) != 0:
            raise RuntimeError("D3D11 channel mutex is unavailable")
        try:
            for slot in range(3):
                base = 256 + slot * 128
                struct.pack_into("<Q", mapping, base + 48, stale_reader_state)
                struct.pack_into("<I", mapping, base + 56, process_id)
                struct.pack_into("<Q", mapping, base + 64, process_creation)
        finally:
            kernel32.ReleaseMutex(mutex)

    def clear_stale_leases() -> None:
        if kernel32.WaitForSingleObject(mutex, 1_000) != 0:
            return
        try:
            for slot in range(3):
                base = 256 + slot * 128
                lease_state = struct.unpack_from("<Q", mapping, base + 48)[0]
                owner_id = struct.unpack_from("<I", mapping, base + 56)[0]
                owner_creation = struct.unpack_from("<Q", mapping, base + 64)[0]
                if (
                    lease_state == stale_reader_state
                    and owner_id == process_id
                    and owner_creation == process_creation
                ):
                    struct.pack_into("<Q", mapping, base + 48, available_state)
        finally:
            kernel32.ReleaseMutex(mutex)

    try:
        write_stale_leases()
        yield
    finally:
        clear_stale_leases()
        kernel32.CloseHandle(mutex)
        mapping.close()


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


def _wait_for_raw_and_program_color(
    raw: SharedMemoryBgraFrameSubscriber,
    program: SharedMemoryVideoFrameSubscriber,
    *,
    raw_bgra: bytes,
    program_match: Callable[[VideoFrame, int, int], bool],
    raw_after: int = 0,
    program_after: int = 0,
    raw_samples: list[bytes] | None = None,
    timeout: float = 5.0,
) -> tuple[VideoFrame | None, VideoFrame | None]:
    raw_output = None
    program_output = None
    sampled_raw_sequence = raw_after
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            raw_candidate = raw.read_latest()
        except TimeoutError:
            raw_candidate = None
        try:
            program_candidate = program.read_latest()
        except TimeoutError:
            program_candidate = None
        if (
            raw_samples is not None
            and raw_candidate is not None
            and raw_candidate.sequence > sampled_raw_sequence
        ):
            sampled_raw_sequence = raw_candidate.sequence
            raw_samples.append(
                _bgra_pixel(
                    raw_candidate,
                    raw_candidate.width // 2,
                    raw_candidate.height // 2,
                )
            )
        if (
            raw_candidate is not None
            and raw_candidate.sequence > raw_after
            and _bgra_pixel(
                raw_candidate,
                raw_candidate.width // 2,
                raw_candidate.height // 2,
            )
            == raw_bgra
        ):
            raw_output = raw_candidate
        if (
            program_candidate is not None
            and program_candidate.sequence > program_after
            and program_match(
                program_candidate,
                program_candidate.width // 2,
                program_candidate.height // 2,
            )
        ):
            program_output = program_candidate
        if raw_output is not None and program_output is not None:
            break
        time.sleep(1 / 240)
    return raw_output, program_output


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
                replace(route, default_scene_id=content_scene.id) for route in document.outputs
            ),
        ),
        content_scene,
    )


@pytest.mark.skipif(sys.platform != "win32", reason="Windows native media pipeline")
@pytest.mark.parametrize(
    "media_mirror_enabled",
    [False, True],
    ids=["media-mirror-off", "media-mirror-on"],
)
def test_hardware_decoded_qt_frame_stays_on_gpu_until_composition(
    tmp_path,
    media_mirror_enabled: bool,
) -> None:
    video_path = Path(os.environ.get("SOLIN_QT_BRIDGE_TEST_VIDEO", ""))
    alternate_video_path = Path(os.environ.get("SOLIN_QT_BRIDGE_TEST_VIDEO_ALTERNATE", ""))
    if not video_path.is_file():
        pytest.skip("SOLIN_QT_BRIDGE_TEST_VIDEO does not identify a decoder fixture")
    engine = create_native_scene_engine(
        tmp_path,
        repository_root=REPOSITORY_ROOT,
    )
    if engine is None:
        pytest.skip("Built native media engine is unavailable")

    application = QCoreApplication.instance() or QCoreApplication([])
    ingress = ContentFrameIngressController(
        maximum_fps=30,
        canvas_width=1920,
        canvas_height=1080,
    )
    descriptor = ingress.descriptor
    if descriptor is None or descriptor.transport is not FrameChannelTransport.D3D11_SHARED_TEXTURE:
        ingress.close()
        engine.stop()
        pytest.skip("Built Qt D3D11 bridge is unavailable")
    egress = SharedMemoryBgraFrameSubscriber(1920, 1080)
    program_egress = SharedMemoryVideoFrameSubscriber(1920, 1080)
    document, content_scene = _content_document()
    descriptor_changes: list[object] = []
    ingress.descriptor_changed.connect(
        descriptor_changes.append,
        Qt.ConnectionType.DirectConnection,
    )
    sink = QVideoSink()
    player = QMediaPlayer()
    player.setVideoSink(sink)
    assert ingress.bind_video_sink(sink)
    sink.videoFrameChanged.connect(ingress.submit_frame)
    ingress.set_decoder_frame_gate(1, True)
    ingress.begin_presentation(1)
    events: list[object] = []
    unsubscribe = engine.subscribe(events.append)
    try:
        capabilities = engine.start(
            session_id="native-qt-gpu-ingress",
            deadline_ms=10_000,
        ).result(15)
        assert capabilities.d3d11_shared_textures
        initial_snapshot = SceneEngineSnapshot(
            session_id="native-qt-gpu-ingress",
            sequence=1,
            document=document,
            active_scenes=tuple((route.bus_id, content_scene.id) for route in document.outputs),
            render_enabled=(
                (BusId.MEDIA_WINDOWS, True),
                (BusId.VIRTUAL_CAMERA, True),
            ),
            output_enabled=(
                (BusId.MEDIA_WINDOWS, media_mirror_enabled),
                (BusId.VIRTUAL_CAMERA, False),
            ),
            content_ingress=descriptor,
            preview_egress=egress.descriptor,
            program_egress=program_egress.descriptor,
        )
        acknowledged = engine.hydrate(
            initial_snapshot,
            request_id="native-qt-gpu-ingress-hydrate",
            deadline_ms=10_000,
        ).result(15)
        assert acknowledged.applied

        player.setSource(QUrl.fromLocalFile(str(video_path.resolve())))
        player.play()
        output = None
        deadline = time.monotonic() + 12.0
        while time.monotonic() < deadline:
            application.processEvents()
            try:
                candidate = egress.read_latest()
            except TimeoutError:
                candidate = None
            visible = candidate is not None and any(
                _bgra_pixel(candidate, x, y)[:3] != bytes(3)
                for x in (candidate.width // 4, candidate.width // 2, candidate.width * 3 // 4)
                for y in (candidate.height // 4, candidate.height // 2, candidate.height * 3 // 4)
            )
            if visible:
                output = candidate
                break
            time.sleep(1 / 240)

        bridge = ingress._accelerated_publisher
        status = bridge.status() if bridge is not None else None
        assert output is not None, (status, events, player.errorString())
        assert ingress.descriptor == descriptor
        assert descriptor_changes == []

        # Hydration deliberately completed before the decoder published its first
        # frame. Program must leave that deferred-preroll state at the producer's
        # cadence instead of falling into its bounded recovery retry interval.
        program_sequences: set[int] = set()
        cadence_deadline = time.monotonic() + 2.0
        while time.monotonic() < cadence_deadline:
            application.processEvents()
            try:
                program_candidate = program_egress.read_latest()
            except TimeoutError:
                program_candidate = None
            if program_candidate is not None:
                program_sequences.add(program_candidate.sequence)
            time.sleep(1 / 240)
        assert 30 <= len(program_sequences) <= 75, (
            len(program_sequences),
            events,
        )

        default_scene = next(
            scene
            for scene in document.scenes
            if len(scene.layers) == 1
            and scene.layers[0].source_id == NO_SIGNAL_SOURCE_ID
        )

        # Replacing the decoder resource does not replace the logical ingress
        # channel. When an alternate fixture is supplied, require the native
        # texture generation and the engine output to advance across a real
        # source-size change. This guards the cached GstD3D11Memory layout.
        if alternate_video_path.is_file():
            assert bridge is not None and status is not None
            initial_resource_generation = int(status["resource_generation"])
            initial_resource_size = (
                int(status["resource_width"]),
                int(status["resource_height"]),
            )
            player.stop()
            player.setSource(QUrl.fromLocalFile(str(alternate_video_path.resolve())))
            player.play()
            replacement_output = None
            replacement_status = None
            deadline = time.monotonic() + 12.0
            while time.monotonic() < deadline:
                application.processEvents()
                replacement_status = bridge.status()
                try:
                    candidate = egress.read_latest()
                except TimeoutError:
                    candidate = None
                if (
                    candidate is not None
                    and candidate.sequence > output.sequence
                    and int(replacement_status["resource_generation"]) > initial_resource_generation
                    and (
                        int(replacement_status["resource_width"]),
                        int(replacement_status["resource_height"]),
                    )
                    != initial_resource_size
                ):
                    replacement_output = candidate
                    break
                time.sleep(1 / 240)
            assert replacement_output is not None, (
                initial_resource_size,
                replacement_status,
                events,
                player.errorString(),
            )
            assert ingress.descriptor == descriptor
            assert descriptor_changes == []

        # Reproduce the production auto-return before a static projection takes
        # Program again. Video, still image and idle are presentations of one
        # logical content source; changing media must not replace its descriptor,
        # runtime or scene graphs.
        away = engine.prepare_scene(
            BusId.VIRTUAL_CAMERA,
            default_scene.id,
            transition=TransitionSpec(TransitionKind.FADE_TO_BLACK, 200),
            document_revision=document.revision,
            request_id="native-qt-leave-content",
            sequence=2,
            deadline_ms=10_000,
        ).result(15)
        assert engine.take_prepared(
            away,
            request_id="native-qt-leave-content-take",
            sequence=3,
            deadline_ms=10_000,
        ).result(15).applied

        # A decoder callback can arrive after playback has been gated while the
        # static projection is taking ownership of Program. The image must be
        # committed to the same dynamic D3D11 channel, and that late GPU frame
        # must never restore the previous video's texture generation.
        late_decoder_frame = QVideoFrame(sink.videoFrame())
        assert late_decoder_frame.isValid()
        ingress.set_decoder_frame_gate(2, False)
        player.stop()
        application.processEvents()
        ingress.begin_presentation(2)
        ingress.set_image_transform(
            ImageTransform(1.0, 0.0, 0.0),
            media_epoch=2,
            canvas_width=1920,
            canvas_height=1080,
            animate=False,
        )
        static_image = QImage(1280, 720, QImage.Format.Format_ARGB32)
        static_image.fill(QColor("#23c45e"))
        ingress.submit_frame(static_image)

        returned = engine.prepare_scene(
            BusId.VIRTUAL_CAMERA,
            content_scene.id,
            transition=TransitionSpec(
                TransitionKind.FADE_TO_BLACK,
                200,
            ),
            document_revision=document.revision,
            request_id="native-qt-return-to-content",
            sequence=4,
            deadline_ms=10_000,
        ).result(15)
        assert engine.take_prepared(
            returned,
            request_id="native-qt-return-to-content-take",
            sequence=5,
            deadline_ms=10_000,
        ).result(15).applied

        static_output = None
        static_program_output = None
        last_static_center = None
        last_program_yuv = None
        static_descriptor = None
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            application.processEvents()
            static_descriptor = ingress.descriptor
            try:
                candidate = egress.read_latest()
            except TimeoutError:
                candidate = None
            try:
                program_candidate = program_egress.read_latest()
            except TimeoutError:
                program_candidate = None
            if (
                candidate is not None
                and _bgra_pixel(candidate, candidate.width // 2, candidate.height // 2)
                == bytes((0x5E, 0xC4, 0x23, 0xFF))
                and static_descriptor == descriptor
            ):
                static_output = candidate
            if candidate is not None:
                last_static_center = _bgra_pixel(
                    candidate,
                    candidate.width // 2,
                    candidate.height // 2,
                )
            if program_candidate is not None and _nv12_pixel_is_green(
                program_candidate,
                program_candidate.width // 2,
                program_candidate.height // 2,
            ):
                static_program_output = program_candidate
            if (
                program_candidate is not None
                and program_candidate.pixel_format is VideoPixelFormat.NV12
            ):
                x = program_candidate.width // 2
                y = program_candidate.height // 2
                chroma_offset = (
                    program_candidate.width * program_candidate.height
                    + (y // 2) * program_candidate.width
                    + (x // 2) * 2
                )
                last_program_yuv = (
                    program_candidate.pixels[y * program_candidate.width + x],
                    program_candidate.pixels[chroma_offset],
                    program_candidate.pixels[chroma_offset + 1],
                )
            if static_output is not None and static_program_output is not None:
                break
            time.sleep(1 / 240)

        assert static_output is not None, (
            f"raw_center={last_static_center!r}, program_yuv={last_program_yuv!r}, "
            f"transport={getattr(static_descriptor, 'transport', None)!r}"
        )
        assert static_program_output is not None, (
            f"raw_center={last_static_center!r}, program_yuv={last_program_yuv!r}, "
            f"transport={getattr(static_descriptor, 'transport', None)!r}, "
            f"events={events!r}"
        )
        assert static_descriptor == descriptor
        assert descriptor_changes == []

        ingress.submit_frame(late_decoder_frame)
        deadline = time.monotonic() + 0.25
        while time.monotonic() < deadline:
            application.processEvents()
            time.sleep(1 / 240)
        assert ingress.descriptor == static_descriptor
        assert descriptor_changes == []

        # Closing and reopening the same image does not change the D3D11
        # descriptor. Each media epoch must nevertheless advance both consumers;
        # neither graph may retain the old decoder texture or an intermediate
        # black transition frame.
        last_raw_sequence = static_output.sequence
        last_program_sequence = static_program_output.sequence
        idle_image = QImage(1024, 768, QImage.Format.Format_ARGB32)
        idle_image.fill(QColor("#d12d3f"))
        for cycle in range(3):
            idle_epoch = 3 + cycle * 2
            retained_leases = (
                _stale_d3d11_reader_leases(descriptor.handle_token) if cycle == 0 else nullcontext()
            )
            with retained_leases:
                ingress.begin_presentation(idle_epoch)
                ingress.set_image_transform(
                    None,
                    media_epoch=idle_epoch,
                    canvas_width=1920,
                    canvas_height=1080,
                    animate=False,
                )
                ingress.submit_frame(idle_image)
                idle_raw, idle_program = _wait_for_raw_and_program_color(
                    egress,
                    program_egress,
                    raw_bgra=bytes((0x3F, 0x2D, 0xD1, 0xFF)),
                    program_match=_nv12_pixel_is_red,
                    raw_after=last_raw_sequence,
                    program_after=last_program_sequence,
                )
            assert idle_raw is not None, (cycle, events)
            assert idle_program is not None, (cycle, events)
            last_raw_sequence = idle_raw.sequence
            last_program_sequence = idle_program.sequence

            image_epoch = idle_epoch + 1
            ingress.begin_presentation(image_epoch)
            ingress.set_image_transform(
                ImageTransform(1.0, 0.0, 0.0),
                media_epoch=image_epoch,
                canvas_width=1920,
                canvas_height=1080,
                animate=False,
            )
            ingress.submit_frame(static_image)
            ingress.submit_frame(late_decoder_frame)
            raw_transition_samples: list[bytes] = []
            image_raw, image_program = _wait_for_raw_and_program_color(
                egress,
                program_egress,
                raw_bgra=bytes((0x5E, 0xC4, 0x23, 0xFF)),
                program_match=_nv12_pixel_is_green,
                raw_after=last_raw_sequence,
                program_after=last_program_sequence,
                raw_samples=raw_transition_samples,
            )
            assert image_raw is not None, (cycle, events)
            assert image_program is not None, (cycle, events)
            assert any(
                max(pixel[:3]) < 48 for pixel in raw_transition_samples
            ), (cycle, raw_transition_samples, events)
            last_raw_sequence = image_raw.sequence
            last_program_sequence = image_program.sequence
            assert ingress.descriptor == static_descriptor
            assert descriptor_changes == []

        # Re-enter the GPU decoder after repeated static presentations, then
        # leave it while a physical Raw consumer may be active. The second
        # video must advance the dynamic resource without replacing its source
        # identity, and the Program fade
        # must retain its outgoing texture until the Take completes.
        ingress.set_decoder_frame_gate(3, True)
        ingress.begin_presentation(9)
        player.setSource(QUrl.fromLocalFile(str(video_path.resolve())))
        player.play()
        video_descriptor = descriptor
        deadline = time.monotonic() + 12.0
        while time.monotonic() < deadline:
            application.processEvents()
            if ingress.descriptor == descriptor and bridge is not None and int(
                bridge.status().get("resource_generation", 0)
            ) > 0:
                break
            time.sleep(1 / 240)
        assert ingress.descriptor == video_descriptor
        assert descriptor_changes == []

        video_program = None
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            application.processEvents()
            try:
                candidate = program_egress.read_latest()
            except TimeoutError:
                candidate = None
            if candidate is not None and candidate.sequence > last_program_sequence:
                video_program = candidate
                break
            time.sleep(1 / 240)
        assert video_program is not None, (bridge.status() if bridge else None, events)

        sample_points = tuple(
            (x, y)
            for x in (
                video_program.width // 4,
                video_program.width // 2,
                video_program.width * 3 // 4,
            )
            for y in (
                video_program.height // 4,
                video_program.height // 2,
                video_program.height * 3 // 4,
            )
        )
        sample_x, sample_y = max(
            sample_points,
            key=lambda point: video_program.pixels[
                point[1] * video_program.width + point[0]
            ],
        )
        outgoing_luma = video_program.pixels[
            sample_y * video_program.width + sample_x
        ]

        ingress.set_decoder_frame_gate(4, False)
        player.stop()
        application.processEvents()
        ingress.begin_presentation(10)
        ingress.set_image_transform(
            None,
            media_epoch=10,
            canvas_width=1920,
            canvas_height=1080,
            animate=False,
        )
        ingress.submit_frame(idle_image)
        assert _wait_for(
            lambda: ingress.descriptor == static_descriptor,
            application=application,
            timeout=5.0,
        )

        leave_second_video = engine.prepare_scene(
            BusId.VIRTUAL_CAMERA,
            default_scene.id,
            transition=TransitionSpec(TransitionKind.FADE_TO_BLACK, 200),
            document_revision=document.revision,
            request_id="native-qt-leave-second-video",
            sequence=8,
            deadline_ms=10_000,
        ).result(15)
        assert engine.take_prepared(
            leave_second_video,
            request_id="native-qt-leave-second-video-take",
            sequence=9,
            deadline_ms=10_000,
        ).result(15).applied

        transition_lumas: list[int] = []
        deadline = time.monotonic() + 0.8
        after_sequence = video_program.sequence
        while time.monotonic() < deadline:
            try:
                candidate = program_egress.read_latest()
            except TimeoutError:
                candidate = None
            if candidate is not None and candidate.sequence > after_sequence:
                after_sequence = candidate.sequence
                transition_lumas.append(
                    candidate.pixels[sample_y * candidate.width + sample_x]
                )
            time.sleep(1 / 240)
        assert any(
            18 < luma < outgoing_luma - 4 for luma in transition_lumas
        ), (outgoing_luma, transition_lumas, events)

        # Complete enough arbitrary A/B switches to exceed the former global
        # retired-graph history. Every destination is still prepared normally;
        # completed pipelines must be reclaimed instead of accumulating worker
        # threads and GPU resources across the session.
        command_sequence = 11
        for cycle in range(18):
            destination = content_scene if cycle % 2 == 0 else default_scene
            prepared = engine.prepare_scene(
                BusId.VIRTUAL_CAMERA,
                destination.id,
                transition=TransitionSpec(TransitionKind.DISSOLVE, 50),
                document_revision=document.revision,
                request_id=f"native-qt-bounded-switch-{cycle}",
                sequence=command_sequence,
                deadline_ms=10_000,
            ).result(15)
            command_sequence += 1
            assert engine.take_prepared(
                prepared,
                request_id=f"native-qt-bounded-switch-{cycle}-take",
                sequence=command_sequence,
                deadline_ms=10_000,
            ).result(15).applied
            command_sequence += 1
            deadline = time.monotonic() + 0.08
            while time.monotonic() < deadline:
                application.processEvents()
                time.sleep(1 / 240)

        metrics = engine.metrics
        assert metrics.timeout_count == 0
        assert metrics.rejected_count == 0
        assert metrics.restart_count == 0
    finally:
        player.stop()
        application.processEvents()
        ingress.close()
        egress.close()
        program_egress.close()
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
        enable_accelerated=False,
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
                offset = ((candidate.height // 2) * candidate.width + candidate.width // 2) * 4
                center_pixel = candidate.pixels[offset : offset + 4]
                if len(center_pixel) == 4 and min(center_pixel[:3]) >= 245:
                    break
            time.sleep(1 / 60)
        assert min(center_pixel[:3]) >= 245

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
        enable_accelerated=False,
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
