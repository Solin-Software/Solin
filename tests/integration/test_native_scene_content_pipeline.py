from __future__ import annotations
import time
import struct
import re
import subprocess
import sys
import textwrap
from collections import Counter, deque
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from pprint import pformat
from queue import Empty, SimpleQueue
from tempfile import TemporaryDirectory
from threading import Event

import pytest
from PySide6.QtCore import QCoreApplication, QObject, Signal
from PySide6.QtGui import QColor, QImage
from PIL import Image

from solin.controllers.content_frame_ingress_controller import (
    ContentFrameIngressController,
)
from solin.controllers.scene_runtime_controller import SceneRuntimeController
from solin.controllers.program_content_controller import ProgramContentController
from solin.core.foundation.runtime_paths import ProfilePaths
from solin.core.projection.application import ProjectionSession
from solin.core.projection.image_framing import ImageTransform
from solin.controllers.shared_memory_preview_egress import SharedMemoryPreviewEgressController
from solin.core.scenes.content_frame_channel import (
    SharedFrameChannelReader,
    SharedFrameChannelWriter,
    _SEQ_OFFSET,
)
from solin.core.scenes.content_frame_publisher import SharedMemoryContentPublisher
from solin.core.scenes.engine import (
    FrameEgressReadyEvent,
    MediaPlaybackEvent,
    SceneEngine,
    SceneEngineSnapshot,
)
from solin.core.scenes.libobs_engine import create_libobs_scene_engine
from solin.core.scenes.media_control import ContentSourceKind, MediaControlAction, MediaPlaybackState
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
from solin.core.scenes.presets import SceneSeedNames, create_default_scene_document


@dataclass(frozen=True, slots=True)
class _BgraFrame:
    sequence: int
    width: int
    height: int
    pixels: bytes
    pixel_format: VideoPixelFormat = VideoPixelFormat.BGRA


@dataclass(slots=True)
class _PixelWaitProbe:
    read_attempts: int = 0
    read_timeouts: int = 0
    frames: int = 0
    last_frame: _BgraFrame | None = None
    empty_reads: Counter[str] = field(default_factory=Counter)
    channel_samples: deque[tuple[int, int, int]] = field(default_factory=lambda: deque(maxlen=8))
    read_total_ms: float = 0.0
    read_maximum_ms: float = 0.0

    def read_latest(self, subscriber: _BgraEgress) -> _BgraFrame | None:
        self.read_attempts += 1
        sequence_before = subscriber.channel_sequence()
        started = time.perf_counter()
        timed_out = False
        try:
            candidate = subscriber.read_latest()
        except TimeoutError:
            self.read_timeouts += 1
            timed_out = True
            candidate = None
        elapsed_ms = (time.perf_counter() - started) * 1000
        self.read_total_ms += elapsed_ms
        self.read_maximum_ms = max(self.read_maximum_ms, elapsed_ms)
        if candidate is not None:
            self.frames += 1
            self.last_frame = candidate
        else:
            sequence_after = subscriber.channel_sequence()
            last_delivered = subscriber._reader._last_seq
            sample = (sequence_before, sequence_after, last_delivered)
            if not self.channel_samples or self.channel_samples[-1] != sample:
                self.channel_samples.append(sample)
            # These snapshots bracket the public read, not its internal copy.
            # A stable unmatched header does not prove why that copy was rejected.
            if timed_out:
                return None
            if sequence_before != sequence_after:
                reason = "header_changed_during_read"
            elif sequence_before == 0:
                reason = "not_published"
            elif sequence_before & 1:
                reason = "observed_odd_sequence"
            elif sequence_before == last_delivered:
                reason = "already_delivered"
            else:
                reason = "stable_unaccepted"
            self.empty_reads[reason] += 1
        return candidate

    def observation(self, *, aspect_ratio: bool = False) -> dict[str, object]:
        frame = self.last_frame
        points = ((200, 900), (200, 200), (1700, 200), (1700, 900), (1200, 700))
        if aspect_ratio and frame is not None:
            points = ((frame.width // 2, frame.height // 2), (0, frame.height // 2),
                      (239, frame.height // 2), (240, frame.height // 2),
                      (1679, frame.height // 2), (1680, frame.height // 2),
                      (frame.width - 1, frame.height // 2))
        return {
            "read_attempts": self.read_attempts,
            "read_timeouts": self.read_timeouts,
            "frames": self.frames,
            "read_total_ms": round(self.read_total_ms, 3),
            "read_maximum_ms": round(self.read_maximum_ms, 3),
            "empty_reads": dict(self.empty_reads),
            "channel_samples_before_after_last": list(self.channel_samples),
            "last_frame": None if frame is None else {
                "sequence": frame.sequence,
                "size": (frame.width, frame.height),
                "pixels": [
                    (x, y, _bgra_pixel(frame, x, y).hex())
                    for x, y in points
                ],
            },
        }


class _BgraEgress:
    """Observe the app-owned libobs egress without depending on Qt dispatch."""

    def __init__(self, width: int, height: int, *, channel_id: str) -> None:
        self._controller = SharedMemoryPreviewEgressController(width, height, channel_id=channel_id)
        descriptor = self._controller.descriptor
        assert descriptor is not None
        self.descriptor = descriptor
        self._reader = SharedFrameChannelReader(descriptor.handle_token, width, height)

    def read_latest(self) -> _BgraFrame | None:
        frame = self._reader.read_latest()
        if frame is None:
            return None
        assert frame.stride == frame.width * 4
        return _BgraFrame(
            sequence=self._reader._last_seq,
            width=frame.width,
            height=frame.height,
            pixel_format=VideoPixelFormat.BGRA,
            pixels=frame.data,
        )

    def channel_sequence(self) -> int:
        assert self._reader._buf is not None
        return struct.unpack_from("<Q", self._reader._buf, _SEQ_OFFSET)[0]

    def close(self) -> None:
        self._reader.close()
        self._controller.close()


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


def _bgra_pixel(frame: _BgraFrame, x: int, y: int) -> bytes:
    offset = (y * frame.width + x) * 4
    return frame.pixels[offset : offset + 4]


def _program_pixel_matches(pixel: bytes, expected: bytes) -> bool:
    # The main mix uses limited-range NV12; its BGRA mirror has the rounding
    # error of an RGB -> YUV -> RGB conversion. Editor BGRA stays lossless.
    return pixel[3] == expected[3] and all(
        abs(actual - target) <= 6 for actual, target in zip(pixel[:3], expected[:3], strict=True)
    )


@contextmanager
def _record_program_centers(
    subscriber: _BgraEgress,
    *, sample_times: list[float] | None = None,
) -> Iterator[list[tuple[int, bytes]]]:
    """Observe egress while the control thread waits for prepare/Take replies."""
    samples: list[tuple[int, bytes]] = []
    descriptor = subscriber.descriptor
    reader = SharedFrameChannelReader(descriptor.handle_token, descriptor.width, descriptor.height)
    started = Event()
    stopped = Event()

    def record() -> None:
        started.set()
        while not stopped.is_set():
            frame = reader.read_latest()
            if frame is None:
                stopped.wait(1 / 240)
                continue
            x, y = frame.width // 2, frame.height // 2
            offset = (y * frame.width + x) * 4
            samples.append((reader._last_seq, frame.data[offset:offset + 4]))
            if sample_times is not None:
                sample_times.append(time.monotonic())

    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="program-frame-recorder") as executor:
        recording = executor.submit(record)
        assert started.wait(2), "Program recorder did not start"
        try:
            yield samples
        finally:
            stopped.set()
            recording.result(2)
            reader.close()


def _wait_for_pixel(
    subscriber: _BgraEgress,
    *,
    x: int,
    y: int,
    expected: bytes,
    after_sequence: int = 0,
    timeout: float = 5.0,
    probe: _PixelWaitProbe | None = None,
) -> _BgraFrame | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            candidate = subscriber.read_latest() if probe is None else probe.read_latest(subscriber)
        except TimeoutError:
            candidate = None
        if candidate is not None and candidate.sequence > after_sequence:
            if _bgra_pixel(candidate, x, y) == expected:
                return candidate
        time.sleep(1 / 120)
    return None


def _image_framing_ingress_observation(
    ingress: ContentFrameIngressController,
) -> dict[str, object]:
    observation: dict[str, object] = {"worker_alive": ingress._worker.is_alive()}
    # Match _publish's lock order and never wait indefinitely for diagnostics.
    if not ingress._publisher_lock.acquire(timeout=0.05):
        observation["snapshot_error"] = "Publisher state busy"
        return observation
    try:
        if not ingress._condition.acquire(timeout=0.05):
            observation["snapshot_error"] = "Control state busy"
            return observation
        try:
            publisher = ingress._publisher
            observation.update({
                "media_epoch": ingress._media_epoch,
                "requested_transform": ingress._image_transform,
                "pending_transform": ingress._pending_image_transform,
                "animation_current": ingress._animation.current,
                "animation_target": ingress._animation.target,
                "animation_active": ingress._animation.is_active,
                "frame_pending": ingress._pending_frame is not None,
                "retained_epoch": (
                    ingress._retained_frame[1] if ingress._retained_frame is not None else None
                ),
                "publisher_sequence": getattr(publisher, "_sequence", None),
                "publisher_present": publisher is not None,
                "publisher_type": type(publisher).__name__,
            })
            descriptor = publisher.descriptor if publisher is not None else None
        finally:
            ingress._condition.release()
    finally:
        ingress._publisher_lock.release()
    if publisher is None or descriptor is None:
        return observation
    observation["descriptor"] = {
        "channel_id": descriptor.channel_id,
        "generation": descriptor.generation,
        "transport": descriptor.transport.value,
        "size": (descriptor.width, descriptor.height),
        "pixel_format": descriptor.pixel_format.value,
        "color_space": descriptor.color_space.value,
        "color_range": descriptor.color_range.value,
    }
    reader = None
    try:
        reader = SharedFrameChannelReader(
            descriptor.handle_token, descriptor.width, descriptor.height,
        )
        frame = reader.read_latest()
        if frame is None:
            observation["frame"] = "No complete ingress snapshot"
        else:
            observation["frame"] = {
                "sequence": reader._last_seq,
                "media_epoch": frame.media_epoch,
                "size": (frame.width, frame.height),
                "stride": frame.stride,
                "quadrant_centers": [
                    (x, y, frame.data[
                        y * frame.stride + x * 4 : y * frame.stride + x * 4 + 4
                    ].hex())
                    for y in (frame.height // 4, 3 * frame.height // 4)
                    for x in (frame.width // 4, 3 * frame.width // 4)
                ],
            }
    except (OSError, ValueError, BufferError) as error:
        observation["snapshot_error"] = {
            "type": type(error).__name__, "errno": getattr(error, "errno", None),
        }
    finally:
        if reader is not None:
            try:
                reader.close()
            except (OSError, ValueError, BufferError) as error:
                observation["snapshot_close_error"] = {
                    "type": type(error).__name__, "errno": getattr(error, "errno", None),
                }
    return observation


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


def _engine_observation(engine: SceneEngine) -> dict[str, object]:
    health = engine.health
    heartbeat = health.last_heartbeat_monotonic
    observation = {
        "status": health.status.value,
        "message": health.message,
        "process_id": health.process_id,
        "restart_count": health.restart_count,
        "health_snapshot_last_receive_age_seconds": (
            None if heartbeat is None else round(time.monotonic() - heartbeat, 3)
        ),
        "metrics": asdict(engine.metrics),
    }
    if sys.platform == "darwin" and health.process_id is not None:
        # Only called to explain a failed assertion, after its deadline. Sample
        # the live sidecar before cleanup removes the native execution stacks.
        try:
            with TemporaryDirectory(prefix="solin-native-sample-") as directory:
                report = Path(directory) / "sample.txt"
                result = subprocess.run(
                    # A 10 ms interval captures blocked calls without building
                    # thousands of software-shader branches before the timeout.
                    ["/usr/bin/sample", str(health.process_id), "1", "10", "-file", str(report)],
                    capture_output=True, text=True, timeout=5, check=False,
                )
                if result.returncode == 0 and report.is_file():
                    # Loaded-image paths are not needed to locate blocked calls.
                    sample = report.read_text(errors="replace").split(
                        "Binary Images:", 1,
                    )[0]
                    # A busy software shader can produce thousands of branches
                    # in one thread. Keep every thread's call-chain prefix within
                    # the report budget rather than truncating away later threads.
                    sections = re.split(r"(?m)(?=^    \d+ Thread_)", sample)
                    budget = max(0, 65536 // len(sections) - 32)
                    observation["native_stacks"] = "\n".join(
                        section if len(section) <= budget
                        else section[:budget] + "\n[Remaining samples omitted]\n"
                        for section in sections
                    )[:65536]
                else:
                    observation["native_sample_error"] = result.stderr[-2000:]
        except (OSError, subprocess.TimeoutExpired) as error:
            observation["native_sample_error"] = type(error).__name__
    return observation


def test_pixel_wait_probe_distinguishes_idle_writer_odd_sequence_and_read_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    egress = _BgraEgress(16, 16, channel_id="pixel-wait-probe")
    try:
        writer = SharedFrameChannelWriter(
            16, 16, name=egress.descriptor.handle_token, create=False,
        )
        try:
            probe = _PixelWaitProbe()
            assert probe.read_latest(egress) is None
            payload = bytes((0xE0, 0x40, 0x20, 0xFF)) * (16 * 16)
            for _ in range(12):
                writer.write(payload)
                assert probe.read_latest(egress) is not None
                assert probe.read_latest(egress) is None
            assert writer._buf is not None
            struct.pack_into("<Q", writer._buf, _SEQ_OFFSET, 25)
            assert probe.read_latest(egress) is None

            def timed_out():
                raise TimeoutError("Injected IPC read timeout")

            with monkeypatch.context() as patch:
                patch.setattr(egress._reader, "read_latest", timed_out)
                assert probe.read_latest(egress) is None

            observation = probe.observation()
            assert probe.read_attempts == 27
            assert probe.read_timeouts == 1
            assert probe.frames == 12
            assert observation["empty_reads"] == {
                "not_published": 1, "already_delivered": 12, "observed_odd_sequence": 1,
            }
            assert len(probe.channel_samples) == 8
            assert probe.channel_samples[-1] == (25, 25, 24)
            assert probe.last_frame is not None
            assert probe.last_frame.sequence == 24
            assert probe.last_frame.pixels == payload
            message = pformat(observation, width=120)
            assert "(25, 25, 24)" in message
            assert "observed_odd_sequence" in message
            assert "..." not in message
        finally:
            writer.close()
    finally:
        egress.close()


def _wait_for_clean_aspect_transition(
    subscriber: _BgraEgress,
    *,
    ingress: ContentFrameIngressController,
    engine: SceneEngine,
    stage: str,
    previous_center: bytes,
    previous_edge: bytes,
    next_center: bytes,
    next_edge: bytes,
    timeout: float = 5.0,
) -> _BgraFrame:
    probe = _PixelWaitProbe()

    def observation() -> dict[str, object]:
        return {
            "stage": stage,
            "expected_center_edge": (next_center.hex(), next_edge.hex()),
            "previous_center_edge": (previous_center.hex(), previous_edge.hex()),
            "egress": probe.observation(aspect_ratio=True),
            "ingress": _image_framing_ingress_observation(ingress),
            "engine": _engine_observation(engine),
        }

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        candidate = probe.read_latest(subscriber)
        if candidate is None:
            time.sleep(1 / 240)
            continue
        center = _bgra_pixel(candidate, candidate.width // 2, candidate.height // 2)
        edge = _bgra_pixel(candidate, 0, candidate.height // 2)
        if center == previous_center:
            assert edge == previous_edge, (
                "the previous raster was rendered with the next sample's geometry\n"
                + pformat(observation(), width=120)
            )
        if center == next_center and edge == next_edge:
            return candidate
    raise AssertionError(
        "the aspect-ratio transition did not reach its next frame\n"
        + pformat(observation(), width=120)
    )


def _seed_names() -> SceneSeedNames:
    return SceneSeedNames(
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
    )


def _content_document() -> tuple[SceneDocument, SceneDefinition]:
    document = create_default_scene_document(
        _seed_names(),
        document_id="libobs-content-pipeline-smoke",
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


class _FontManager(QObject):
    font_ready = Signal(str)

    def ensure(self, _name: str) -> None:
        pass

    def family(self, _name: str) -> str:
        return "Arial"


@pytest.mark.parametrize(
    "transition",
    [TransitionSpec(TransitionKind.CUT, 0), TransitionSpec(TransitionKind.DISSOLVE, 600),
     TransitionSpec(TransitionKind.FADE_TO_BLACK, 600)],
    ids=["cut", "dissolve", "fade-to-black"],
)
@pytest.mark.parametrize("preview_content", [False, True], ids=["automatic-preview", "content-preview"])
def test_closing_image_preserves_outgoing_pixels_until_default_transition(
    tmp_path: Path, scene_workspace_factory, transition: TransitionSpec, preview_content: bool,
) -> None:
    paths = ProfilePaths.from_roots(
        data_dir=tmp_path / "data", cache_dir=tmp_path / "cache", profile_id="image-return",
    )
    paths.ensure_dirs()
    workspace = scene_workspace_factory(paths, seed_names=_seed_names())
    workspace.documents.set_program_transition(transition)
    content_scene_id = workspace.documents.program_media_scene_id
    default_scene_id = workspace.documents.program_default_scene_id
    default_layer = workspace.documents.document.scene(default_scene_id).layers[0]
    workspace.documents.update_layer(
        default_scene_id, default_layer.id, replace(default_layer, source_id=NO_SIGNAL_SOURCE_ID),
    )
    engine = create_libobs_scene_engine()
    ingress = ContentFrameIngressController(publisher_factory=SharedMemoryContentPublisher)
    program = _BgraEgress(1920, 1080, channel_id="solin-program")
    scene_ids = tuple(scene.id for scene in workspace.documents.document.scenes)
    thumbnails = (
        _BgraEgress(160, 90 * len(scene_ids), channel_id="solin-thumbnails")
        if preview_content else None
    )
    projection = ProjectionSession()
    content = ProgramContentController(
        projection, _FontManager(), ingress.submit_frame, lambda: ("", "", ""),
        media_epoch_sink=ingress.begin_presentation, width=1920, height=1080,
    )
    controller = SceneRuntimeController(workspace, projection, engine=engine)
    application = QCoreApplication.instance()
    assert application is not None
    probe = _PixelWaitProbe()

    def green_on_air() -> bool:
        frame = probe.read_latest(program)
        return frame is not None and _program_pixel_matches(
            _bgra_pixel(frame, frame.width // 2, frame.height // 2), bytes((0, 255, 0, 255)),
        )

    try:
        controller.set_content_ingress(ingress.descriptor)
        controller.set_program_egress(program.descriptor)
        controller.start_engine()
        assert _wait_for(
            lambda: controller.applied_scene(BusId.VIRTUAL_CAMERA) == default_scene_id,
            application=application, timeout=15,
        )
        if thumbnails is not None:
            assert engine.set_thumbnail_egress(
                thumbnails.descriptor, scene_ids, 160, 90, request_id="image-thumbnails",
                sequence=controller._next_sequence(), deadline_ms=10_000,
            ).result(15).applied
        projection.set_state({"type": "image"})
        image = QImage(160, 90, QImage.Format.Format_ARGB32)
        image.fill(QColor("#00ff00"))
        content.submit_frame(image)
        assert _wait_for(green_on_air, application=application)
        if preview_content:
            controller.set_preview_scene(content_scene_id)
            assert _wait_for(lambda: not controller._pending, application=application)
            assert thumbnails is not None
            assert _wait_for_pixel(
                thumbnails, x=80, y=scene_ids.index(content_scene_id) * 90 + 45,
                expected=bytes((0, 255, 0, 255)),
            ) is not None
        time.sleep(0.7)
        sample_times: list[float] = []
        with _record_program_centers(program, sample_times=sample_times) as samples:
            projection.reset_state()
            # Allow ingress to deliver idle's transparent frame while Qt has not
            # yet processed the asynchronous default Take acknowledgement.
            time.sleep(0.15)
            outgoing = tuple(samples)
            assert outgoing
            if transition.kind is not TransitionKind.CUT:
                # A fast preparation can finish synchronously while reset_state
                # is still on the Qt thread. A legitimate fade may already have
                # started, but cannot consume its green origin in 150 ms.
                assert all(pixel[1] > 20 for _, pixel in outgoing), (
                    f"Idle blank erased the image before its transition: {outgoing!r}"
                )
            assert _wait_for(
                lambda: controller.applied_scene(BusId.VIRTUAL_CAMERA) == default_scene_id
                and controller.applied_scene(BusId.MEDIA_WINDOWS) == default_scene_id
                and not controller._pending,
                application=application,
            )
            time.sleep(0.8)
        if transition.kind is not TransitionKind.CUT:
            assert any(20 < pixel[1] < 230 for _, pixel in samples), (
                f"The outgoing image never faded into Default: {samples!r}"
            )
            phase_seconds = transition.duration_ms / 1000
            if transition.kind is TransitionKind.FADE_TO_BLACK:
                phase_seconds /= 2
            # Bound each drop by elapsed time, allowing skipped output frames
            # under load without accepting an abrupt replacement of the origin.
            for index in range(1, len(samples)):
                elapsed = sample_times[index] - sample_times[index - 1]
                # Readback completion can lag composition. Account for native
                # frame progression as well as the observer's wall clock.
                frame_count = (samples[index][0] - samples[index - 1][0]) / 2
                elapsed = max(elapsed, frame_count / 60)
                previous_green, green = samples[index - 1][1][1], samples[index][1][1]
                assert previous_green - green <= 20 + 3 * 255 * elapsed / phase_seconds, (
                    f"Content replacement interrupted the outgoing fade: {samples!r}"
                )
        assert controller.applied_scene(BusId.VIRTUAL_CAMERA) != content_scene_id
        if thumbnails is not None:
            assert _wait_for_pixel(
                thumbnails, x=80, y=scene_ids.index(content_scene_id) * 90 + 45,
                expected=bytes((0, 0, 0, 255)),
            ) is not None
    finally:
        controller.close()
        content.close()
        ingress.close()
        program.close()
        if thumbnails is not None:
            thumbnails.close()


@pytest.mark.parametrize("autoplay", [True, False], ids=["playing", "paused"])
@pytest.mark.parametrize("return_kind", ["idle", "image"], ids=["default-return", "image-return"])
@pytest.mark.parametrize(
    "transition",
    [
        TransitionSpec(TransitionKind.CUT, 0),
        TransitionSpec(TransitionKind.DISSOLVE, 350),
        TransitionSpec(TransitionKind.FADE_TO_BLACK, 350),
    ],
    ids=["cut", "dissolve", "fade-to-black"],
)
def test_video_auto_switch_reaches_program_without_app_decoded_frames(
    tmp_path: Path,
    scene_workspace_factory,
    autoplay: bool,
    transition: TransitionSpec,
    return_kind: str,
    record_property,
) -> None:
    # Each clip has a persistent identity at its center and a changing corner.
    # A latest-frame observer can skip any 200 ms interval under render load;
    # verify identity and motion without requiring a particular playback phase.
    media_paths = []
    for cycle, color in enumerate(("#ff0000", "#0000ff")):
        path = tmp_path / f"video-{cycle}.gif"
        media_paths.append(path)
        images = [Image.new("RGB", (160, 90), color) for _ in range(80)]
        try:
            for index, image in enumerate(images):
                image.paste((index * 3,) * 3, (0, 0, 40, 30))
            images[0].save(path, save_all=True, append_images=images[1:], duration=200, loop=0)
        finally:
            for image in images:
                image.close()

    paths = ProfilePaths.from_roots(
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
        profile_id="video-route",
    )
    paths.ensure_dirs()
    workspace = scene_workspace_factory(paths, seed_names=_seed_names())
    workspace.documents.set_program_transition(transition)
    content_scene_id = workspace.documents.program_media_scene_id
    assert content_scene_id is not None
    default_scene_id = workspace.documents.program_default_scene_id
    engine = create_libobs_scene_engine()
    ingress = ContentFrameIngressController(publisher_factory=SharedMemoryContentPublisher)
    program = _BgraEgress(1920, 1080, channel_id="solin-program")
    projection = ProjectionSession()
    unsubscribe_projection = projection.subscribe(
        lambda: ingress.begin_presentation(projection.presentation_session_id)
    )
    controller = SceneRuntimeController(workspace, projection, engine=engine)
    events: list[object] = []
    errors: list[str] = []
    fallbacks: list[str] = []
    unsubscribe_engine = engine.subscribe(events.append)
    controller.engine_error.connect(errors.append)
    controller.transition_fallback.connect(fallbacks.append)
    application = QCoreApplication.instance()
    assert application is not None
    probe = _PixelWaitProbe()
    close_latencies_ms: list[float] = []
    destination_latencies_ms: list[float] = []

    def program_is_color(expected: bytes, after_sequence: int) -> bool:
        frame = probe.read_latest(program)
        return frame is not None and frame.sequence > after_sequence and _program_pixel_matches(
            _bgra_pixel(frame, frame.width // 2, frame.height // 2),
            expected,
        )

    def observation() -> str:
        return pformat(
            {
                "errors": errors,
                "fallbacks": fallbacks,
                "events": events[-10:],
                "program": probe.observation(),
                "desired": controller.desired_scenes,
                "applied": controller.applied_scenes,
                "engine": _engine_observation(engine),
            },
            width=120,
        )

    try:
        controller.set_content_ingress(ingress.descriptor)
        controller.set_program_egress(program.descriptor)
        controller.start_engine()
        assert _wait_for(
            lambda: controller.applied_scene(BusId.VIRTUAL_CAMERA) == default_scene_id,
            application=application,
            timeout=15,
        ), observation()

        for cycle, path in enumerate(media_paths):
            before_image = program.channel_sequence()
            projection.set_state({"type": "image", "title": f"Image {cycle}"})
            previous_image = QImage(160, 90, QImage.Format.Format_ARGB32)
            previous_image.fill(QColor("#00ff00"))
            ingress.submit_frame(previous_image)
            assert _wait_for(
                lambda boundary=before_image: (
                    controller.applied_scene(BusId.VIRTUAL_CAMERA) == content_scene_id
                    and program_is_color(bytes((0, 255, 0, 255)), boundary)
                ),
                application=application,
            ), observation()

            before_open = program.channel_sequence()
            ingress_sequence = getattr(ingress._publisher, "_sequence", 0)
            # Match MediaProjectionController: commit presentation identity first,
            # immediately queue open_media next, without pumping Qt between them.
            # An ingress wait here blocks the decoder queued behind preparation.
            expected = bytes((0, 0, 255, 255)) if cycle == 0 else bytes((255, 0, 0, 255))
            with _record_program_centers(program) as entry_samples:
                projection.set_state({"type": "video", "title": f"Video {cycle}"})
                assert (
                    engine.open_media(
                        str(path),
                        content_media_epoch=projection.presentation_session_id,
                        is_local_file=True,
                        autoplay=autoplay,
                        request_id=f"video-open-{cycle}",
                        deadline_ms=4000,
                    )
                    .result(5)
                    .applied
                )
                assert _wait_for(
                    lambda current_path=str(path): any(
                        isinstance(event, MediaPlaybackEvent)
                        and event.state.slot == 0 and event.state.path == current_path
                        and event.state.state
                        is (MediaPlaybackState.PLAYING if autoplay else MediaPlaybackState.PAUSED)
                        for event in events
                    ),
                    application=application,
                ), observation()
                assert _wait_for(
                    lambda: (
                        controller.applied_scene(BusId.VIRTUAL_CAMERA) == content_scene_id
                        and controller.applied_scene(BusId.MEDIA_WINDOWS) == content_scene_id
                    ),
                    application=application,
                ), observation()
                if not autoplay:
                    assert _wait_for(
                        lambda expected=expected, boundary=before_open: program_is_color(
                            expected, boundary,
                        ),
                        application=application,
                    ), f"Paused video never published its first frame: {observation()}"
                    assert (
                        engine.control_media(
                            MediaControlAction.PLAY,
                            request_id=f"video-play-{cycle}",
                            deadline_ms=4000,
                        )
                        .result(5)
                        .applied
                    )
                assert _wait_for(
                    lambda expected=expected, boundary=before_open: program_is_color(
                        expected, boundary,
                    ),
                    application=application,
                ), observation()
                assert _wait_for(
                    lambda current_path=str(path): any(
                        isinstance(event, MediaPlaybackEvent)
                        and event.state.slot == 0 and event.state.path == current_path
                        and event.state.state is MediaPlaybackState.PLAYING
                        and event.state.position_ms > 800 and not event.state.error_code
                        for event in events
                    ), application=application,
                ), observation()

                animation_origin: bytes | None = None

                def animation_changed(expected=expected) -> bool:
                    nonlocal animation_origin
                    frame = probe.read_latest(program)
                    if frame is None or not _program_pixel_matches(
                        _bgra_pixel(frame, frame.width // 2, frame.height // 2), expected,
                    ):
                        return False
                    pixel = _bgra_pixel(frame, frame.width // 8, frame.height // 8)
                    if animation_origin is None:
                        animation_origin = pixel
                    # Exclude limited-range conversion rounding from the motion check.
                    return abs(pixel[0] - animation_origin[0]) > 12

                assert _wait_for(animation_changed, application=application), observation()
                assert getattr(ingress._publisher, "_sequence", 0) == ingress_sequence
            if transition.kind is TransitionKind.DISSOLVE:
                assert any(
                    20 < pixel[1] < 230 and max(pixel[0], pixel[2]) > 20
                    for _, pixel in entry_samples
                ), f"Image to video bypassed the configured dissolve: {entry_samples!r}"
                assert all(
                    not (20 < pixel[1] < 230 and max(pixel[0], pixel[2]) <= 6)
                    for _, pixel in entry_samples
                ), f"Image faded to an empty decoder: {entry_samples!r}"
            elif transition.kind is TransitionKind.FADE_TO_BLACK:
                assert any(20 < pixel[1] < 230 for _, pixel in entry_samples), entry_samples
                assert any(
                    pixel[1] <= 6 and 20 < max(pixel[0], pixel[2]) < 230
                    for _, pixel in entry_samples
                ), entry_samples
            assert errors == [], observation()
            assert fallbacks == [], observation()

            with _record_program_centers(program) as samples:
                close_started_at = time.monotonic()
                assert (
                    engine.control_media(
                        MediaControlAction.CLOSE,
                        request_id=f"video-close-{cycle}",
                        deadline_ms=4000,
                    )
                    .result(5)
                    .applied
                )
                close_latencies_ms.append((time.monotonic() - close_started_at) * 1000)
                # IPC and Qt reconciliation are asynchronous. Observe the closed
                # content scene before the default take can hide an invalid swap.
                time.sleep(0.15)
                outgoing = tuple(samples)
                assert outgoing and all(
                    pixel[1] <= 6 and max(pixel[0], pixel[2]) >= 240
                    for _, pixel in outgoing
                ), f"Closing transport discarded the outgoing video picture: {outgoing!r}"
                destination_started_at = time.monotonic()
                if return_kind == "image":
                    projection.set_state({"type": "image", "title": "Next image"})
                    next_image = QImage(160, 90, QImage.Format.Format_ARGB32)
                    next_image.fill(QColor("#ff00ff"))
                    ingress.submit_frame(next_image)
                    destination_scene_id = content_scene_id
                else:
                    projection.reset_state()
                    destination_scene_id = default_scene_id
                assert _wait_for(
                    lambda destination_scene_id=destination_scene_id: (
                        controller.applied_scene(BusId.VIRTUAL_CAMERA) == destination_scene_id
                        and controller.applied_scene(BusId.MEDIA_WINDOWS) == destination_scene_id
                        and not controller._pending
                    ),
                    application=application,
                ), observation()
                if return_kind == "image":
                    assert _wait_for(
                        lambda: any(pixel[0] >= 240 and pixel[2] >= 240 for _, pixel in tuple(samples)),
                        application=application,
                    ), observation()
                destination_latencies_ms.append((time.monotonic() - destination_started_at) * 1000)
                time.sleep(transition.duration_ms / 1000 + 0.1)
            assert samples, observation()
            assert all(pixel[1] <= max(pixel[0], pixel[2]) + 6 for _, pixel in samples), (
                f"The previous green image reappeared while closing video: {samples!r}"
            )
            events.clear()
        record_property("media_close_ms", ",".join(f"{value:.3f}" for value in close_latencies_ms))
        record_property(
            "destination_commit_ms",
            ",".join(f"{value:.3f}" for value in destination_latencies_ms),
        )
    finally:
        controller.close()
        unsubscribe_projection()
        unsubscribe_engine()
        ingress.close()
        program.close()


@pytest.mark.parametrize("autoplay", [True, False], ids=["playing", "paused"])
def test_replacing_native_video_preserves_the_outgoing_picture_during_dissolve(
    tmp_path: Path, autoplay: bool, record_property,
) -> None:
    paths = []
    for name, colors in (("red", ("#ff0000", "#fe0000")), ("blue", ("#0000ff", "#0000fe"))):
        path = tmp_path / f"{name}.gif"
        images = [Image.new("RGB", (160, 90), colors[index % 2]) for index in range(40)]
        try:
            images[0].save(path, save_all=True, append_images=images[1:], duration=200, loop=0)
        finally:
            for image in images:
                image.close()
        paths.append(path)
    engine = create_libobs_scene_engine()
    program = _BgraEgress(1920, 1080, channel_id="solin-program")
    document, scene = _content_document()
    sequence = 1
    take_latencies_ms = []

    def prepare(epoch: int, bus: BusId):
        nonlocal sequence
        sequence += 1
        return engine.prepare_scene(
            bus, scene.id, document_revision=document.revision,
            transition=TransitionSpec(TransitionKind.DISSOLVE, 600),
            content_media_epoch=epoch, content_source_kind=ContentSourceKind.NATIVE_MEDIA,
            request_id=f"video-replacement-prepare-{sequence}", sequence=sequence,
            deadline_ms=4000,
        ).result(5)

    def take(prepared):
        nonlocal sequence
        sequence += 1
        started = time.monotonic()
        assert engine.take_prepared(
            prepared, request_id=f"video-replacement-take-{sequence}",
            sequence=sequence, deadline_ms=4000,
        ).result(5).applied
        take_latencies_ms.append((time.monotonic() - started) * 1000)

    def is_color(color: bytes):
        frame = program.read_latest()
        return frame is not None and _program_pixel_matches(
            _bgra_pixel(frame, frame.width // 2, frame.height // 2), color,
        )

    try:
        engine.start(session_id="native-video-replacement", deadline_ms=10_000).result(15)
        assert engine.hydrate(
            SceneEngineSnapshot(
                session_id="native-video-replacement", sequence=sequence, document=document,
                active_scenes=tuple((bus, scene.id) for bus in BusId),
                render_enabled=tuple((bus, True) for bus in BusId),
                output_enabled=tuple((bus, False) for bus in BusId),
                program_egress=program.descriptor,
            ),
            request_id="native-video-replacement-hydrate", deadline_ms=10_000,
        ).result(15).applied
        assert engine.open_media(
            str(paths[0]), is_local_file=True, content_media_epoch=1,
            request_id="native-video-first", deadline_ms=4000,
        ).result(5).applied
        for bus in (BusId.VIRTUAL_CAMERA, BusId.MEDIA_WINDOWS, BusId.EDITOR):
            take(prepare(1, bus))
        assert _wait_for(lambda: is_color(bytes((0, 0, 255, 255))))
        # Preparation precedes opening on the ordered stream, as in production.
        prepared = prepare(2, BusId.VIRTUAL_CAMERA)
        with _record_program_centers(program) as samples:
            assert engine.open_media(
                str(paths[1]), is_local_file=True, autoplay=autoplay, content_media_epoch=2,
                request_id="native-video-second", deadline_ms=4000,
            ).result(5).applied
            time.sleep(0.1)
            assert samples and all(pixel[2] >= 240 and pixel[0] <= 6 for _, pixel in samples)
            take(prepared)
            for bus in (BusId.MEDIA_WINDOWS, BusId.EDITOR):
                take(prepare(2, bus))
            assert _wait_for(lambda: any(
                _program_pixel_matches(pixel, bytes((255, 0, 0, 255)))
                for _, pixel in tuple(samples)
            ))
        assert any(20 < pixel[0] < 230 and 20 < pixel[2] < 230 for _, pixel in samples), samples
        assert all(pixel[0] + pixel[2] >= 230 for _, pixel in samples), samples
        record_property("native_presentation_take_ms", ",".join(f"{value:.3f}" for value in take_latencies_ms))
    finally:
        engine.stop()
        program.close()


@pytest.mark.parametrize("local", [True, False], ids=["local", "http"])
@pytest.mark.parametrize("autoplay", [True, False], ids=["playing", "paused"])
def test_retired_native_presentations_unload_inputs_and_release_scenes(
    tmp_path: Path, local: bool, autoplay: bool,
) -> None:
    """Observe native destruction, not just Python bookkeeping or noisy RSS."""
    path = tmp_path / "retirement.gif"
    images = [Image.new("RGB", (160, 90), "red") for _ in range(40)]
    try:
        for index, image in enumerate(images):
            image.putpixel((0, 0), (0, 0, index * 6))  # retain distinct GIF frames
        images[0].save(path, save_all=True, append_images=images[1:], duration=100, loop=0)
    finally:
        for image in images:
            image.close()
    # Native libobs belongs in a separate process, including its weak references.
    script = tmp_path / "native_retirement.py"
    script.write_text(textwrap.dedent('''
        import sys
        import time
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from pathlib import Path
        from threading import Thread
        from pylibobs._ffi import ffi, get_lib
        from solin.core.media.obs_runtime import ObsRuntime
        from solin.core.media.obs_source_render import render_source_to_bgra
        from solin.core.scenes.ipc_protocol import SceneIpcEnvelope
        from solin.core.scenes.libobs_sidecar import LibobsSidecarEngine

        lib = get_lib()
        get_weak = ffi.cast(
            "obs_weak_source_t *(*)(obs_source_t *)", lib.obs_source_get_weak_source,
        )
        engine = LibobsSidecarEngine(runtime_factory=ObsRuntime)
        sequence = 0
        weak_refs = []
        buses = ("virtual_camera", "media_windows", "editor")
        local = sys.argv[2] == "True"
        autoplay = sys.argv[3] == "True"
        server = None
        server_thread = None
        path = sys.argv[1]
        if not local:
            payload = Path(path).read_bytes()
            class Handler(BaseHTTPRequestHandler):
                protocol_version = "HTTP/1.1"

                def do_GET(self):
                    start, end = 0, len(payload) - 1
                    byte_range = self.headers.get("Range")
                    if byte_range:
                        first, last = byte_range.removeprefix("bytes=").split("-", 1)
                        start = int(first)
                        if last:
                            end = min(int(last), end)
                    self.send_response(206 if byte_range else 200)
                    self.send_header("Accept-Ranges", "bytes")
                    self.send_header("Content-Type", "image/gif")
                    self.send_header("Content-Length", str(end - start + 1))
                    if byte_range:
                        self.send_header("Content-Range", f"bytes {start}-{end}/{len(payload)}")
                    self.end_headers()
                    self.wfile.write(payload[start:end + 1])

                def log_message(self, *_args):
                    pass
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            server_thread = Thread(target=server.serve_forever, daemon=True)
            server_thread.start()
            path = f"http://127.0.0.1:{server.server_port}/{Path(path).name}"

        def assert_picture(source):
            frame = render_source_to_bgra(
                source, 160, 90, canvas_width=160, canvas_height=90,
            )
            assert frame is not None, "Retired picture is no longer drawable"
            data, stride = frame
            pixel = data[45 * stride + 80 * 4:45 * stride + 80 * 4 + 4]
            assert pixel[0] < 8 and pixel[1] < 8 and pixel[2] > 245 and pixel[3] == 255, pixel

        def send(kind, payload=None):
            global sequence
            sequence += 1
            result = engine.handle(SceneIpcEnvelope(
                message_type=kind, request_id=f"retire-{sequence}",
                session_id="native-retirement", process_generation="native-retirement",
                sequence=sequence, document_revision=0,
                deadline_monotonic_ms=int(time.monotonic() * 1000) + 4000,
                payload=payload or {},
            ))
            assert result.message_type != "error", result
            assert result.payload.get("applied", True), result
            return result

        try:
            send("hello")
            send("hydrate", {"document": {
                "sources": [{"id": "solin.content.current", "type": "content"}],
                "scenes": [{"id": "idle", "layers": []}, {"id": "content", "layers": [{
                    "id": "picture", "source_id": "solin.content.current",
                }]}],
            }, "active_scenes": dict.fromkeys(buses, "idle")})
            previous = []
            for epoch in range(1, 7):
                send("open_media", {
                    "path": path, "autoplay": autoplay,
                    "is_local_file": local, "content_media_epoch": epoch,
                })
                for bus in buses:
                    prepared = send("prepare_scene", {
                        "bus_id": bus, "scene_id": "content",
                        "content_media_epoch": epoch, "content_source_kind": "native_media",
                        "transition": {"kind": "cut", "duration_ms": 0},
                    })
                    send("take_prepared", {
                        "bus_id": bus,
                        "preparation_token": prepared.payload["preparation_token"],
                    })
                deadline = time.monotonic() + 4
                while previous and not all(lib.obs_weak_source_expired(weak) for weak in previous):
                    assert time.monotonic() < deadline, (
                        "Retired native scene/decoder is still alive",
                        [bool(lib.obs_weak_source_expired(weak)) for weak in previous],
                        [[(key, scene.as_source().showing) for key, scene in scenes.items()]
                         for scenes, _ in engine._scene_graph._retired_presentations],
                        len(engine._retired_media_sources),
                    )
                    # This headless output needs its own render, like a room display.
                    render_source_to_bgra(
                        engine._projection_route._transition, 160, 90,
                        canvas_width=1920, canvas_height=1080,
                    )
                    time.sleep(.02)
                    send("ping")
                graph = engine._scene_graph
                media = engine._media_source.source
                assert media.media_duration > 0, "Input never became ready"
                assert_picture(media)
                previous = [get_weak(media._ptr)] + [
                    get_weak(graph._scenes["content"].as_source()._ptr)
                ]
                weak_refs.extend(previous)
                assert not any(lib.obs_weak_source_expired(weak) for weak in previous)
                send("control_media", {"action": "close"})
                deadline = time.monotonic() + 4
                while media.media_duration != 0:
                    assert time.monotonic() < deadline, "Closed presentation still owns its decoder/input"
                    time.sleep(.02)
                # The same native source remains borrowed; only its input is gone.
                assert graph.content_source is media
                for _ in range(5):
                    assert_picture(media)
                    time.sleep(.02)
                assert not lib.obs_weak_source_expired(previous[0])
                for bus in buses:
                    prepared = send("prepare_scene", {
                        "bus_id": bus, "scene_id": "idle",
                        "transition": {"kind": "cut", "duration_ms": 0},
                    })
                    send("take_prepared", {
                        "bus_id": bus,
                        "preparation_token": prepared.payload["preparation_token"],
                    })
                assert media.media_duration == 0
                assert_picture(media)
            engine.shutdown()
            assert all(lib.obs_weak_source_expired(weak) for weak in weak_refs)
        finally:
            for weak in weak_refs:
                lib.obs_weak_source_release(weak)
            engine.shutdown()
            if server is not None:
                server.shutdown()
                server.server_close()
                server_thread.join(timeout=2)
    '''), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(script), str(path), str(local), str(autoplay)], capture_output=True, text=True,
        errors="replace", timeout=45,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    "retained_kind",
    ["image", "idle"],
    ids=["image-shm", "idle-shm"],
)
def test_return_to_cached_content_never_publishes_the_previous_presentation(
    retained_kind: str,
    record_property,
) -> None:
    engine = create_libobs_scene_engine()
    ingress = ContentFrameIngressController(
        publisher_factory=SharedMemoryContentPublisher,
        maximum_fps=30,
        canvas_width=1920,
        canvas_height=1080,
    )
    descriptor = ingress.descriptor
    preview = _BgraEgress(1920, 1080, channel_id="solin-preview")
    program = _BgraEgress(1920, 1080, channel_id="solin-program")
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
    retained_pixel = (
        bytes((0x20, 0xD0, 0x20, 0xFF))
        if retained_kind == "idle"
        else bytes((0x20, 0x20, 0xE0, 0xFF))
    )
    events: list[object] = []
    program_ready = Event()

    def on_engine_event(event: object) -> None:
        events.append(event)
        if (
            isinstance(event, FrameEgressReadyEvent)
            and event.channel_id == program.descriptor.channel_id
            and event.generation == program.descriptor.generation
            and event.handle_token == program.descriptor.handle_token
        ):
            program_ready.set()

    unsubscribe = engine.subscribe(on_engine_event)
    prepare_latencies: list[float] = []
    take_latencies: list[float] = []
    program_probe = _PixelWaitProbe()
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
        started_at = time.monotonic()
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
        if prepared.scene_id == content_scene.id:
            take_latencies.append((time.monotonic() - started_at) * 1000)

    def program_matches(match: Callable[[_BgraFrame, int, int], bool]) -> bool:
        frame = program_probe.read_latest(program)
        return frame is not None and match(frame, frame.width // 2, frame.height // 2)

    def is_blue(pixel: bytes) -> bool:
        return _program_pixel_matches(pixel, bytes((0xFF, 0, 0, 0xFF)))

    def is_black_to_blue(pixel: bytes) -> bool:
        return pixel[1] <= 6 and pixel[2] <= 6 and pixel[3] == 0xFF

    try:
        capabilities = engine.start(
            session_id="libobs-cached-content-return", deadline_ms=10_000
        ).result(15)
        assert capabilities.hardware_compositing
        ingress.begin_presentation(1)
        ingress.submit_frame(retained_frame)
        publisher = ingress._publisher
        assert publisher is not None
        assert _wait_for(lambda: int(getattr(publisher, "_sequence", 0)) > 0), (
            "Initial retained presentation was not published"
        )
        assert (
            engine.hydrate(
                SceneEngineSnapshot(
                    session_id="libobs-cached-content-return",
                    sequence=sequence,
                    document=document,
                    active_scenes=tuple((bus_id, content_scene.id) for bus_id in BusId),
                    render_enabled=tuple((bus_id, True) for bus_id in BusId),
                    output_enabled=tuple(
                        (bus_id, bus_id is BusId.MEDIA_WINDOWS) for bus_id in BusId
                    ),
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
        assert program_ready.wait(15), pformat({
            "stage": "program egress readiness",
            "descriptor": asdict(program.descriptor),
            "events": events,
            "program": program_probe.observation(),
            "engine": _engine_observation(engine),
        }, width=120)

        for cycle in range(3):
            if cycle:
                ingress.begin_presentation(cycle * 2 + 1)
                ingress.submit_frame(retained_frame)
                take(prepare(content_scene.id, content_media_epoch=cycle * 2 + 1))
            assert _wait_for(
                lambda: program_matches(
                    lambda frame, x, y: _program_pixel_matches(
                        _bgra_pixel(frame, x, y), retained_pixel
                    )
                )
            ), pformat({
                "stage": "retained program presentation", "cycle": cycle,
                "expected_pixel": retained_pixel.hex(),
                "program": program_probe.observation(),
                "ingress": _image_framing_ingress_observation(ingress),
                "engine": _engine_observation(engine),
            }, width=120)

            take(prepare(away_scene.id))
            assert _wait_for(
                lambda: program_matches(
                    lambda frame, x, y: _program_pixel_matches(
                        _bgra_pixel(frame, x, y), bytes((0, 0, 0, 0xFF))
                    )
                )
            ), events

            # Production auto switch starts preparing Program as soon as the
            # presentation identity changes. The first pixels are published by
            # the content controller in the same turn, but the sidecar still has
            # to retire the previous presentation before those pixels are safe
            # to expose. Waiting for Preview to finish that transition here
            # hides the race seen by the application.
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
                assert _wait_for(lambda: any(is_blue(pixel) for _, pixel in tuple(samples))), (
                    cycle,
                    samples,
                    events,
                )
                time.sleep(0.1)
            assert samples, "Program emitted no frames during the cached return"
            assert all(is_black_to_blue(pixel) for _, pixel in samples), (
                f"cached {retained_kind} reappeared on return {cycle}: {samples!r}"
            )
            sequences = [frame_sequence for frame_sequence, _ in samples]
            assert sequences == sorted(set(sequences)), (
                f"Program egress sequence regressed or repeated: {sequences!r}"
            )
        assert prepare_latencies and max(prepare_latencies) < 1_500, prepare_latencies
        assert take_latencies and max(take_latencies) < 1_500, take_latencies
        record_property("content_presentation_take_ms", ",".join(f"{value:.3f}" for value in take_latencies))
    finally:
        ingress.close()
        preview.close()
        program.close()
        unsubscribe()
        engine.stop()


def _run_native_readback(scenario: str) -> None:
    # libobs, its plugins and registered source types have process-wide state.
    # Give each scenario the same lifetime boundary as the production sidecar.
    result = subprocess.run(
        [sys.executable, "-c", (
            "from tests.integration.test_native_scene_content_pipeline import "
            f"{scenario}; {scenario}()"
        )],
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_native_readback_preserves_source_alpha_and_flattens_scene_background() -> None:
    _run_native_readback("_assert_native_readback_alpha")


def _assert_native_readback_alpha() -> None:
    from solin.core.media.obs_runtime import ObsRuntime
    from solin.core.media.obs_source_render import (
        render_source_to_bgra, resolve_render_source_to_bgra, shutdown,
    )

    runtime = ObsRuntime()
    source = scene = None
    try:
        runtime.ensure_started(width=64, height=64)
        source = runtime.ob.Source.create("color_source_v3", "alpha-readback", {
            "width": 32, "height": 64, "color": 0x800000FF,
        })
        scene = runtime.ob.Scene.create("alpha-readback-scene")
        scene.add(source)

        def pixels(renderer, subject):
            frame = renderer(subject, 64, 64, canvas_width=64, canvas_height=64)
            assert frame is not None
            data, stride = frame
            return (data[32 * stride + 16 * 4:32 * stride + 17 * 4],
                    data[32 * stride + 48 * 4:32 * stride + 49 * 4])

        raw_center, raw_background = pixels(render_source_to_bgra, source)
        assert raw_center == bytes((0, 0, 255, 128))
        assert raw_background == bytes((0, 0, 0, 0))

        scene_source = scene.as_source()
        scene_center, scene_background = pixels(render_source_to_bgra, scene_source)
        assert scene_center[3] == 128
        assert scene_background == bytes((0, 0, 0, 0))

        opaque_renderer = resolve_render_source_to_bgra(opaque_background=True)
        opaque_center, opaque_background = pixels(opaque_renderer, scene_source)
        # Flatten the scene once: premultiplied color must not darken a second time.
        assert opaque_center[:3] == scene_center[:3]
        assert opaque_center[3] == 255
        assert opaque_background == bytes((0, 0, 0, 255))
    finally:
        if scene is not None:
            scene.release()
        if source is not None:
            source.release()
        shutdown()
        runtime.shutdown()


def test_native_bgra_frames_preserve_color_and_transparency_across_opacity_changes() -> None:
    _run_native_readback("_assert_native_bgra_opacity_changes")


def _assert_native_bgra_opacity_changes() -> None:
    from solin.core.media.obs_frame_source import create_frame_source
    from solin.core.media.obs_runtime import ObsRuntime
    from solin.core.media.obs_source_render import (
        render_source_to_bgra, resolve_render_source_to_bgra, shutdown,
    )

    runtime = ObsRuntime()
    source = scene = callback = None
    samples = SimpleQueue()
    last_sample = None
    try:
        runtime.ensure_started(width=64, height=64)
        source = create_frame_source(runtime, "bgra-alpha-readback")
        assert source is not None
        scene = runtime.ob.Scene.create("bgra-alpha-readback-scene")
        scene.add(source.source)
        opaque_renderer = resolve_render_source_to_bgra(opaque_background=True)

        def pixels(renderer, subject):
            result = renderer(subject, 64, 64, canvas_width=64, canvas_height=64)
            assert result is not None
            data, stride = result
            return (data[32 * stride + 16 * 4:32 * stride + 17 * 4],
                    data[32 * stride + 48 * 4:32 * stride + 49 * 4])

        def sample_frame(_width, _height):
            # Async frames are selected during the video tick. Consume them in
            # that same thread's render phase, as the production egress does.
            try:
                samples.put((
                    pixels(render_source_to_bgra, source.source),
                    pixels(render_source_to_bgra, scene.as_source()),
                    pixels(opaque_renderer, scene.as_source()),
                ))
            except Exception as error:  # noqa: BLE001 - propagate callback failures to pytest
                samples.put(error)

        def next_frame_matches(expected):
            nonlocal last_sample
            try:
                last_sample = samples.get_nowait()
            except Empty:
                return False
            if isinstance(last_sample, Exception):
                raise last_sample
            return last_sample[0] == (expected, bytes((0, 0, 0, 0)))

        callback = runtime.ob.add_main_render_callback(sample_frame)
        # Reuse the same native source/texture so alpha metadata cannot leak
        # from an opaque frame into a transparent one or vice versa.
        for reset in (True, False):
            rgb = (32, 64, 224) if reset else (64, 32, 192)
            for alpha, composed in (
                (255, bytes((*rgb, 255))),
                (128, bytes((*(channel // 2 for channel in rgb), 128))),
                (0, bytes((0, 0, 0, 0))),
                (255, bytes((*rgb, 255))),
            ):
                original = bytes((*rgb, alpha))
                assert source.push_bgra(original * 32 * 64, 32, 64, 128, reset=reset)
                assert _wait_for(lambda expected=original: next_frame_matches(expected)), (
                    reset, alpha, last_sample,
                )
                assert last_sample[1] == (
                    composed, bytes((0, 0, 0, 0)),
                )
                assert last_sample[2] == (
                    composed[:3] + b"\xff", bytes((0, 0, 0, 255)),
                )
    finally:
        if callback is not None:
            runtime.ob.remove_main_render_callback(callback)
        if scene is not None:
            scene.release()
        if source is not None:
            source.release()
        shutdown()
        runtime.shutdown()


def _take_content_presentation(
    engine: SceneEngine, document: SceneDocument, scene: SceneDefinition, epoch: int,
) -> None:
    """Drive the explicit epoch handoff normally owned by SceneRuntimeController."""
    for index, bus in enumerate((BusId.VIRTUAL_CAMERA, BusId.MEDIA_WINDOWS, BusId.EDITOR)):
        sequence = epoch * 6 + index * 2 + 2
        prepared = engine.prepare_scene(
            bus, scene.id, transition=TransitionSpec(TransitionKind.CUT, 0),
            document_revision=document.revision, request_id=f"content-prepare-{epoch}-{bus}",
            sequence=sequence, deadline_ms=10_000, content_media_epoch=epoch,
        ).result(15)
        assert engine.take_prepared(
            prepared, request_id=f"content-take-{epoch}-{bus}", sequence=sequence + 1,
            deadline_ms=10_000,
        ).result(15).applied


def test_actual_size_content_reaches_composed_libobs_output() -> None:
    engine = create_libobs_scene_engine()

    ingress = ContentFrameIngressController(
        publisher_factory=SharedMemoryContentPublisher,
        maximum_fps=30,
        canvas_width=1920,
        canvas_height=1080,
    )
    egress = _BgraEgress(1920, 1080, channel_id="solin-preview")
    program_egress = _BgraEgress(1920, 1080, channel_id="solin-program")
    document, content_scene = _content_document()
    image = QImage(1280, 720, QImage.Format.Format_ARGB32)
    image.fill(QColor("#123456"))
    latest = None
    center_pixel = b""
    events: list[object] = []
    unsubscribe = engine.subscribe(events.append)

    try:
        capabilities = engine.start(
            session_id="libobs-content-smoke",
            deadline_ms=10_000,
        ).result(15)
        assert capabilities.hardware_compositing
        snapshot = SceneEngineSnapshot(
            session_id="libobs-content-smoke",
            sequence=1,
            document=document,
            active_scenes=tuple((bus_id, content_scene.id) for bus_id in BusId),
            render_enabled=(
                (BusId.MEDIA_WINDOWS, True),
                (BusId.VIRTUAL_CAMERA, True),
                (BusId.EDITOR, True),
            ),
            output_enabled=(
                (BusId.MEDIA_WINDOWS, True),
                (BusId.VIRTUAL_CAMERA, False),
                (BusId.EDITOR, False),
            ),
            content_ingress=ingress.descriptor,
            preview_egress=egress.descriptor,
            program_egress=program_egress.descriptor,
        )
        acknowledged = engine.hydrate(
            snapshot,
            request_id="libobs-content-smoke-hydrate",
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
            if program_frame is not None and _program_pixel_matches(
                _bgra_pixel(program_frame, 960, 540), bytes((0x56, 0x34, 0x12, 0xFF))
            ):
                break
            time.sleep(1 / 60)
        assert program_frame is not None
        assert program_frame.pixel_format is VideoPixelFormat.BGRA
        assert (program_frame.width, program_frame.height) == (1920, 1080)
        assert len(program_frame.pixels) == 1920 * 1080 * 4
        assert _program_pixel_matches(
            _bgra_pixel(program_frame, 960, 540), bytes((0x56, 0x34, 0x12, 0xFF))
        )
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
        for cycle in range(8):
            ingress.submit_frame(sixteen_by_nine)
            latest = _wait_for_clean_aspect_transition(
                egress,
                ingress=ingress,
                engine=engine,
                stage=f"aspect cycle {cycle}: 4:3 to 16:9",
                previous_center=green,
                previous_edge=black,
                next_center=yellow,
                next_edge=yellow,
            )
            assert _bgra_pixel(latest, 1919, 540) == yellow

            ingress.submit_frame(four_by_three)
            latest = _wait_for_clean_aspect_transition(
                egress,
                ingress=ingress,
                engine=engine,
                stage=f"aspect cycle {cycle}: 16:9 to 4:3",
                previous_center=yellow,
                previous_edge=yellow,
                next_center=green,
                next_edge=black,
            )
            assert _bgra_pixel(latest, 240, 540) == green

        # Image framing is baked into ingress pixels before libobs renders them. A 4:3
        # image normally has black side bars in the 16:9 scene; zooming it to
        # 2x must cover both edges in the composed output.
        ingress.begin_presentation(1)
        ingress.set_image_transform(
            ImageTransform(1.0, 0.0, 0.0),
            media_epoch=1,
            canvas_width=1920,
            canvas_height=1080,
            animate=False,
        )
        ingress.submit_frame(four_by_three)
        _take_content_presentation(engine, document, content_scene, 1)
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
        _take_content_presentation(engine, document, content_scene, 2)
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

        # The first frame makes the epoch ready; accepted Take commits it without
        # altering the preserved owner's texture while pixels are in transit.
        ingress.submit_frame(sixteen_by_nine)
        _take_content_presentation(engine, document, content_scene, 3)
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
        _take_content_presentation(engine, document, content_scene, 4)
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


def test_libobs_image_framing_preserves_aspect_and_normalized_pan() -> None:
    engine = create_libobs_scene_engine()

    ingress = ContentFrameIngressController(
        publisher_factory=SharedMemoryContentPublisher,
        maximum_fps=30,
        canvas_width=1920,
        canvas_height=1080,
    )
    egress = _BgraEgress(1920, 1080, channel_id="solin-preview")
    document, content_scene = _content_document()
    try:
        capabilities = engine.start(
            session_id="libobs-image-framing",
            deadline_ms=10_000,
        ).result(15)
        assert capabilities.hardware_compositing
        acknowledged = engine.hydrate(
            SceneEngineSnapshot(
                session_id="libobs-image-framing",
                sequence=1,
                document=document,
                active_scenes=tuple((bus_id, content_scene.id) for bus_id in BusId),
                render_enabled=(
                    (BusId.MEDIA_WINDOWS, True),
                    (BusId.VIRTUAL_CAMERA, True),
                    (BusId.EDITOR, True),
                ),
                output_enabled=(
                    (BusId.MEDIA_WINDOWS, True),
                    (BusId.VIRTUAL_CAMERA, False),
                    (BusId.EDITOR, False),
                ),
                content_ingress=ingress.descriptor,
                preview_egress=egress.descriptor,
            ),
            request_id="libobs-image-framing-hydrate",
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
        probe = _PixelWaitProbe()
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
                probe=probe,
            )

        assert output is not None, pformat({
            "expected_blue": blue.hex(),
            "egress": probe.observation(),
            "ingress": _image_framing_ingress_observation(ingress),
            "engine": _engine_observation(engine),
        }, width=120)
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
        probe = _PixelWaitProbe()
        centered = _wait_for_pixel(
            egress,
            x=1200,
            y=200,
            expected=green,
            timeout=8.0,
            probe=probe,
        )
        assert centered is not None, pformat({
            "stage": "centered zoom",
            "expected_green": green.hex(),
            "egress": probe.observation(),
            "ingress": _image_framing_ingress_observation(ingress),
            "engine": _engine_observation(engine),
        }, width=120)
        assert _bgra_pixel(centered, 200, 200) == red
        assert _bgra_pixel(centered, 200, 900) == blue
        assert _bgra_pixel(centered, 1200, 700) == yellow
        assert _bgra_pixel(centered, 1200, 900) == yellow
    finally:
        ingress.close()
        egress.close()
        engine.stop()
