from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass
from enum import StrEnum
from math import gcd, isfinite
from typing import Protocol, TypeAlias

from solin.core.scenes.model import (
    MAXIMUM_CAMERA_SOURCE_DIMENSION,
    MAXIMUM_CAMERA_SOURCE_FPS,
    MAXIMUM_CAMERA_SOURCE_PIXELS,
    MAXIMUM_CAMERA_SOURCE_SHORT_EDGE,
    BusId,
    CameraMediaType,
    SceneDocument,
    SceneLayer,
    TransitionKind,
    VideoColorRange,
    VideoColorSpace,
    VideoPixelFormat,
)


MAXIMUM_OUTPUT_WINDOW_TARGETS = 32
DEFAULT_ENGINE_STARTUP_DEADLINE_MS = 15_000


class SceneEngineStatus(StrEnum):
    STOPPED = "stopped"
    STARTING = "starting"
    READY = "ready"
    DEGRADED = "degraded"
    FAILED = "failed"


class FrameChannelTransport(StrEnum):
    D3D11_SHARED_TEXTURE = "d3d11_shared_texture"
    SHARED_MEMORY_BGRA = "shared_memory_bgra"
    SHARED_MEMORY_VIDEO = "shared_memory_video"


class FrameProducerKind(StrEnum):
    SOLIN_OFFSCREEN = "solin_offscreen"
    NATIVE_COMPOSITOR = "native_compositor"


class SourceHealthStatus(StrEnum):
    STARTING = "starting"
    READY = "ready"
    DEGRADED = "degraded"
    FAILED = "failed"
    STOPPED = "stopped"


@dataclass(frozen=True, slots=True)
class SceneEngineCapabilities:
    protocol_version: int
    process_generation: str
    local_cameras: bool
    rtsp_cameras: bool
    hardware_compositing: bool
    virtual_camera: bool
    d3d11_shared_textures: bool

    def __post_init__(self) -> None:
        _positive_int(self.protocol_version, "protocol_version")
        _identity(self.process_generation, "process_generation")
        _booleans(
            self.local_cameras,
            self.rtsp_cameras,
            self.hardware_compositing,
            self.virtual_camera,
            self.d3d11_shared_textures,
        )


@dataclass(frozen=True, slots=True)
class LocalVideoFormat:
    media_type: CameraMediaType
    pixel_format: str
    width: int
    height: int
    fps_numerator: int
    fps_denominator: int

    def __post_init__(self) -> None:
        if not isinstance(self.media_type, CameraMediaType):
            raise ValueError("Invalid local video media type")
        if not self.pixel_format:
            raise ValueError("Local video pixel format is required")
        _bounded_text(self.pixel_format, 80, "local video pixel format")
        _bounded_int(
            self.width,
            1,
            MAXIMUM_CAMERA_SOURCE_DIMENSION,
            "local video width",
        )
        _bounded_int(
            self.height,
            1,
            MAXIMUM_CAMERA_SOURCE_DIMENSION,
            "local video height",
        )
        if (
            self.width * self.height > MAXIMUM_CAMERA_SOURCE_PIXELS
            or min(self.width, self.height) > MAXIMUM_CAMERA_SOURCE_SHORT_EDGE
        ):
            raise ValueError("Local video dimensions exceed the media budget")
        _bounded_int(self.fps_numerator, 1, 480_000, "local video FPS numerator")
        _bounded_int(self.fps_denominator, 1, 100_000, "local video FPS denominator")
        if (
            self.fps_numerator > MAXIMUM_CAMERA_SOURCE_FPS * self.fps_denominator
            or gcd(self.fps_numerator, self.fps_denominator) != 1
        ):
            raise ValueError("Invalid local video frame rate")

    @property
    def frames_per_second(self) -> float:
        return self.fps_numerator / self.fps_denominator


@dataclass(frozen=True, slots=True)
class LocalCameraDevice:
    device_id: str
    display_name: str
    software_device: bool
    formats: tuple[LocalVideoFormat, ...]

    def __post_init__(self) -> None:
        if not self.device_id:
            raise ValueError("Local camera device id is required")
        _bounded_text(self.device_id, 1024, "local camera device id")
        _bounded_text(self.display_name, 512, "local camera display name")
        if not isinstance(self.software_device, bool):
            raise ValueError("Local camera software-device flag must be a boolean")
        if (
            not isinstance(self.formats, tuple)
            or len(self.formats) > 256
            or not all(isinstance(item, LocalVideoFormat) for item in self.formats)
            or len(self.formats) != len(set(self.formats))
        ):
            raise ValueError("Invalid local camera format list")


@dataclass(frozen=True, slots=True)
class LocalCameraDiscovery:
    supported: bool
    ready: bool
    generation: int
    devices: tuple[LocalCameraDevice, ...]
    error_code: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.supported, bool) or not isinstance(self.ready, bool):
            raise ValueError("Local camera discovery state must use booleans")
        _non_negative_int(self.generation, "local camera discovery generation")
        if (
            not isinstance(self.devices, tuple)
            or len(self.devices) > 64
            or not all(isinstance(item, LocalCameraDevice) for item in self.devices)
        ):
            raise ValueError("Invalid local camera device list")
        device_ids = tuple(device.device_id for device in self.devices)
        if len(device_ids) != len(set(device_ids)):
            raise ValueError("Local camera device ids must be unique")
        _bounded_text(self.error_code, 128, "local camera discovery error code")
        if not self.supported and not self.error_code:
            raise ValueError("Unsupported local camera discovery requires an error code")
        if not self.ready and (self.devices or self.error_code):
            raise ValueError("Pending local camera discovery cannot contain results")


@dataclass(frozen=True, slots=True)
class FrameChannelDescriptor:
    channel_id: str
    generation: int
    producer_kind: FrameProducerKind
    transport: FrameChannelTransport
    handle_token: str
    width: int
    height: int
    pixel_format: VideoPixelFormat
    color_space: VideoColorSpace
    color_range: VideoColorRange

    def __post_init__(self) -> None:
        _identity(self.channel_id, "channel_id")
        _non_negative_int(self.generation, "channel generation")
        if not isinstance(self.producer_kind, FrameProducerKind):
            raise ValueError("Invalid frame producer kind")
        if not isinstance(self.transport, FrameChannelTransport):
            raise ValueError("Invalid frame channel transport")
        _identity(self.handle_token, "frame handle token")
        _bounded_int(self.width, 1, 7680, "frame width")
        _bounded_int(self.height, 1, 4320, "frame height")
        if not isinstance(self.pixel_format, VideoPixelFormat):
            raise ValueError("Invalid frame pixel format")
        if (
            self.transport is FrameChannelTransport.SHARED_MEMORY_BGRA
            and self.pixel_format is not VideoPixelFormat.BGRA
        ):
            raise ValueError("Shared-memory frame channels require BGRA pixels")
        if (
            self.transport is FrameChannelTransport.SHARED_MEMORY_VIDEO
            and self.pixel_format is not VideoPixelFormat.DYNAMIC
        ):
            raise ValueError("Dynamic shared-memory channels require dynamic pixels")
        if (
            self.transport is not FrameChannelTransport.SHARED_MEMORY_VIDEO
            and self.pixel_format is VideoPixelFormat.DYNAMIC
        ):
            raise ValueError("Dynamic pixels require a dynamic shared-memory channel")
        if not isinstance(self.color_space, VideoColorSpace):
            raise ValueError("Invalid frame color space")
        if not isinstance(self.color_range, VideoColorRange):
            raise ValueError("Invalid frame color range")


@dataclass(frozen=True, slots=True)
class OutputWindowTarget:
    bus_id: BusId
    target_id: str
    screen_id: str
    native_handle: int
    x: int
    y: int
    width: int
    height: int
    device_pixel_ratio: float
    visible: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.bus_id, BusId):
            raise ValueError("Invalid window render bus")
        _identity(self.target_id, "window target id")
        _identity(self.screen_id, "screen id")
        _bounded_int(self.native_handle, 1, 2**64 - 1, "window native handle")
        _bounded_int(self.x, -(2**31), 2**31 - 1, "window x")
        _bounded_int(self.y, -(2**31), 2**31 - 1, "window y")
        _bounded_int(self.width, 1, 32_768, "window width")
        _bounded_int(self.height, 1, 32_768, "window height")
        if (
            isinstance(self.device_pixel_ratio, bool)
            or not isinstance(self.device_pixel_ratio, (int, float))
            or not 0.5 <= self.device_pixel_ratio <= 8.0
        ):
            raise ValueError("Invalid window device pixel ratio")
        if not isinstance(self.visible, bool):
            raise ValueError("Window visibility must be a boolean")


@dataclass(frozen=True, slots=True)
class SceneEngineSnapshot:
    session_id: str
    sequence: int
    document: SceneDocument
    active_scenes: tuple[tuple[BusId, str], ...]
    render_enabled: tuple[tuple[BusId, bool], ...]
    output_enabled: tuple[tuple[BusId, bool], ...]
    content_ingress: FrameChannelDescriptor | None = None
    preview_egress: FrameChannelDescriptor | None = None
    program_egress: FrameChannelDescriptor | None = None
    window_targets: tuple[OutputWindowTarget, ...] = ()

    def __post_init__(self) -> None:
        _identity(self.session_id, "engine session id")
        _non_negative_int(self.sequence, "engine sequence")
        if not isinstance(self.document, SceneDocument):
            raise ValueError("Snapshot document must be a SceneDocument")
        _validate_bus_pairs(self.active_scenes, value_type=str, field_name="active scenes")
        _validate_bus_pairs(
            self.render_enabled,
            value_type=bool,
            field_name="render enabled state",
        )
        _validate_bus_pairs(
            self.output_enabled,
            value_type=bool,
            field_name="output enabled state",
        )
        scene_ids = {scene.id for scene in self.document.scenes}
        if any(scene_id not in scene_ids for _, scene_id in self.active_scenes):
            raise ValueError("Snapshot references an unknown active scene")
        if self.content_ingress is not None and not isinstance(
            self.content_ingress,
            FrameChannelDescriptor,
        ):
            raise ValueError("Invalid content ingress descriptor")
        for descriptor, field_name in (
            (self.preview_egress, "preview egress"),
            (self.program_egress, "program egress"),
        ):
            if descriptor is not None and not isinstance(
                descriptor,
                FrameChannelDescriptor,
            ):
                raise ValueError(f"Invalid {field_name} descriptor")
        if (
            not isinstance(self.window_targets, tuple)
            or len(self.window_targets) > MAXIMUM_OUTPUT_WINDOW_TARGETS
            or not all(isinstance(target, OutputWindowTarget) for target in self.window_targets)
        ):
            raise ValueError("Window targets must be an immutable target tuple")
        target_ids = [target.target_id for target in self.window_targets]
        if len(target_ids) != len(set(target_ids)):
            raise ValueError("Window target ids must be unique")


@dataclass(frozen=True, slots=True)
class SceneEngineAck:
    request_id: str
    session_id: str
    process_generation: str
    sequence: int
    document_revision: int
    applied: bool
    error_code: str = ""
    error_message: str = ""

    def __post_init__(self) -> None:
        _identity(self.request_id, "request id")
        _identity(self.session_id, "session id")
        _identity(self.process_generation, "process generation")
        _non_negative_int(self.sequence, "ack sequence")
        _non_negative_int(self.document_revision, "ack document revision")
        if not isinstance(self.applied, bool):
            raise ValueError("Ack applied must be a boolean")
        if not isinstance(self.error_code, str) or not isinstance(self.error_message, str):
            raise ValueError("Ack error fields must be strings")
        if self.applied and (self.error_code or self.error_message):
            raise ValueError("A successful ack cannot contain an error")
        if not self.applied and not self.error_code:
            raise ValueError("A rejected ack must contain an error code")


@dataclass(frozen=True, slots=True)
class ScenePreparation:
    request_id: str
    session_id: str
    process_generation: str
    sequence: int
    document_revision: int
    bus_id: BusId
    scene_id: str
    preparation_token: str

    def __post_init__(self) -> None:
        _identity(self.request_id, "request id")
        _identity(self.session_id, "session id")
        _identity(self.process_generation, "process generation")
        _non_negative_int(self.sequence, "preparation sequence")
        _non_negative_int(self.document_revision, "preparation document revision")
        if not isinstance(self.bus_id, BusId):
            raise ValueError("Invalid preparation bus")
        _identity(self.scene_id, "preparation scene id")
        _identity(self.preparation_token, "preparation token")


@dataclass(frozen=True, slots=True)
class SceneEngineHealth:
    status: SceneEngineStatus
    session_id: str
    process_generation: str
    process_id: int | None
    last_heartbeat_monotonic: float | None
    restart_count: int
    message: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.status, SceneEngineStatus):
            raise ValueError("Invalid scene engine status")
        _bounded_text(self.session_id, 256, "health session id")
        _bounded_text(self.process_generation, 256, "health process generation")
        if self.process_id is not None:
            _positive_int(self.process_id, "health process id")
        if self.last_heartbeat_monotonic is not None and (
            isinstance(self.last_heartbeat_monotonic, bool)
            or not isinstance(self.last_heartbeat_monotonic, (int, float))
            or not isfinite(self.last_heartbeat_monotonic)
            or self.last_heartbeat_monotonic < 0
        ):
            raise ValueError("Invalid health heartbeat")
        _non_negative_int(self.restart_count, "health restart count")
        _bounded_text(self.message, 1024, "health message")


@dataclass(frozen=True, slots=True)
class SceneEngineMetrics:
    request_count: int
    timeout_count: int
    rejected_count: int
    protocol_error_count: int
    restart_count: int
    latency_sample_count: int
    latency_p50_ms: float
    latency_p95_ms: float
    latency_maximum_ms: float

    def __post_init__(self) -> None:
        for value, field_name in (
            (self.request_count, "request count"),
            (self.timeout_count, "timeout count"),
            (self.rejected_count, "rejected count"),
            (self.protocol_error_count, "protocol error count"),
            (self.restart_count, "restart count"),
            (self.latency_sample_count, "latency sample count"),
        ):
            _non_negative_int(value, field_name)
        for value, field_name in (
            (self.latency_p50_ms, "P50 request latency"),
            (self.latency_p95_ms, "P95 request latency"),
            (self.latency_maximum_ms, "maximum request latency"),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not isfinite(value)
                or value < 0
            ):
                raise ValueError(f"Invalid {field_name}")
        if not self.latency_p50_ms <= self.latency_p95_ms <= self.latency_maximum_ms:
            raise ValueError("Scene engine latency percentiles must be monotonic")
        if self.latency_sample_count > self.request_count:
            raise ValueError("Scene engine latency samples cannot exceed request count")


@dataclass(frozen=True, slots=True)
class SourceHealthEvent:
    source_id: str
    status: SourceHealthStatus
    error_code: str = ""
    message: str = ""

    def __post_init__(self) -> None:
        _identity(self.source_id, "source health id")
        if not isinstance(self.status, SourceHealthStatus):
            raise ValueError("Invalid source health status")
        _bounded_text(self.error_code, 128, "source health error code")
        _bounded_text(self.message, 1024, "source health message")
        if self.status is SourceHealthStatus.FAILED and not self.error_code:
            raise ValueError("A failed source health event requires an error code")
        if self.status is SourceHealthStatus.READY and self.error_code:
            raise ValueError("A ready source health event cannot contain an error code")


@dataclass(frozen=True, slots=True)
class EngineHealthEvent:
    health: SceneEngineHealth

    def __post_init__(self) -> None:
        if not isinstance(self.health, SceneEngineHealth):
            raise ValueError("Invalid scene engine health event")


SceneEngineEvent: TypeAlias = SourceHealthEvent | EngineHealthEvent


class SceneEngine(Protocol):
    @property
    def health(self) -> SceneEngineHealth: ...

    @property
    def metrics(self) -> SceneEngineMetrics: ...

    def subscribe(
        self,
        listener: Callable[[SceneEngineEvent], None],
    ) -> Callable[[], None]: ...

    def start(
        self,
        *,
        session_id: str,
        deadline_ms: int,
    ) -> Future[SceneEngineCapabilities]: ...

    def list_local_cameras(
        self,
        *,
        request_id: str,
        deadline_ms: int,
    ) -> Future[LocalCameraDiscovery]: ...

    def hydrate(
        self,
        snapshot: SceneEngineSnapshot,
        *,
        request_id: str,
        deadline_ms: int,
    ) -> Future[SceneEngineAck]: ...

    def prepare_scene(
        self,
        bus_id: BusId,
        scene_id: str,
        *,
        document_revision: int,
        request_id: str,
        sequence: int,
        deadline_ms: int,
    ) -> Future[ScenePreparation]: ...

    def take_prepared(
        self,
        preparation: ScenePreparation,
        *,
        transition: TransitionKind,
        transition_duration_ms: int,
        request_id: str,
        sequence: int,
        deadline_ms: int,
    ) -> Future[SceneEngineAck]: ...

    def cancel_preparation(self, request_id: str) -> None: ...

    def preview_layer_geometry(
        self,
        bus_id: BusId,
        scene_id: str,
        layer: SceneLayer,
        *,
        document_revision: int,
        request_id: str,
        sequence: int,
        deadline_ms: int,
    ) -> Future[SceneEngineAck]: ...

    def set_output_enabled(
        self,
        bus_id: BusId,
        enabled: bool,
        *,
        request_id: str,
        sequence: int,
        deadline_ms: int,
    ) -> Future[SceneEngineAck]: ...

    def set_render_enabled(
        self,
        bus_id: BusId,
        enabled: bool,
        *,
        request_id: str,
        sequence: int,
        deadline_ms: int,
    ) -> Future[SceneEngineAck]: ...

    def stop(self) -> None: ...


def _validate_bus_pairs(
    pairs: object,
    *,
    value_type: type,
    field_name: str,
) -> None:
    if not isinstance(pairs, tuple) or not all(
        isinstance(pair, tuple)
        and len(pair) == 2
        and isinstance(pair[0], BusId)
        and isinstance(pair[1], value_type)
        and (value_type is not bool or type(pair[1]) is bool)
        for pair in pairs
    ):
        raise ValueError(f"{field_name} must be an immutable typed bus tuple")
    buses = tuple(pair[0] for pair in pairs)
    if len(buses) != len(BusId) or set(buses) != set(BusId):
        raise ValueError(f"{field_name} must define every output bus once")


def _identity(value: object, field_name: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 256
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError(f"Invalid {field_name}")


def _bounded_text(value: object, maximum: int, field_name: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) > maximum
        or any(ord(character) < 32 and character not in "\t\n\r" for character in value)
    ):
        raise ValueError(f"Invalid {field_name}")


def _booleans(*values: object) -> None:
    if not all(type(value) is bool for value in values):
        raise ValueError("Capability flags must be booleans")


def _positive_int(value: object, field_name: str) -> None:
    _bounded_int(value, 1, 2**63 - 1, field_name)


def _non_negative_int(value: object, field_name: str) -> None:
    _bounded_int(value, 0, 2**63 - 1, field_name)


def _bounded_int(value: object, minimum: int, maximum: int, field_name: str) -> None:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"Invalid {field_name}")
