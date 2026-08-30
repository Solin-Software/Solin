from __future__ import annotations

import logging
import os
import random
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, TypeVar, cast
from uuid import uuid4

from solin.core.scenes.engine import (
    DEFAULT_ENGINE_STARTUP_DEADLINE_MS,
    MAXIMUM_OUTPUT_WINDOW_TARGETS,
    EngineHealthEvent,
    FrameChannelDescriptor,
    LocalCameraDevice,
    LocalCameraDiscovery,
    LocalCameraProbe,
    LocalCameraProbeStatus,
    LocalVideoFormat,
    OutputWindowTarget,
    SceneEngineAck,
    SceneEngineCapabilities,
    SceneEngineEvent,
    SceneEngineHealth,
    SceneEngineMetrics,
    SceneEngineSnapshot,
    SceneEngineStatus,
    ScenePreparation,
    ProgramRecordingEvent,
    SourceHealthEvent,
    SourceHealthStatus,
    scene_engine_document_record,
)
from solin.core.scenes.recording import (
    MAXIMUM_AUDIO_DEVICES,
    MAXIMUM_AUDIO_DEVICE_ID_LENGTH,
    MAXIMUM_AUDIO_DEVICE_NAME_LENGTH,
    AudioDevice,
    AudioDeviceDirection,
    AudioDeviceDiscovery,
    AudioDeviceSelection,
    ProgramRecordingNativeState,
    ProgramRecordingRequest,
    ProgramRecordingStatus,
)
from solin.core.scenes.ipc_protocol import (
    PROTOCOL_VERSION,
    SceneIpcEnvelope,
    SceneIpcError,
    SceneIpcMessageError,
    read_envelope,
    require_bool,
    require_non_negative_int,
    require_payload_fields,
    require_text,
    write_envelope,
)
from solin.core.scenes.model import (
    BusId,
    CameraMediaType,
    SceneLayer,
    TransitionKind,
    TransitionSpec,
)


_SUPERVISOR_TICK_SECONDS = 0.05
_HELLO_FIELDS = frozenset(
    {
        "local_cameras",
        "rtsp_cameras",
        "hardware_compositing",
        "virtual_camera",
        "d3d11_shared_textures",
        "program_recording",
        "audio_input_capture",
        "system_audio_capture",
    }
)
_ACK_FIELDS = frozenset({"applied", "error_code", "error_message"})
_ERROR_FIELDS = frozenset({"error_code", "error_message"})
_PREPARATION_FIELDS = frozenset(
    {
        "bus_id",
        "scene_id",
        "preparation_token",
        "transition",
        "fallback_applied",
        "fallback_reason",
    }
)
_TRANSITION_FIELDS = frozenset({"kind", "duration_ms"})
_HEARTBEAT_FIELDS = frozenset({"monotonic_ms"})
_SOURCE_HEALTH_FIELDS = frozenset({"source_id", "status", "error_code", "message"})
_LOCAL_CAMERA_LIST_FIELDS = frozenset({"supported", "ready", "generation", "devices", "error_code"})
_LOCAL_CAMERA_FIELDS = frozenset(
    {"device_id", "display_name", "software_device", "formats", "probe"}
)
_LOCAL_CAMERA_PROBE_FIELDS = frozenset(
    {"status", "backend", "failure_stage", "error_code", "native_error_code"}
)
_LOCAL_VIDEO_FORMAT_FIELDS = frozenset(
    {
        "media_type",
        "pixel_format",
        "width",
        "height",
        "fps_numerator",
        "fps_denominator",
    }
)
_AUDIO_DEVICE_LIST_FIELDS = frozenset(
    {"supported", "ready", "generation", "devices", "error_code"}
)
_AUDIO_DEVICE_FIELDS = frozenset(
    {"device_id", "display_name", "direction", "is_default"}
)
_PROGRAM_RECORDING_STATE_FIELDS = frozenset(
    {
        "status",
        "path",
        "error_code",
        "message",
        "microphone_warning",
        "system_audio_warning",
        "dropped_frames",
        "duplicated_frames",
        "frame_feed_p95_ns",
    }
)
_T = TypeVar("_T")
log = logging.getLogger(__name__)


class SceneEngineProcessError(RuntimeError):
    """Base class for failures at the isolated scene-engine boundary."""


class SceneEngineNotReadyError(SceneEngineProcessError):
    """Raised when a command is issued while no engine generation is ready."""


class SceneEngineRequestTimeoutError(SceneEngineProcessError):
    """Raised when the engine does not answer before a command deadline."""


class SceneEngineProtocolError(SceneEngineProcessError):
    """Raised when the child violates the versioned control protocol."""


class SceneEngineCommandRejectedError(SceneEngineProcessError):
    """Raised when a valid command cannot be performed by the current engine."""

    def __init__(self, error_code: str) -> None:
        super().__init__("Scene engine command was rejected")
        self.error_code = error_code


@dataclass(frozen=True, slots=True)
class SceneEngineProcessConfig:
    executable: Path
    arguments: tuple[str, ...] = ()
    gstreamer_runtime_root: Path | None = None
    gstreamer_registry_path: Path | None = None
    hello_timeout_ms: int = DEFAULT_ENGINE_STARTUP_DEADLINE_MS
    heartbeat_interval_ms: int = 500
    heartbeat_timeout_ms: int = 2000
    stop_timeout_ms: int = 1500
    maximum_restarts: int = 3
    restart_window_seconds: float = 60.0
    restart_initial_delay_ms: int = 250
    restart_maximum_delay_ms: int = 4000
    restart_jitter_ratio: float = 0.2

    def __post_init__(self) -> None:
        if not isinstance(self.executable, Path):
            raise TypeError("Scene engine executable must be a Path")
        if not isinstance(self.arguments, tuple) or not all(
            isinstance(argument, str) and argument for argument in self.arguments
        ):
            raise ValueError("Scene engine arguments must be a non-empty string tuple")
        if self.gstreamer_runtime_root is not None and not isinstance(
            self.gstreamer_runtime_root, Path
        ):
            raise TypeError("GStreamer runtime root must be a Path")
        if self.gstreamer_registry_path is not None and not isinstance(
            self.gstreamer_registry_path, Path
        ):
            raise TypeError("GStreamer registry path must be a Path")
        if (self.gstreamer_runtime_root is None) != (self.gstreamer_registry_path is None):
            raise ValueError("GStreamer runtime and registry paths must be configured together")
        for value, field_name in (
            (self.hello_timeout_ms, "hello timeout"),
            (self.heartbeat_interval_ms, "heartbeat interval"),
            (self.heartbeat_timeout_ms, "heartbeat timeout"),
            (self.stop_timeout_ms, "stop timeout"),
            (self.restart_initial_delay_ms, "restart initial delay"),
            (self.restart_maximum_delay_ms, "restart maximum delay"),
        ):
            if type(value) is not int or value <= 0:
                raise ValueError(f"Invalid {field_name}")
        if self.heartbeat_timeout_ms <= self.heartbeat_interval_ms:
            raise ValueError("Heartbeat timeout must exceed its interval")
        if self.restart_maximum_delay_ms < self.restart_initial_delay_ms:
            raise ValueError("Restart maximum delay must not be shorter than its initial delay")
        if type(self.maximum_restarts) is not int or self.maximum_restarts < 0:
            raise ValueError("Invalid maximum restart count")
        if (
            isinstance(self.restart_window_seconds, bool)
            or not isinstance(self.restart_window_seconds, (int, float))
            or not self.restart_window_seconds > 0
        ):
            raise ValueError("Invalid restart window")
        if (
            isinstance(self.restart_jitter_ratio, bool)
            or not isinstance(self.restart_jitter_ratio, (int, float))
            or not 0 <= self.restart_jitter_ratio <= 1
        ):
            raise ValueError("Invalid restart jitter ratio")


@dataclass(slots=True)
class _PendingRequest:
    expected_message_type: str
    session_id: str
    process_generation: str
    sequence: int
    document_revision: int
    deadline_monotonic_ms: int
    started_monotonic: float
    is_liveness_probe: bool
    converter: Callable[[SceneIpcEnvelope], object]
    future: Future[object]


class SubprocessSceneEngine:
    """Supervised, non-blocking adapter for the isolated native media engine."""

    def __init__(
        self,
        config: SceneEngineProcessConfig,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        random_uniform: Callable[[float, float], float] | None = None,
    ) -> None:
        self._config = config
        self._monotonic = monotonic
        self._random_uniform = random_uniform or random.SystemRandom().uniform
        self._lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._listeners: set[Callable[[SceneEngineEvent], None]] = set()
        self._pending: dict[str, _PendingRequest] = {}
        self._latency_samples_ms: deque[float] = deque(maxlen=1024)
        self._request_count = 0
        self._timeout_count = 0
        self._rejected_count = 0
        self._protocol_error_count = 0
        self._process: subprocess.Popen[bytes] | None = None
        self._supervisor: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._generation_failed = threading.Event()
        self._session_id = "not-started"
        self._process_generation = "not-started"
        self._last_received_monotonic: float | None = None
        self._last_command_timeout_monotonic: float | None = None
        self._document_revision = 0
        self._heartbeat_request_id: str | None = None
        self._restart_count = 0
        self._restart_times: deque[float] = deque()
        self._started = False
        self._ready = False
        self._start_future: Future[SceneEngineCapabilities] | None = None
        self._health = SceneEngineHealth(
            status=SceneEngineStatus.STOPPED,
            session_id=self._session_id,
            process_generation=self._process_generation,
            process_id=None,
            last_heartbeat_monotonic=None,
            restart_count=0,
        )

    @property
    def health(self) -> SceneEngineHealth:
        with self._lock:
            return self._health

    @property
    def metrics(self) -> SceneEngineMetrics:
        with self._lock:
            samples = tuple(self._latency_samples_ms)
            request_count = self._request_count
            timeout_count = self._timeout_count
            rejected_count = self._rejected_count
            protocol_error_count = self._protocol_error_count
            restart_count = self._restart_count
        ordered = sorted(samples)
        return SceneEngineMetrics(
            request_count=request_count,
            timeout_count=timeout_count,
            rejected_count=rejected_count,
            protocol_error_count=protocol_error_count,
            restart_count=restart_count,
            latency_sample_count=len(ordered),
            latency_p50_ms=_percentile(ordered, 0.50),
            latency_p95_ms=_percentile(ordered, 0.95),
            latency_maximum_ms=ordered[-1] if ordered else 0.0,
        )

    def subscribe(
        self,
        listener: Callable[[SceneEngineEvent], None],
    ) -> Callable[[], None]:
        if not callable(listener):
            raise TypeError("Scene engine listener must be callable")
        with self._lock:
            self._listeners.add(listener)

        def unsubscribe() -> None:
            with self._lock:
                self._listeners.discard(listener)

        return unsubscribe

    def start(
        self,
        *,
        session_id: str,
        deadline_ms: int,
    ) -> Future[SceneEngineCapabilities]:
        _validate_identity(session_id, "session id")
        _validate_deadline(deadline_ms)
        with self._lock:
            if self._started:
                return _failed_future(SceneEngineProcessError("Scene engine is already started"))
            if self._supervisor is not None and self._supervisor.is_alive():
                return _failed_future(SceneEngineProcessError("Scene engine is still stopping"))
            self._started = True
            self._session_id = session_id
            self._restart_count = 0
            self._restart_times.clear()
            self._document_revision = 0
            self._heartbeat_request_id = None
            self._latency_samples_ms.clear()
            self._request_count = 0
            self._timeout_count = 0
            self._rejected_count = 0
            self._protocol_error_count = 0
            self._stop_event.clear()
            self._generation_failed.clear()
            future: Future[SceneEngineCapabilities] = Future()
            self._start_future = future
            self._supervisor = threading.Thread(
                target=self._supervise,
                args=(min(deadline_ms, self._config.hello_timeout_ms),),
                name="solin-scene-engine-supervisor",
                daemon=True,
            )
            self._supervisor.start()
            return future

    def hydrate(
        self,
        snapshot: SceneEngineSnapshot,
        *,
        request_id: str,
        deadline_ms: int,
    ) -> Future[SceneEngineAck]:
        if not isinstance(snapshot, SceneEngineSnapshot):
            return _failed_future(TypeError("Invalid scene engine snapshot"))
        payload: dict[str, object] = {
            "document": scene_engine_document_record(snapshot.document),
            "active_scenes": {
                bus_id.value: scene_id for bus_id, scene_id in snapshot.active_scenes
            },
            "render_enabled": {
                bus_id.value: enabled for bus_id, enabled in snapshot.render_enabled
            },
            "output_enabled": {
                bus_id.value: enabled for bus_id, enabled in snapshot.output_enabled
            },
            "content_ingress": _frame_channel_record(snapshot.content_ingress),
            "preview_egress": _frame_channel_record(snapshot.preview_egress),
            "program_egress": _frame_channel_record(snapshot.program_egress),
            "window_targets": [
                _window_target_record(target) for target in snapshot.window_targets
            ],
        }
        return self._request(
            message_type="hydrate",
            expected_message_type="ack",
            request_id=request_id,
            sequence=snapshot.sequence,
            document_revision=snapshot.document.revision,
            deadline_ms=deadline_ms,
            payload=payload,
            converter=lambda envelope: self._hydration_ack_from_envelope(
                envelope,
                snapshot.document.revision,
            ),
        )

    def list_local_cameras(
        self,
        *,
        request_id: str,
        deadline_ms: int,
    ) -> Future[LocalCameraDiscovery]:
        return self._request(
            message_type="list_local_cameras",
            expected_message_type="local_camera_list",
            request_id=request_id,
            sequence=0,
            document_revision=0,
            deadline_ms=deadline_ms,
            payload={},
            converter=_local_camera_discovery_from_envelope,
        )

    def list_audio_devices(
        self,
        *,
        request_id: str,
        deadline_ms: int,
    ) -> Future[AudioDeviceDiscovery]:
        return self._request(
            message_type="list_audio_devices",
            expected_message_type="audio_device_list",
            request_id=request_id,
            sequence=0,
            document_revision=0,
            deadline_ms=deadline_ms,
            payload={},
            converter=_audio_device_discovery_from_envelope,
        )

    def _hydration_ack_from_envelope(
        self,
        envelope: SceneIpcEnvelope,
        document_revision: int,
    ) -> SceneEngineAck:
        acknowledgement = _ack_from_envelope(envelope)
        if acknowledgement.applied:
            with self._lock:
                self._document_revision = document_revision
        return acknowledgement

    def prepare_scene(
        self,
        bus_id: BusId,
        scene_id: str,
        *,
        transition: TransitionSpec,
        document_revision: int,
        request_id: str,
        sequence: int,
        deadline_ms: int,
    ) -> Future[ScenePreparation]:
        if not isinstance(bus_id, BusId):
            return _failed_future(TypeError("Invalid output bus"))
        if not isinstance(transition, TransitionSpec):
            return _failed_future(TypeError("Invalid scene transition"))
        _validate_identity(scene_id, "scene id")
        return self._request(
            message_type="prepare_scene",
            expected_message_type="scene_prepared",
            request_id=request_id,
            sequence=sequence,
            document_revision=document_revision,
            deadline_ms=deadline_ms,
            payload={
                "bus_id": bus_id.value,
                "scene_id": scene_id,
                "transition": transition.to_record(),
            },
            converter=_preparation_from_envelope,
        )

    def take_prepared(
        self,
        preparation: ScenePreparation,
        *,
        request_id: str,
        sequence: int,
        deadline_ms: int,
    ) -> Future[SceneEngineAck]:
        if not isinstance(preparation, ScenePreparation):
            return _failed_future(TypeError("Invalid scene preparation"))
        return self._request(
            message_type="take_prepared",
            expected_message_type="ack",
            request_id=request_id,
            sequence=sequence,
            document_revision=preparation.document_revision,
            deadline_ms=deadline_ms,
            payload={
                "bus_id": preparation.bus_id.value,
                "scene_id": preparation.scene_id,
                "preparation_token": preparation.preparation_token,
            },
            converter=_ack_from_envelope,
        )

    def cancel_preparation(self, request_id: str) -> None:
        _validate_identity(request_id, "request id")
        with self._lock:
            pending = self._pending.pop(request_id, None)
        if pending is not None and not pending.future.done():
            pending.future.cancel()
        self._notify(
            message_type="cancel_preparation",
            payload={"cancelled_request_id": request_id},
        )

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
    ) -> Future[SceneEngineAck]:
        if not isinstance(bus_id, BusId):
            return _failed_future(TypeError("Invalid preview bus"))
        if not isinstance(layer, SceneLayer):
            return _failed_future(TypeError("Invalid preview layer"))
        try:
            _validate_identity(scene_id, "scene id")
        except (TypeError, ValueError) as exc:
            return _failed_future(exc)
        return self._request(
            message_type="preview_layer_geometry",
            expected_message_type="ack",
            request_id=request_id,
            sequence=sequence,
            document_revision=document_revision,
            deadline_ms=deadline_ms,
            payload={
                "bus_id": bus_id.value,
                "scene_id": scene_id,
                "layer": layer.to_record(),
            },
            converter=_ack_from_envelope,
        )

    def set_output_enabled(
        self,
        bus_id: BusId,
        enabled: bool,
        *,
        request_id: str,
        sequence: int,
        deadline_ms: int,
    ) -> Future[SceneEngineAck]:
        if not isinstance(bus_id, BusId):
            return _failed_future(TypeError("Invalid output bus"))
        if type(enabled) is not bool:
            return _failed_future(TypeError("Output enabled state must be a boolean"))
        with self._lock:
            document_revision = self._document_revision
        return self._request(
            message_type="set_output_enabled",
            expected_message_type="ack",
            request_id=request_id,
            sequence=sequence,
            document_revision=document_revision,
            deadline_ms=deadline_ms,
            payload={"bus_id": bus_id.value, "enabled": enabled},
            converter=_ack_from_envelope,
        )

    def set_render_enabled(
        self,
        bus_id: BusId,
        enabled: bool,
        *,
        request_id: str,
        sequence: int,
        deadline_ms: int,
    ) -> Future[SceneEngineAck]:
        if not isinstance(bus_id, BusId):
            return _failed_future(TypeError("Invalid render bus"))
        if type(enabled) is not bool:
            return _failed_future(TypeError("Render enabled state must be a boolean"))
        with self._lock:
            document_revision = self._document_revision
        return self._request(
            message_type="set_render_enabled",
            expected_message_type="ack",
            request_id=request_id,
            sequence=sequence,
            document_revision=document_revision,
            deadline_ms=deadline_ms,
            payload={"bus_id": bus_id.value, "enabled": enabled},
            converter=_ack_from_envelope,
        )

    def set_window_targets(
        self,
        targets: tuple[OutputWindowTarget, ...],
        *,
        request_id: str,
        sequence: int,
        deadline_ms: int,
    ) -> Future[SceneEngineAck]:
        if (
            not isinstance(targets, tuple)
            or len(targets) > MAXIMUM_OUTPUT_WINDOW_TARGETS
            or not all(isinstance(target, OutputWindowTarget) for target in targets)
        ):
            return _failed_future(TypeError("Invalid window targets"))
        target_ids = tuple(target.target_id for target in targets)
        if len(target_ids) != len(set(target_ids)):
            return _failed_future(ValueError("Window target ids must be unique"))
        with self._lock:
            document_revision = self._document_revision
        return self._request(
            message_type="set_window_targets",
            expected_message_type="ack",
            request_id=request_id,
            sequence=sequence,
            document_revision=document_revision,
            deadline_ms=deadline_ms,
            payload={
                "window_targets": [
                    _window_target_record(target) for target in targets
                ]
            },
            converter=_ack_from_envelope,
        )

    def start_program_recording(
        self,
        recording: ProgramRecordingRequest,
        *,
        request_id: str,
        deadline_ms: int,
    ) -> Future[SceneEngineAck]:
        if not isinstance(recording, ProgramRecordingRequest):
            return _failed_future(TypeError("Invalid Program recording request"))
        with self._lock:
            document_revision = self._document_revision
        return self._request(
            message_type="start_program_recording",
            expected_message_type="ack",
            request_id=request_id,
            sequence=0,
            document_revision=document_revision,
            deadline_ms=deadline_ms,
            payload={
                "path": str(recording.path),
                "width": recording.width,
                "height": recording.height,
                "fps_numerator": recording.fps_numerator,
                "fps_denominator": recording.fps_denominator,
                "microphone": recording.microphone.to_engine_record(),
                "system_audio": recording.system_audio.to_engine_record(),
            },
            converter=_ack_from_envelope,
        )

    def set_program_recording_audio(
        self,
        microphone: AudioDeviceSelection,
        system_audio: AudioDeviceSelection,
        *,
        request_id: str,
        deadline_ms: int,
    ) -> Future[SceneEngineAck]:
        if not isinstance(microphone, AudioDeviceSelection) or not isinstance(
            system_audio,
            AudioDeviceSelection,
        ):
            return _failed_future(TypeError("Invalid Program recording audio configuration"))
        with self._lock:
            document_revision = self._document_revision
        return self._request(
            message_type="set_program_recording_audio",
            expected_message_type="ack",
            request_id=request_id,
            sequence=0,
            document_revision=document_revision,
            deadline_ms=deadline_ms,
            payload={
                "microphone": microphone.to_engine_record(),
                "system_audio": system_audio.to_engine_record(),
            },
            converter=_ack_from_envelope,
        )

    def stop_program_recording(
        self,
        *,
        request_id: str,
        deadline_ms: int,
    ) -> Future[SceneEngineAck]:
        with self._lock:
            document_revision = self._document_revision
        return self._request(
            message_type="stop_program_recording",
            expected_message_type="ack",
            request_id=request_id,
            sequence=0,
            document_revision=document_revision,
            deadline_ms=deadline_ms,
            payload={},
            converter=_ack_from_envelope,
        )

    def stop(self) -> None:
        with self._lock:
            supervisor = self._supervisor
            if not self._started and (supervisor is None or not supervisor.is_alive()):
                return
            notify = self._started and self._ready
        if notify:
            self._notify(message_type="stop", payload={})
        with self._lock:
            self._started = False
            self._ready = False
        self._stop_event.set()
        self._fail_all(SceneEngineProcessError("Scene engine stopped"))
        self._fail_start(SceneEngineProcessError("Scene engine stopped"))
        if supervisor is None or supervisor is threading.current_thread():
            return
        shutdown_budget = (self._config.stop_timeout_ms * 2 + 500) / 1000.0
        supervisor.join(timeout=shutdown_budget)
        if supervisor.is_alive():
            process = self._current_process()
            if process is not None:
                self._terminate_process(process)
            supervisor.join(timeout=self._config.stop_timeout_ms / 1000.0)
        if supervisor.is_alive():
            log.warning("Scene engine supervisor exceeded its shutdown budget")
        else:
            with self._lock:
                if self._supervisor is supervisor:
                    self._supervisor = None

    def _supervise(self, hello_timeout_ms: int) -> None:
        first_generation = True
        restart_attempt = 0
        while not self._stop_event.is_set():
            generation = uuid4().hex
            with self._lock:
                self._process_generation = generation
                self._last_received_monotonic = None
                self._last_command_timeout_monotonic = None
                self._heartbeat_request_id = None
            self._generation_failed.clear()
            self._publish_health(SceneEngineStatus.STARTING, "engine_starting")
            try:
                process = self._launch_process(generation)
            except OSError:
                if self._stop_event.is_set():
                    self._publish_health(SceneEngineStatus.STOPPED, "")
                    return
                if not self._recover_or_fail(first_generation, "engine_launch_failed"):
                    return
                first_generation = False
                restart_attempt += 1
                self._wait_before_restart(restart_attempt)
                continue

            reader = threading.Thread(
                target=self._read_stdout,
                args=(process, generation),
                name=f"solin-scene-engine-reader-{generation[:8]}",
                daemon=True,
            )
            stderr_reader = threading.Thread(
                target=self._drain_stderr,
                args=(process,),
                name=f"solin-scene-engine-stderr-{generation[:8]}",
                daemon=True,
            )
            reader.start()
            stderr_reader.start()

            hello = self._request(
                message_type="hello",
                expected_message_type="hello_ack",
                request_id=uuid4().hex,
                sequence=0,
                document_revision=0,
                deadline_ms=hello_timeout_ms,
                payload={"parent_process_id": os.getpid()},
                converter=_capabilities_from_envelope,
                require_ready=False,
            )
            try:
                capabilities = hello.result(timeout=hello_timeout_ms / 1000.0)
            except (FutureTimeoutError, SceneEngineProcessError, SceneIpcError):
                self._terminate_process(process)
                self._fail_generation(generation, SceneEngineProtocolError("Handshake failed"))
                if self._stop_event.is_set():
                    self._publish_health(SceneEngineStatus.STOPPED, "")
                    return
                if not self._recover_or_fail(first_generation, "engine_handshake_failed"):
                    return
                first_generation = False
                restart_attempt += 1
                self._wait_before_restart(restart_attempt)
                continue

            with self._lock:
                self._ready = True
                self._last_received_monotonic = self._monotonic()
                start_future = self._start_future
                if start_future is not None and not start_future.done():
                    start_future.set_result(capabilities)
            self._publish_health(SceneEngineStatus.READY, "")
            first_generation = False
            restart_attempt = 0
            next_heartbeat = self._monotonic()

            while not self._stop_event.wait(_SUPERVISOR_TICK_SECONDS):
                now = self._monotonic()
                self._expire_requests(now)
                if process.poll() is not None or self._generation_failed.is_set():
                    break
                with self._lock:
                    last_received = self._last_received_monotonic
                command_deadline = self._command_liveness_deadline()
                heartbeat_timed_out = (
                    last_received is None
                    or now - last_received > self._config.heartbeat_timeout_ms / 1000.0
                )
                if heartbeat_timed_out and (
                    command_deadline is None or now > command_deadline
                ):
                    self._generation_failed.set()
                    break
                if command_deadline is None and now >= next_heartbeat:
                    self._send_heartbeat()
                    next_heartbeat = now + self._config.heartbeat_interval_ms / 1000.0

            if self._stop_event.is_set():
                self._stop_process_gracefully(process)
                self._publish_health(SceneEngineStatus.STOPPED, "")
                return

            with self._lock:
                self._ready = False
            self._publish_health(SceneEngineStatus.FAILED, "engine_connection_lost")
            self._terminate_process(process)
            self._fail_generation(
                generation,
                SceneEngineProcessError("Scene engine generation ended"),
            )
            if not self._permit_restart():
                self._fail_start(SceneEngineProcessError("Scene engine restart limit reached"))
                return
            restart_attempt += 1
            self._wait_before_restart(restart_attempt)

        process = self._current_process()
        if process is not None:
            self._stop_process_gracefully(process)
        self._publish_health(SceneEngineStatus.STOPPED, "")

    def _launch_process(self, generation: str) -> subprocess.Popen[bytes]:
        command = [str(self._config.executable), *self._config.arguments]
        environment = os.environ.copy()
        environment["SOLIN_MEDIA_ENGINE_PROCESS_GENERATION"] = generation
        environment["SOLIN_MEDIA_ENGINE_PARENT_PROCESS_ID"] = str(os.getpid())
        self._configure_gstreamer_environment(environment)
        creation_flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        process = subprocess.Popen(  # noqa: S603 - executable is application configuration
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            close_fds=True,
            creationflags=creation_flags,
            env=environment,
        )
        if process.stdin is None or process.stdout is None or process.stderr is None:
            process.kill()
            raise OSError("Scene engine pipes are unavailable")
        with self._lock:
            self._process = process
            self._last_received_monotonic = self._monotonic()
        return process

    def _configure_gstreamer_environment(self, environment: dict[str, str]) -> None:
        runtime_root = self._config.gstreamer_runtime_root
        registry_path = self._config.gstreamer_registry_path
        if runtime_root is None or registry_path is None:
            return
        runtime_root = runtime_root.resolve()
        binary_directory = runtime_root / "bin"
        plugin_directory = runtime_root / "lib" / "gstreamer-1.0"
        if not binary_directory.is_dir() or not plugin_directory.is_dir():
            raise OSError("Configured GStreamer runtime is incomplete")
        registry_path = registry_path.resolve()
        registry_path.parent.mkdir(parents=True, exist_ok=True)
        runtime_search_directories = [binary_directory]
        proxy_backend_directory = runtime_root / "lib" / "libproxy"
        if proxy_backend_directory.is_dir():
            runtime_search_directories.append(proxy_backend_directory)
        existing_path = environment.get("PATH")
        environment["PATH"] = os.pathsep.join(
            str(directory)
            for directory in (
                *runtime_search_directories,
                *((existing_path,) if existing_path else ()),
            )
        )
        environment["GST_PLUGIN_PATH_1_0"] = ""
        environment["GST_PLUGIN_SYSTEM_PATH_1_0"] = str(plugin_directory)
        environment["GST_REGISTRY_1_0"] = str(registry_path)
        scanner = runtime_root / "libexec" / "gstreamer-1.0" / "gst-plugin-scanner.exe"
        if scanner.is_file():
            environment["GST_PLUGIN_SCANNER_1_0"] = str(scanner)

    def _read_stdout(self, process: subprocess.Popen[bytes], generation: str) -> None:
        stream = cast(BinaryIO, process.stdout)
        try:
            while not self._stop_event.is_set():
                envelope = read_envelope(stream)
                if envelope is None:
                    break
                self._receive(envelope, generation)
        except OSError:
            pass
        except (SceneIpcError, ValueError):
            with self._lock:
                self._protocol_error_count += 1
        finally:
            if not self._stop_event.is_set() and generation == self._process_generation:
                self._generation_failed.set()

    @staticmethod
    def _drain_stderr(process: subprocess.Popen[bytes]) -> None:
        stream = cast(BinaryIO, process.stderr)
        try:
            while raw_line := stream.readline(8192):
                message = raw_line.decode("utf-8", errors="replace").strip()
                if message:
                    log.warning("Native scene engine: %s", message)
        except OSError:
            pass

    def _receive(self, envelope: SceneIpcEnvelope, generation: str) -> None:
        with self._lock:
            if (
                generation != self._process_generation
                or envelope.session_id != self._session_id
                or envelope.process_generation != generation
            ):
                return
            self._last_received_monotonic = self._monotonic()
            if envelope.message_type in {"source_health", "program_recording_state"}:
                pending = None
            else:
                pending = self._pending.pop(envelope.request_id, None)
        if envelope.message_type == "source_health":
            self._emit_event(_source_health_from_envelope(envelope))
            return
        if envelope.message_type == "program_recording_state":
            self._emit_event(_program_recording_event_from_envelope(envelope))
            return
        if pending is None:
            return
        if pending.future.done():
            return
        self._record_request_latency(pending)
        try:
            self._validate_response(envelope, pending)
            if envelope.message_type == "error":
                with self._lock:
                    self._rejected_count += 1
                pending.future.set_exception(_command_error_from_envelope(envelope))
                return
            result = pending.converter(envelope)
        except (SceneIpcError, ValueError):
            with self._lock:
                self._protocol_error_count += 1
            pending.future.set_exception(SceneEngineProtocolError("Invalid engine response"))
            self._generation_failed.set()
            return
        pending.future.set_result(result)

    @staticmethod
    def _validate_response(envelope: SceneIpcEnvelope, pending: _PendingRequest) -> None:
        if (
            envelope.message_type not in {pending.expected_message_type, "error"}
            or envelope.session_id != pending.session_id
            or envelope.process_generation != pending.process_generation
            or envelope.sequence != pending.sequence
            or envelope.document_revision != pending.document_revision
            or envelope.deadline_monotonic_ms != pending.deadline_monotonic_ms
        ):
            raise SceneIpcMessageError("Scene IPC response does not match its request")

    def _request(
        self,
        *,
        message_type: str,
        expected_message_type: str,
        request_id: str,
        sequence: int,
        document_revision: int,
        deadline_ms: int,
        payload: dict[str, object],
        converter: Callable[[SceneIpcEnvelope], _T],
        require_ready: bool = True,
        is_liveness_probe: bool = False,
    ) -> Future[_T]:
        try:
            _validate_identity(request_id, "request id")
            _validate_non_negative_int(sequence, "sequence")
            _validate_non_negative_int(document_revision, "document revision")
            _validate_deadline(deadline_ms)
        except (TypeError, ValueError) as exc:
            return _failed_future(exc)
        with self._lock:
            if not self._started or (require_ready and not self._ready):
                return _failed_future(SceneEngineNotReadyError("Scene engine is not ready"))
            if request_id in self._pending:
                return _failed_future(SceneEngineProcessError("Duplicate scene engine request id"))
            generation = self._process_generation
            session_id = self._session_id
            deadline_monotonic_ms = int(self._monotonic() * 1000) + deadline_ms
            envelope = SceneIpcEnvelope(
                message_type=message_type,
                request_id=request_id,
                session_id=session_id,
                process_generation=generation,
                sequence=sequence,
                document_revision=document_revision,
                deadline_monotonic_ms=deadline_monotonic_ms,
                payload=payload,
            )
            result: Future[object] = Future()
            self._pending[request_id] = _PendingRequest(
                expected_message_type=expected_message_type,
                session_id=session_id,
                process_generation=generation,
                sequence=sequence,
                document_revision=document_revision,
                deadline_monotonic_ms=deadline_monotonic_ms,
                started_monotonic=self._monotonic(),
                is_liveness_probe=is_liveness_probe,
                converter=cast(Callable[[SceneIpcEnvelope], object], converter),
                future=result,
            )
            self._request_count += 1
        try:
            self._write(envelope)
        except (OSError, SceneIpcError):
            with self._lock:
                self._pending.pop(request_id, None)
            result.set_exception(SceneEngineProcessError("Scene engine command write failed"))
            self._generation_failed.set()
        return cast(Future[_T], result)

    def _notify(self, *, message_type: str, payload: dict[str, object]) -> None:
        with self._lock:
            if not self._started or not self._ready:
                return
            envelope = SceneIpcEnvelope(
                message_type=message_type,
                request_id=uuid4().hex,
                session_id=self._session_id,
                process_generation=self._process_generation,
                sequence=0,
                document_revision=0,
                deadline_monotonic_ms=int(self._monotonic() * 1000) + 1000,
                payload=payload,
            )
        try:
            self._write(envelope)
        except (OSError, SceneIpcError):
            self._generation_failed.set()

    def _write(self, envelope: SceneIpcEnvelope) -> None:
        with self._write_lock:
            with self._lock:
                process = self._process
                stream = None if process is None else process.stdin
            if stream is None:
                raise OSError("Scene engine stdin is unavailable")
            write_envelope(cast(BinaryIO, stream), envelope)

    def _send_heartbeat(self) -> None:
        with self._lock:
            if self._heartbeat_request_id is not None:
                return
            request_id = f"heartbeat-{uuid4().hex}"
            self._heartbeat_request_id = request_id
        future = self._request(
            message_type="ping",
            expected_message_type="heartbeat",
            request_id=request_id,
            sequence=0,
            document_revision=0,
            deadline_ms=self._config.heartbeat_timeout_ms,
            payload={},
            converter=_heartbeat_from_envelope,
            is_liveness_probe=True,
        )

        def completed(result: Future[int]) -> None:
            with self._lock:
                if self._heartbeat_request_id == request_id:
                    self._heartbeat_request_id = None
            try:
                result.result()
            except (SceneEngineProcessError, SceneIpcError):
                command_deadline = self._command_liveness_deadline()
                if command_deadline is None or self._monotonic() > command_deadline:
                    self._generation_failed.set()

        future.add_done_callback(completed)

    def _expire_requests(self, now: float) -> None:
        now_ms = int(now * 1000)
        with self._lock:
            expired = [
                request_id
                for request_id, pending in self._pending.items()
                if now_ms > pending.deadline_monotonic_ms
            ]
            pending_requests = [self._pending.pop(request_id) for request_id in expired]
            if any(not pending.is_liveness_probe for pending in pending_requests):
                self._last_command_timeout_monotonic = now
        for pending in pending_requests:
            if not pending.future.done():
                with self._lock:
                    self._timeout_count += 1
                pending.future.set_exception(
                    SceneEngineRequestTimeoutError("Scene engine request timed out")
                )

    def _command_liveness_deadline(self) -> float | None:
        """Let the oldest ordered command deadline stand in for a heartbeat."""

        with self._lock:
            if (
                self._last_command_timeout_monotonic is not None
                and (
                    self._last_received_monotonic is None
                    or self._last_received_monotonic
                    <= self._last_command_timeout_monotonic
                )
            ):
                return None
            pending_commands = (
                pending
                for pending in self._pending.values()
                if (
                    pending.process_generation == self._process_generation
                    and not pending.is_liveness_probe
                )
            )
            oldest = min(
                pending_commands,
                key=lambda pending: pending.started_monotonic,
                default=None,
            )
        if oldest is None:
            return None
        return oldest.deadline_monotonic_ms / 1000.0

    def _recover_or_fail(self, initial: bool, message: str) -> bool:
        self._publish_health(SceneEngineStatus.FAILED, message)
        if not self._permit_restart():
            self._fail_start(SceneEngineProcessError("Scene engine could not start"))
            return False
        if initial and self._config.maximum_restarts == 0:
            self._fail_start(SceneEngineProcessError("Scene engine could not start"))
            return False
        return True

    def _record_request_latency(self, pending: _PendingRequest) -> None:
        latency_ms = max(0.0, (self._monotonic() - pending.started_monotonic) * 1000.0)
        with self._lock:
            self._latency_samples_ms.append(latency_ms)

    def _permit_restart(self) -> bool:
        now = self._monotonic()
        cutoff = now - float(self._config.restart_window_seconds)
        while self._restart_times and self._restart_times[0] < cutoff:
            self._restart_times.popleft()
        if len(self._restart_times) >= self._config.maximum_restarts:
            return False
        self._restart_times.append(now)
        self._restart_count += 1
        return True

    def _wait_before_restart(self, attempt: int) -> None:
        base_ms = min(
            self._config.restart_initial_delay_ms * 2 ** max(0, attempt - 1),
            self._config.restart_maximum_delay_ms,
        )
        spread = base_ms * float(self._config.restart_jitter_ratio)
        delay_ms = self._random_uniform(base_ms - spread, base_ms + spread)
        self._stop_event.wait(max(0.0, delay_ms / 1000.0))

    def _terminate_process(self, process: subprocess.Popen[bytes]) -> None:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=self._config.stop_timeout_ms / 1000.0)
            except subprocess.TimeoutExpired:
                process.kill()
                try:
                    process.wait(timeout=self._config.stop_timeout_ms / 1000.0)
                except subprocess.TimeoutExpired:
                    pass
        with self._lock:
            if self._process is process:
                self._process = None

    def _stop_process_gracefully(self, process: subprocess.Popen[bytes]) -> None:
        if process.poll() is None:
            try:
                process.wait(timeout=self._config.stop_timeout_ms / 1000.0)
            except subprocess.TimeoutExpired:
                self._terminate_process(process)
                return
        with self._lock:
            if self._process is process:
                self._process = None

    def _fail_generation(self, generation: str, error: BaseException) -> None:
        with self._lock:
            request_ids = [
                request_id
                for request_id, pending in self._pending.items()
                if pending.process_generation == generation
            ]
            pending_requests = [self._pending.pop(request_id) for request_id in request_ids]
        for pending in pending_requests:
            if not pending.future.done():
                pending.future.set_exception(error)

    def _fail_all(self, error: BaseException) -> None:
        with self._lock:
            pending_requests = tuple(self._pending.values())
            self._pending.clear()
        for pending in pending_requests:
            if not pending.future.done():
                pending.future.set_exception(error)

    def _fail_start(self, error: BaseException) -> None:
        with self._lock:
            self._ready = False
            start_future = self._start_future
            self._started = False
        if start_future is not None and not start_future.done():
            start_future.set_exception(error)

    def _publish_health(self, status: SceneEngineStatus, message: str) -> None:
        with self._lock:
            process = self._process
            health = SceneEngineHealth(
                status=status,
                session_id=self._session_id,
                process_generation=self._process_generation,
                process_id=None if process is None else process.pid,
                last_heartbeat_monotonic=self._last_received_monotonic,
                restart_count=self._restart_count,
                message=message,
            )
            self._health = health
        self._emit_event(EngineHealthEvent(health))

    def _emit_event(self, event: SceneEngineEvent) -> None:
        with self._lock:
            listeners = tuple(self._listeners)
        for listener in listeners:
            try:
                listener(event)
            except Exception:  # noqa: BLE001 - observers cannot own engine lifecycle
                continue

    def _current_process(self) -> subprocess.Popen[bytes] | None:
        with self._lock:
            return self._process


def _capabilities_from_envelope(envelope: SceneIpcEnvelope) -> SceneEngineCapabilities:
    payload = require_payload_fields(
        envelope.payload,
        _HELLO_FIELDS,
        message_type="hello acknowledgement",
    )
    return SceneEngineCapabilities(
        protocol_version=PROTOCOL_VERSION,
        process_generation=envelope.process_generation,
        local_cameras=require_bool(payload["local_cameras"], "local camera capability"),
        rtsp_cameras=require_bool(payload["rtsp_cameras"], "RTSP camera capability"),
        hardware_compositing=require_bool(
            payload["hardware_compositing"],
            "hardware composition capability",
        ),
        virtual_camera=require_bool(payload["virtual_camera"], "virtual camera capability"),
        d3d11_shared_textures=require_bool(
            payload["d3d11_shared_textures"],
            "D3D11 shared-texture capability",
        ),
        program_recording=require_bool(
            payload["program_recording"],
            "Program recording capability",
        ),
        audio_input_capture=require_bool(
            payload["audio_input_capture"],
            "audio input capture capability",
        ),
        system_audio_capture=require_bool(
            payload["system_audio_capture"],
            "system audio capture capability",
        ),
    )


def _ack_from_envelope(envelope: SceneIpcEnvelope) -> SceneEngineAck:
    payload = require_payload_fields(envelope.payload, _ACK_FIELDS, message_type="acknowledgement")
    return SceneEngineAck(
        request_id=envelope.request_id,
        session_id=envelope.session_id,
        process_generation=envelope.process_generation,
        sequence=envelope.sequence,
        document_revision=envelope.document_revision,
        applied=require_bool(payload["applied"], "acknowledgement applied state"),
        error_code=require_text(payload["error_code"], "acknowledgement error code", maximum=128),
        error_message=require_text(
            payload["error_message"],
            "acknowledgement error message",
        ),
    )


def _command_error_from_envelope(envelope: SceneIpcEnvelope) -> SceneEngineCommandRejectedError:
    payload = require_payload_fields(
        envelope.payload,
        _ERROR_FIELDS,
        message_type="command error",
    )
    error_code = require_text(payload["error_code"], "command error code", maximum=128)
    require_text(payload["error_message"], "command error message")
    if not error_code:
        raise SceneIpcMessageError("Command error requires an error code")
    return SceneEngineCommandRejectedError(error_code)


def _preparation_from_envelope(envelope: SceneIpcEnvelope) -> ScenePreparation:
    payload = require_payload_fields(
        envelope.payload,
        _PREPARATION_FIELDS,
        message_type="scene preparation",
    )
    try:
        bus_id = BusId(require_text(payload["bus_id"], "preparation bus", maximum=64))
    except ValueError as exc:
        raise SceneIpcMessageError("Invalid preparation bus") from exc
    raw_transition = payload["transition"]
    if not isinstance(raw_transition, dict):
        raise SceneIpcMessageError("Prepared transition must be an object")
    transition_record = require_payload_fields(
        raw_transition,
        _TRANSITION_FIELDS,
        message_type="prepared transition",
    )
    try:
        transition = TransitionSpec(
            kind=TransitionKind(
                require_text(
                    transition_record["kind"],
                    "prepared transition kind",
                    maximum=64,
                )
            ),
            duration_ms=require_non_negative_int(
                transition_record["duration_ms"],
                "prepared transition duration",
            ),
        )
    except (TypeError, ValueError) as exc:
        raise SceneIpcMessageError("Invalid prepared transition") from exc
    return ScenePreparation(
        request_id=envelope.request_id,
        session_id=envelope.session_id,
        process_generation=envelope.process_generation,
        sequence=envelope.sequence,
        document_revision=envelope.document_revision,
        bus_id=bus_id,
        scene_id=require_text(payload["scene_id"], "preparation scene id", maximum=256),
        preparation_token=require_text(
            payload["preparation_token"],
            "preparation token",
            maximum=256,
        ),
        transition=transition,
        fallback_applied=require_bool(
            payload["fallback_applied"],
            "transition fallback state",
        ),
        fallback_reason=require_text(
            payload["fallback_reason"],
            "transition fallback reason",
            maximum=256,
        ),
    )


def _heartbeat_from_envelope(envelope: SceneIpcEnvelope) -> int:
    payload = require_payload_fields(
        envelope.payload,
        _HEARTBEAT_FIELDS,
        message_type="heartbeat",
    )
    return require_non_negative_int(payload["monotonic_ms"], "heartbeat timestamp")


def _source_health_from_envelope(envelope: SceneIpcEnvelope) -> SourceHealthEvent:
    payload = require_payload_fields(
        envelope.payload,
        _SOURCE_HEALTH_FIELDS,
        message_type="source health",
    )
    try:
        status = SourceHealthStatus(
            require_text(payload["status"], "source health status", maximum=64)
        )
    except ValueError as exc:
        raise SceneIpcMessageError("Invalid source health status") from exc
    return SourceHealthEvent(
        source_id=require_text(payload["source_id"], "source health id", maximum=256),
        status=status,
        error_code=require_text(payload["error_code"], "source error code", maximum=128),
        message=require_text(payload["message"], "source health message"),
    )


def _local_camera_discovery_from_envelope(
    envelope: SceneIpcEnvelope,
) -> LocalCameraDiscovery:
    payload = require_payload_fields(
        envelope.payload,
        _LOCAL_CAMERA_LIST_FIELDS,
        message_type="local camera list",
    )
    raw_devices = payload["devices"]
    if not isinstance(raw_devices, list) or len(raw_devices) > 64:
        raise SceneIpcMessageError("Invalid local camera device list")
    devices: list[LocalCameraDevice] = []
    for raw_device in raw_devices:
        device = require_payload_fields(
            raw_device,
            _LOCAL_CAMERA_FIELDS,
            message_type="local camera",
        )
        raw_formats = device["formats"]
        if not isinstance(raw_formats, list) or len(raw_formats) > 256:
            raise SceneIpcMessageError("Invalid local camera format list")
        formats: list[LocalVideoFormat] = []
        for raw_format in raw_formats:
            video_format = require_payload_fields(
                raw_format,
                _LOCAL_VIDEO_FORMAT_FIELDS,
                message_type="local video format",
            )
            try:
                media_type = CameraMediaType(
                    require_text(video_format["media_type"], "local video media type", maximum=80)
                )
            except ValueError as exc:
                raise SceneIpcMessageError("Invalid local video media type") from exc
            formats.append(
                LocalVideoFormat(
                    media_type=media_type,
                    pixel_format=require_text(
                        video_format["pixel_format"],
                        "local video pixel format",
                        maximum=80,
                    ),
                    width=require_non_negative_int(video_format["width"], "local video width"),
                    height=require_non_negative_int(video_format["height"], "local video height"),
                    fps_numerator=require_non_negative_int(
                        video_format["fps_numerator"],
                        "local video FPS numerator",
                    ),
                    fps_denominator=require_non_negative_int(
                        video_format["fps_denominator"],
                        "local video FPS denominator",
                    ),
                )
            )
        devices.append(
            LocalCameraDevice(
                device_id=require_text(
                    device["device_id"],
                    "local camera device id",
                    maximum=1024,
                ),
                display_name=require_text(
                    device["display_name"],
                    "local camera display name",
                    maximum=512,
                ),
                software_device=require_bool(
                    device["software_device"],
                    "local camera software-device flag",
                ),
                formats=tuple(formats),
                probe=_local_camera_probe(device["probe"]),
            )
        )
    return LocalCameraDiscovery(
        supported=require_bool(payload["supported"], "local camera discovery support"),
        ready=require_bool(payload["ready"], "local camera discovery readiness"),
        generation=require_non_negative_int(
            payload["generation"],
            "local camera discovery generation",
        ),
        devices=tuple(devices),
        error_code=require_text(
            payload["error_code"],
            "local camera discovery error code",
            maximum=128,
        ),
    )


def _local_camera_probe(raw_probe: object) -> LocalCameraProbe:
    probe = require_payload_fields(
        raw_probe,
        _LOCAL_CAMERA_PROBE_FIELDS,
        message_type="local camera probe",
    )
    try:
        status = LocalCameraProbeStatus(
            require_text(probe["status"], "local camera probe status", maximum=64)
        )
    except ValueError as exc:
        raise SceneIpcMessageError("Invalid local camera probe status") from exc
    return LocalCameraProbe(
        status=status,
        backend=require_text(probe["backend"], "local camera probe backend", maximum=256),
        failure_stage=require_text(
            probe["failure_stage"],
            "local camera probe failure stage",
            maximum=128,
        ),
        error_code=require_text(
            probe["error_code"],
            "local camera probe error code",
            maximum=128,
        ),
        native_error_code=require_text(
            probe["native_error_code"],
            "local camera probe native error code",
            maximum=128,
        ),
    )


def _audio_device_discovery_from_envelope(
    envelope: SceneIpcEnvelope,
) -> AudioDeviceDiscovery:
    payload = require_payload_fields(
        envelope.payload,
        _AUDIO_DEVICE_LIST_FIELDS,
        message_type="audio device list",
    )
    raw_devices = payload["devices"]
    if not isinstance(raw_devices, list) or len(raw_devices) > MAXIMUM_AUDIO_DEVICES:
        raise SceneIpcMessageError("Invalid audio device list")
    devices: list[AudioDevice] = []
    for raw_device in raw_devices:
        device = require_payload_fields(
            raw_device,
            _AUDIO_DEVICE_FIELDS,
            message_type="audio device",
        )
        try:
            direction = AudioDeviceDirection(
                require_text(device["direction"], "audio device direction", maximum=64)
            )
        except ValueError as exc:
            raise SceneIpcMessageError("Invalid audio device direction") from exc
        devices.append(
            AudioDevice(
                device_id=require_text(
                    device["device_id"],
                    "audio device id",
                    maximum=MAXIMUM_AUDIO_DEVICE_ID_LENGTH,
                ),
                display_name=require_text(
                    device["display_name"],
                    "audio device display name",
                    maximum=MAXIMUM_AUDIO_DEVICE_NAME_LENGTH,
                ),
                direction=direction,
                is_default=require_bool(
                    device["is_default"],
                    "audio device default state",
                ),
            )
        )
    return AudioDeviceDiscovery(
        supported=require_bool(payload["supported"], "audio discovery support"),
        ready=require_bool(payload["ready"], "audio discovery readiness"),
        generation=require_non_negative_int(
            payload["generation"],
            "audio discovery generation",
        ),
        devices=tuple(devices),
        error_code=require_text(
            payload["error_code"],
            "audio discovery error code",
            maximum=128,
        ),
    )


def _program_recording_event_from_envelope(
    envelope: SceneIpcEnvelope,
) -> ProgramRecordingEvent:
    payload = require_payload_fields(
        envelope.payload,
        _PROGRAM_RECORDING_STATE_FIELDS,
        message_type="Program recording state",
    )
    try:
        status = ProgramRecordingStatus(
            require_text(payload["status"], "Program recording status", maximum=64)
        )
    except ValueError as exc:
        raise SceneIpcMessageError("Invalid Program recording status") from exc
    return ProgramRecordingEvent(
        ProgramRecordingNativeState(
            status=status,
            path=require_text(
                payload["path"],
                "Program recording path",
                maximum=4096,
            ),
            error_code=require_text(
                payload["error_code"],
                "Program recording error code",
                maximum=128,
            ),
            message=require_text(
                payload["message"],
                "Program recording message",
                maximum=2048,
            ),
            microphone_warning=require_text(
                payload["microphone_warning"],
                "Program recording microphone warning",
                maximum=128,
            ),
            system_audio_warning=require_text(
                payload["system_audio_warning"],
                "Program recording system-audio warning",
                maximum=128,
            ),
            dropped_frames=require_non_negative_int(
                payload["dropped_frames"],
                "Program recording dropped-frame count",
            ),
            duplicated_frames=require_non_negative_int(
                payload["duplicated_frames"],
                "Program recording duplicated-frame count",
            ),
            frame_feed_p95_ns=require_non_negative_int(
                payload["frame_feed_p95_ns"],
                "Program recording frame-feed P95",
            ),
        )
    )


def _frame_channel_record(descriptor: FrameChannelDescriptor | None) -> object:
    if descriptor is None:
        return None
    return {
        "channel_id": descriptor.channel_id,
        "generation": descriptor.generation,
        "producer_kind": descriptor.producer_kind.value,
        "transport": descriptor.transport.value,
        "handle_token": descriptor.handle_token,
        "width": descriptor.width,
        "height": descriptor.height,
        "pixel_format": descriptor.pixel_format.value,
        "color_space": descriptor.color_space.value,
        "color_range": descriptor.color_range.value,
    }


def _window_target_record(target: OutputWindowTarget) -> dict[str, object]:
    return {
        "bus_id": target.bus_id.value,
        "target_id": target.target_id,
        "screen_id": target.screen_id,
        "native_handle": target.native_handle,
        "x": target.x,
        "y": target.y,
        "width": target.width,
        "height": target.height,
        "device_pixel_ratio": target.device_pixel_ratio,
        "visible": target.visible,
    }


def _failed_future(error: BaseException) -> Future[_T]:  # pyright: ignore[reportInvalidTypeVarUse]
    future: Future[_T] = Future()
    future.set_exception(error)
    return future


def _percentile(ordered_values: list[float], percentile: float) -> float:
    if not ordered_values:
        return 0.0
    index = max(
        0,
        min(len(ordered_values) - 1, int(len(ordered_values) * percentile + 0.999) - 1),
    )
    return ordered_values[index]


def _validate_identity(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value or len(value) > 256:
        raise ValueError(f"Invalid {field_name}")


def _validate_non_negative_int(value: object, field_name: str) -> None:
    if type(value) is not int or not 0 <= value <= 2**63 - 1:
        raise ValueError(f"Invalid {field_name}")


def _validate_deadline(value: object) -> None:
    if type(value) is not int or value <= 0:
        raise ValueError("Invalid scene engine deadline")
