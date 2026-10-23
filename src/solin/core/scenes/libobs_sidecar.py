"""Supervised libobs composition, independent media transports and idle content.

The scene-engine protocol runs over stdin/stdout. Runtime boot is injectable for
tests; failed boot completes the handshake with unavailable capabilities. Scene
hydration restores output routing and the session's explicit idle state. Idle
preparation and camera discovery run separately from foreground commands and
heartbeat handling; shutdown drains both before releasing shared native owners.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, BinaryIO, Callable, Protocol, cast

if TYPE_CHECKING:
    from solin.core.scenes.libobs_projection_route import LibobsProjectionRoute
    from solin.core.scenes.libobs_scene_builder import LibobsSceneGraph, PreparedContentPresentation
    from solin.core.scenes.libobs_thumbnail_egress import LibobsThumbnailEgress
    from solin.core.scenes.libobs_window_output import LibobsWindowOutput

from solin.core.scenes.ipc_protocol import (
    SceneIpcEnvelope,
    SceneIpcError,
    read_envelope,
    reserve_protocol_stream,
    require_payload_fields,
    require_non_negative_int,
    require_text,
    write_envelope,
)
from solin.core.scenes.model import SceneValidationError
from solin.core.scenes.engine import (
    SCENE_THUMBNAILS_CHANNEL,
    LocalCameraModeQuery,
    SceneSourcePreview,
    camera_still_camera_id,
    is_scene_render_channel,
)
from solin.core.scenes.media_control import ContentSourceKind
from solin.core.scenes.idle import IdleScreenState
from solin.core.scenes.output_health import OutputHealth, OutputHealthStatus
from solin.core.scenes.camera_recovery import LocalCameraRecovery

log = logging.getLogger(__name__)

# Unavailable-runtime baseline. Boot advertises the capabilities actually supplied
# by its adapters. The keys match SubprocessSceneEngine's hello contract.
CAPABILITIES: dict[str, bool] = {
    "local_cameras": False,
    "rtsp_cameras": False,
    "hardware_compositing": False,
    "virtual_camera": False,
    "program_recording": False,
    "audio_input_capture": False,
    "system_audio_capture": False,
}

# Control messages the client sends fire-and-forget (no response is expected).
_NOTIFY_MESSAGE_TYPES = frozenset({"cancel_preparation"})

# Operational override: run the control plane WITHOUT booting libobs. Useful for
# CI/diagnostics on machines with no GPU/display (the libobs runtime is heavy and
# needs a real graphics context); the handshake and supervision still work.
_NO_RUNTIME_ENV = "SOLIN_LIBOBS_SIDECAR_NO_RUNTIME"

# How often the media poller samples + reports playback state (position/state) so
# the app's transport UI (scrubber, play/pause, ended) stays live.
_MEDIA_POLL_INTERVAL_S = 0.25
# Each open scene window renders its scene every frame tick; bound how many
# the app can ask for so a runaway client cannot exhaust the render budget.
_MAXIMUM_SCENE_WINDOW_FEEDS = 12
_OUTPUT_POLL_INTERVAL_S = 0.5


# A runtime factory yields an object exposing ``ensure_started()`` / ``shutdown()``
# (the :class:`~solin.core.media.obs_runtime.ObsRuntime` contract). It is injected
# so the boot lifecycle can be exercised without a real libobs runtime.
class RuntimeLifecycle(Protocol):
    def ensure_started(self) -> None: ...

    def shutdown(self) -> None: ...


RuntimeFactory = Callable[[], RuntimeLifecycle]


def _default_runtime_factory() -> RuntimeLifecycle:
    from solin.core.media.obs_runtime import obs_runtime

    return obs_runtime()


def _reply(
    request: SceneIpcEnvelope,
    message_type: str,
    payload: Mapping[str, object],
) -> SceneIpcEnvelope:
    """Build a response that echoes the request's correlation fields.

    The client validates that a response carries the same session, generation,
    sequence, revision and deadline as its request (see
    ``SubprocessSceneEngine._validate_response``), so each is copied through
    verbatim; only the message type and payload change.
    """
    return SceneIpcEnvelope(
        message_type=message_type,
        request_id=request.request_id,
        session_id=request.session_id,
        process_generation=request.process_generation,
        sequence=request.sequence,
        document_revision=request.document_revision,
        deadline_monotonic_ms=request.deadline_monotonic_ms,
        payload=dict(payload),
    )


def _payload_int(payload: Mapping[str, object], field: str, default: int = 0) -> int:
    """Coerce a JSON scalar using the existing missing/null/empty defaults."""
    value = payload.get(field, default) or 0
    if not isinstance(value, (int, float, str)):
        raise SceneIpcError(f"Invalid {field}")
    return int(value)


def _content_media_epoch(payload: Mapping[str, object]) -> int | None:
    epoch = payload.get("content_media_epoch")
    if epoch is not None and (type(epoch) is not int or not 0 <= epoch < 2**64):
        raise SceneIpcError("Invalid content media epoch")
    return epoch


def _payload_object(value: object) -> dict[str, object]:
    if value is None:
        return {}
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise SceneIpcError("Expected a payload object")
    return value


def _ack(
    request: SceneIpcEnvelope,
    *,
    applied: bool,
    error_code: str = "",
    error_message: str = "",
) -> SceneIpcEnvelope:
    """Build the ``ack`` response the client expects for hydrate/window commands."""
    return _reply(
        request,
        "ack",
        {"applied": applied, "error_code": error_code, "error_message": error_message},
    )


def _unavailable_discovery(error_code: str = "runtime_unavailable") -> dict[str, object]:
    """A device-discovery payload meaning "unavailable" that satisfies the client.

    ``AudioDeviceDiscovery`` / ``LocalCameraDiscovery`` both require an *unsupported*
    result to carry a non-empty ``error_code``, and a *not-ready* result to carry
    neither devices nor an error. The only valid "we can't enumerate" shape is
    therefore ``supported=False, ready=True`` with an ``error_code`` — reporting
    "unavailable" cleanly instead of a protocol error the client would reject.
    """
    return {
        "supported": False,
        "ready": True,
        "generation": 0,
        "devices": [],
        "error_code": error_code,
    }


def build_response(request: SceneIpcEnvelope) -> SceneIpcEnvelope | None:
    """Map one control request to its response, or ``None`` for a notification.

    Pure and side-effect free (no libobs), so the wire contract is unit-testable
    without a runtime. Side effects (booting libobs on ``hello``) live in
    :class:`LibobsSidecarEngine`.
    """
    message_type = request.message_type
    if message_type == "hello":
        return _reply(request, "hello_ack", dict(CAPABILITIES))
    if message_type == "ping":
        # The client validates the heartbeat payload strictly (it must carry a
        # non-negative monotonic timestamp); an empty payload fails _validate and
        # the supervisor treats it as a generation failure and restarts us.
        return _reply(request, "heartbeat", {"monotonic_ms": int(time.monotonic() * 1000)})
    if message_type in _NOTIFY_MESSAGE_TYPES:
        return None
    return _reply(
        request,
        "error",
        {
            "error_code": "not_implemented",
            "error_message": (
                f"The libobs scene engine does not yet handle '{message_type}'"
            ),
        },
    )


@dataclass(frozen=True, slots=True)
class _PreparedContentBinding:
    kind: ContentSourceKind
    epoch: int
    idle_revision: int | None = None

    @property
    def selector(self) -> tuple[ContentSourceKind, int]:
        return self.kind, self.epoch


class _CameraDiscoveryRequests:
    """One native query for concurrent callers, with individually correlated replies.

    Closing suppresses late responses and joins native work before runtime teardown.
    The worker never dispatches foreground handlers or changes their session state.
    """

    def __init__(
        self,
        discover: Callable[[], Mapping[str, object]],
        emit: Callable[[SceneIpcEnvelope], None],
        *,
        query_modes: Callable[[LocalCameraModeQuery], Mapping[str, object]] | None = None,
        cancel: Callable[[], None] | None = None,
    ) -> None:
        self._discover = discover
        self._query_modes = query_modes
        self._cancel = cancel
        self._emit = emit
        self._condition = threading.Condition()
        self._requests: list[SceneIpcEnvelope] = []
        self._authority: tuple[str, str] | None = None
        self._closed = False
        self._worker = threading.Thread(target=self._run, name="solin-camera-discovery")
        self._worker.start()

    def submit(self, request: SceneIpcEnvelope) -> None:
        with self._condition:
            if self._closed:
                return
            try:
                self._query(request)
            except (ValueError, TypeError):
                self._emit_response(self._error(request, "invalid_request"))
                return
            if time.monotonic() * 1000 >= request.deadline_monotonic_ms:
                self._emit_response(self._error(request, "deadline_exceeded"))
                return
            self._authority = request.session_id, request.process_generation
            self._requests.append(request)
            self._condition.notify()

    @staticmethod
    def _query(request: SceneIpcEnvelope) -> LocalCameraModeQuery | None:
        if request.message_type == "list_local_cameras" and not request.payload:
            return None
        if request.message_type == "query_local_camera_modes" and set(request.payload) == {"device_id", "width", "height"}:
            return LocalCameraModeQuery(**cast(dict[str, Any], request.payload))
        raise ValueError("Invalid camera metadata request")

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._requests.clear()
            self._condition.notify_all()
        if self._cancel is not None:
            self._cancel()
        self._worker.join()

    @staticmethod
    def _error(request: SceneIpcEnvelope, code: str) -> SceneIpcEnvelope:
        return _reply(request, "error", {
            "error_code": code,
            "error_message": "The camera discovery request could not be completed",
        })

    def _emit_response(self, response: SceneIpcEnvelope) -> None:
        try:
            self._emit(response)
        except Exception:  # noqa: BLE001 - a broken reply cannot strand the worker or shutdown
            log.exception("Could not emit camera discovery response")

    def _run(self) -> None:
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._closed or bool(self._requests))
                if self._closed:
                    return
                admitted = []
                for request in self._requests:
                    if time.monotonic() * 1000 >= request.deadline_monotonic_ms:
                        self._emit_response(self._error(request, "deadline_exceeded"))
                    else:
                        admitted.append(request)
                self._requests = admitted
                if not admitted:
                    continue
                query = self._query(self._requests[0])
            try:
                if query is None:
                    payload = self._discover()
                elif self._query_modes is None:
                    payload = _unavailable_discovery("capability_query_failed")
                else:
                    payload = self._query_modes(query)
            except Exception:  # noqa: BLE001 - one failed query must allow retries
                log.exception("Camera discovery worker failed")
                payload = _unavailable_discovery("camera_enumeration_failed")
            with self._condition:
                if self._closed:
                    return
                requests = [item for item in self._requests if self._query(item) == query]
                self._requests = [item for item in self._requests if self._query(item) != query]
                for request in requests:
                    code = ""
                    if (request.session_id, request.process_generation) != self._authority:
                        code = "stale_request"
                    elif time.monotonic() * 1000 >= request.deadline_monotonic_ms:
                        code = "deadline_exceeded"
                    response = (
                        self._error(request, code) if code
                        else _reply(request, "local_camera_list" if query is None else "local_camera_modes", payload)
                    )
                    self._emit_response(response)


@dataclass(frozen=True, slots=True)
class _CameraHealthBinding:
    source_id: str
    device_id: str
    configuration_id: str


class LibobsSidecarEngine:
    """Owns the libobs runtime lifecycle behind the control protocol.

    The wire mapping stays in :func:`build_response`; this adds the side effects:
    the libobs runtime is started on the ``hello`` handshake and released on
    :meth:`shutdown`. A ``runtime_factory`` of ``None`` means "do not touch
    libobs" (protocol-only mode, used by plain wire tests); the real sidecar is
    constructed with :func:`_default_runtime_factory`.
    """

    def __init__(self, runtime_factory: RuntimeFactory | None = None) -> None:
        self._runtime_factory = runtime_factory
        self._runtime: RuntimeLifecycle | None = None
        self._runtime_started = False
        self._scene_graph: LibobsSceneGraph | None = None
        self._window_output: LibobsWindowOutput | None = None
        self._projection_route: LibobsProjectionRoute | None = None
        self._thumbnail_egress: LibobsThumbnailEgress | None = None
        # One extra scene-render feed per open scene window, by channel id. Built
        # lazily from the same factory as the card atlas, so every feed follows
        # the graph through suspend/resume/refresh exactly like the cards do.
        self._scene_window_feeds: dict[str, LibobsThumbnailEgress] = {}
        self._scene_render_factory: Callable[[], LibobsThumbnailEgress] | None = None
        self._camera_still_factory: Callable[[], LibobsThumbnailEgress] | None = None
        self._content_consumer: Any | None = None
        self._preview_egress: Any | None = None
        self._program_egress: Any | None = None
        self._virtual_camera: Any | None = None
        self._output_health_request: SceneIpcEnvelope | None = None
        self._output_health: OutputHealth | None = None
        self._next_output_poll_at = 0.0
        self._recorder: Any | None = None
        self._meeting_output: Any | None = None
        self._audio_mixer: Any | None = None
        self._camera_generation = 0
        self._camera_health_bindings: tuple[_CameraHealthBinding, ...] = ()
        self._camera_health_request: SceneIpcEnvelope | None = None
        self._camera_health: dict[str, tuple[int, str, str, int, int]] = {}
        self._camera_recovery: LocalCameraRecovery | None = None
        self._media_source: Any | None = None
        self._bg_media_source: Any | None = None
        # Transport close must not replace the outgoing picture with old ingress
        # pixels. Keep sources while compositions or native outputs reference them.
        self._retired_media_sources: list[Any] = []
        self._retired_frame_sources: list[Any] = []
        self._prepared_content: dict[str, _PreparedContentBinding] = {}
        self._empty_content_source: Any | None = None
        self._content_presentation_epoch: int | None = None
        self._media_epoch: int | None = None
        self._requested_video_epoch: int | None = None
        self._reported_video_epoch: int | None = None
        self._media_lock = threading.Lock()
        # slot -> the last (position, duration) actually observed, so a dropped
        # stream can be told apart from a video that reached its end.
        self._media_last_progress: dict[int, tuple[int, int]] = {}
        self._media_poller: threading.Thread | None = None
        self._media_poll_stop = threading.Event()
        self._media_trim_start_ms = 0
        self._media_trim_end_from_ms = 0  # ms trimmed from the END (0 = play to natural end)
        self._media_trim_ended = False  # latched once the trim window's end is reached
        self._event_sink: Callable[[SceneIpcEnvelope], None] | None = None
        self._session_id = "not-started"
        self._process_generation = "not-started"
        self._document_revision: int | None = None
        self._editor_scene_id = ""
        self._editor_preview_sequence = -1
        self._idle_source: Any | None = None
        self._idle_presentations: dict[tuple[int, int], tuple[Any, Any]] = {}
        self._idle_presentations_lock = threading.RLock()
        self._idle_callback: Any | None = None
        self._idle_suspended = False
        self._render_demand: dict[str, object] = {}
        self._idle_definitions: tuple[str, ...] = ()
        self._idle_transport_health: tuple[str, str, int, tuple[str, ...], str] | None = None

    @property
    def runtime_started(self) -> bool:
        return self._runtime_started

    def set_event_sink(self, sink: Callable[[SceneIpcEnvelope], None] | None) -> None:
        """Register the (thread-safe) writer serve() uses for unsolicited events."""
        self._event_sink = sink
        self._output_health = None

    def _capabilities(self) -> dict[str, bool]:
        # Advertise honestly: features are only real once the runtime is up.
        capabilities = dict(CAPABILITIES)
        capabilities["hardware_compositing"] = self._runtime_started
        capabilities["virtual_camera"] = self._runtime_started
        capabilities["program_recording"] = self._runtime_started
        if self._runtime_started and self._audio_mixer is not None:
            microphone, system_audio = self._audio_mixer.available()
            capabilities["audio_input_capture"] = microphone
            capabilities["system_audio_capture"] = system_audio
        return capabilities

    def _boot_runtime(self) -> None:
        if self._runtime_factory is None or self._runtime_started:
            return
        try:
            runtime = self._runtime_factory()
            runtime.ensure_started()
        except Exception:  # noqa: BLE001 - a boot failure must not kill the handshake
            log.warning(
                "libobs runtime failed to start in the scene-engine sidecar",
                exc_info=True,
            )
            self._runtime = None
            return
        self._runtime = runtime
        self._runtime_started = True
        from solin.core.scenes.libobs_scene_builder import LibobsSceneGraph
        from solin.core.scenes.libobs_window_output import LibobsWindowOutput

        from solin.core.scenes.libobs_projection_route import LibobsProjectionRoute

        from solin.core.scenes.libobs_idle_source import LibobsIdleSource

        idle_source = LibobsIdleSource(runtime)
        self._idle_source = idle_source
        self._scene_graph = LibobsSceneGraph(runtime)
        self._window_output = LibobsWindowOutput(runtime)
        # The projection output owns its own transition, outside the scene graph so
        # a structural-edit rebuild cannot black the room's screen out.
        self._projection_route = LibobsProjectionRoute(runtime)
        self._window_output.set_projection_resolver(
            lambda: self._projection_route.source_ptr
            if self._projection_route is not None else None
        )
        # Let a window target render a specific scene directly (editor preview);
        # re-resolved every frame against the current graph.
        self._window_output.set_scene_resolver(
            lambda scene_id: self._scene_graph.editor_scene_source(scene_id)
            if self._scene_graph is not None else None
        )
        from solin.core.scenes.libobs_preview_egress import LibobsPreviewEgress
        from solin.core.scenes.libobs_program_egress import LibobsProgramEgress

        from solin.core.scenes.libobs_thumbnail_egress import LibobsThumbnailEgress

        self._preview_egress = LibobsPreviewEgress(
            runtime, before_render=self._scene_graph.refresh_source_crops,
        )
        graph_for_feeds = self._scene_graph

        def render_feed(resolver: Callable[[str], Any]) -> LibobsThumbnailEgress:
            return LibobsThumbnailEgress(
                runtime, resolver, before_render=graph_for_feeds.refresh_source_crops,
            )

        def scene_render_feed() -> LibobsThumbnailEgress:
            return render_feed(
                lambda scene_id: self._scene_graph.scene_source(scene_id)
                if self._scene_graph is not None else None
            )

        def camera_still_feed() -> LibobsThumbnailEgress:
            # Reads the graph's cache only: the render callback must never build.
            return render_feed(
                lambda camera_id: self._scene_graph.camera_still_source(camera_id)
                if self._scene_graph is not None else None
            )

        self._scene_render_factory = scene_render_feed
        self._camera_still_factory = camera_still_feed
        self._thumbnail_egress = scene_render_feed()
        self._program_egress = LibobsProgramEgress(
            runtime,
            on_ready=self._emit_frame_egress_ready,
        )
        from solin.core.scenes.libobs_audio_sources import LibobsAudioMixer
        from solin.core.scenes.libobs_media_source import LibobsMediaSource
        from solin.core.scenes.libobs_recorder import LibobsRecorder
        from solin.core.scenes.libobs_virtual_camera import LibobsVirtualCamera

        self._virtual_camera = LibobsVirtualCamera(runtime)
        self._recorder = LibobsRecorder(runtime)
        self._audio_mixer = LibobsAudioMixer(runtime)
        self._media_source = LibobsMediaSource(runtime)
        # Background audio slot: monitored, but not composited into the scene.
        self._bg_media_source = LibobsMediaSource(runtime)
        from solin.core.scenes.libobs_meeting_output import LibobsMeetingOutput

        self._meeting_output = LibobsMeetingOutput(
            runtime,
            media_sources=self._playing_media_sources,
            on_state=self._emit_meeting_output_state,
        )

    def handle(self, request: SceneIpcEnvelope) -> SceneIpcEnvelope | None:
        if request.message_type == "set_editor_source_preview":
            return self._handle_editor_source_preview(request)
        if request.message_type == "clear_content_presentation":
            return self._handle_clear_content_presentation(request)
        # Remember correlation ids so unsolicited events (recording state) are
        # accepted by the client (it validates session + generation on events).
        self._session_id = request.session_id
        self._process_generation = request.process_generation
        message_type = request.message_type
        if message_type == "hello":
            self._boot_runtime()
            return _reply(request, "hello_ack", self._capabilities())
        if message_type == "ping":
            self._collect_retired_presentations()
            self._poll_camera_recovery()
            self._publish_camera_health()
            self._publish_output_health(poll=True)
            return build_response(request)
        if message_type == "reconcile_local_cameras":
            return self._reconcile_local_cameras(request)
        if message_type == "start_meeting_output":
            return self._handle_start_meeting_output(request)
        if message_type == "stop_meeting_output":
            return self._handle_stop_meeting_output(request)
        if message_type == "start_program_recording":
            return self._handle_start_recording(request)
        if message_type == "stop_program_recording":
            return self._handle_stop_recording(request)
        if message_type == "set_program_recording_audio":
            return self._handle_set_recording_audio(request)
        if message_type == "open_media":
            return self._handle_open_media(request)
        if message_type == "control_media":
            return self._handle_control_media(request)
        if message_type == "set_media_properties":
            return self._handle_set_media_properties(request)
        if message_type == "list_local_cameras":
            if not self._runtime_started:
                return _reply(request, "local_camera_list", _unavailable_discovery())
            return _reply(request, "local_camera_list", self._local_camera_list())
        if message_type == "query_local_camera_modes":
            query = _CameraDiscoveryRequests._query(request)
            assert query is not None
            payload = self._local_camera_modes(query) if self._runtime_started else _unavailable_discovery()
            return _reply(request, "local_camera_modes", payload)
        if message_type == "list_audio_devices":
            return _reply(request, "audio_device_list", self._audio_device_list())
        if message_type == "hydrate":
            return self._handle_hydrate(request)
        if message_type == "prepare_scene":
            return self._handle_prepare_scene(request)
        if message_type == "take_prepared":
            return self._handle_take_prepared(request)
        if message_type == "cancel_preparation":
            graph = self._scene_graph
            if graph is not None:
                graph.cancel_all()
            self._prepared_content.clear()
            return None  # a notification — no response
        if message_type == "set_idle_screen":
            return self._handle_idle_screen(request)
        if message_type == "preview_layer_geometry":
            return self._handle_preview_layer_geometry(request)
        if message_type == "set_output_enabled":
            return self._handle_set_output_enabled(request)
        if message_type == "set_render_enabled":
            return self._handle_set_render_enabled(request)
        if message_type == "set_thumbnail_egress":
            return self._handle_set_thumbnail_egress(request)
        if message_type == "set_window_targets":
            return self._handle_set_window_targets(request)
        return build_response(request)

    def cancel_idle_preparation(self) -> None:
        """Invalidate a pending idle candidate without touching committed content."""
        if self._idle_source is not None:
            self._idle_source.cancel_preparation()

    def admit_idle_screen(self, request: SceneIpcEnvelope, state: IdleScreenState) -> None:
        """Authenticate before reserving a revision or superseding preparation."""
        self._validate_idle_request(request)
        assert self._idle_source is not None
        self._idle_source.admit(state)

    def _validate_idle_request(self, request: SceneIpcEnvelope) -> None:
        from solin.core.scenes.libobs_idle_source import IdleScreenError

        if not self._runtime_started or self._idle_source is None:
            raise IdleScreenError("runtime_unavailable")
        if (request.session_id != self._session_id
                or request.process_generation != self._process_generation):
            raise IdleScreenError("session_mismatch")
        if time.monotonic() * 1000 >= request.deadline_monotonic_ms:
            raise IdleScreenError("deadline_exceeded")

    def _handle_idle_screen(self, request: SceneIpcEnvelope) -> SceneIpcEnvelope:
        from solin.core.scenes.libobs_idle_source import IdleScreenError

        try:
            payload = require_payload_fields(
                request.payload, frozenset({"idle_screen"}), message_type="idle screen",
            )
            state = IdleScreenState.from_record(payload["idle_screen"])
            # Revalidate authority at dispatch, but do not readmit a choice
            # that the worker may already have cancelled during shutdown.
            self._validate_idle_request(request)
            assert self._idle_source is not None
            self._idle_source.apply(state, request.deadline_monotonic_ms)
            self._publish_idle_health(request, "")
            self._reconcile_idle_demand()
            return _ack(request, applied=True)
        except (ValueError, IdleScreenError) as exc:
            code = getattr(exc, "error_code", "invalid_idle_screen")
            return _ack(request, applied=False, error_code=code,
                        error_message="The idle screen could not be applied")

    def _publish_idle_health(
        self, request: SceneIpcEnvelope, error_code: str, *, message: str | None = None,
    ) -> None:
        # A control update supersedes any transport notification, including a
        # recovery that installs annual text while preserving its own error.
        self._idle_transport_health = None
        sink = self._event_sink
        if sink is None:
            return
        if message is None:
            message = "Idle media unavailable; showing year text" if error_code else ""
        for source_id in self._idle_definitions:
            sink(_reply(request, "source_health", {
                "source_id": source_id, "status": "failed" if error_code else "ready",
                "configuration_id": "",
                "error_code": error_code,
                "message": message,
            }))

    def _reconcile_idle_health(self, error_code: str) -> None:
        """Report reset failures once; their retained frame is not an annual fallback."""
        revision = self._document_revision
        if revision is None or not self._idle_definitions or self._event_sink is None:
            return
        health = (
            self._session_id, self._process_generation, revision,
            self._idle_definitions, error_code,
        )
        previous = self._idle_transport_health
        if health == previous or (not error_code and (previous is None or not previous[-1])):
            return
        request = SceneIpcEnvelope(
            message_type="source_health", request_id="event-idle-health",
            session_id=health[0], process_generation=health[1], sequence=0,
            document_revision=revision,
            deadline_monotonic_ms=int(time.monotonic() * 1000) + 2000, payload={},
        )
        self._publish_idle_health(
            request, error_code,
            message="Idle video could not restart; showing its initial frame" if error_code else "",
        )
        self._idle_transport_health = health

    def _reconcile_idle_demand(self, *_canvas: int) -> None:
        """Native references include transition origins; explicit demand gates outputs."""
        idle, graph = self._idle_source, self._scene_graph
        if idle is None or graph is None or self._idle_suspended:
            return
        output = self._window_output
        guard = output.hydrate_lock if output is not None else None
        if guard is not None and not guard.acquire(blocking=False):
            return
        if not self._idle_presentations_lock.acquire(blocking=False):
            if guard is not None:
                guard.release()
            return
        try:
            # Borrowed from the stable owner on the control thread. Acquiring the
            # owner's lock here can deadlock an atomic scene commit against OBS.
            def contains(source: Any, target: Any, *, transition: bool = False) -> bool:
                runtime = cast(Any, self._runtime)
                if transition:
                    return runtime.transition_source_tree_contains(source, target)
                return runtime.active_source_tree_contains(source, target)

            rendered_scene_ids = {
                scene_id
                for feed in self._scene_render_feeds()
                for scene_id in feed.scene_ids
            }
            camera, recorder = self._virtual_camera, self._recorder
            program, editor = self._program_egress, self._preview_egress
            for owner, idle_source in tuple(self._idle_presentations.values()):
                preview = bool(
                    rendered_scene_ids
                    and graph.content_source is idle_source
                    and any(
                        graph.scene_uses_presentation_source(scene_id)
                        for scene_id in rendered_scene_ids
                    )
                )
                live = bool(
                    (
                        (camera is not None and camera.active)
                        or (recorder is not None and recorder.active)
                    )
                    and contains(graph.program_source, idle_source, transition=True)
                )
                if not preview and program is not None and program.active:
                    preview = contains(graph.program_source, idle_source, transition=True)
                if not preview and editor is not None and editor.active:
                    preview = contains(
                        graph.editor_scene_source(self._editor_scene_id), idle_source
                    )
                if output is not None:
                    for route, scene_id in output.render_targets:
                        if route == "scene":
                            preview = preview or contains(
                                graph.editor_scene_source(scene_id), idle_source
                            )
                        elif route == "projection":
                            projection = self._projection_route
                            live = live or (
                                projection is not None
                                and contains(
                                    projection.transition_source, idle_source, transition=True
                                )
                            )
                        else:
                            live = live or contains(
                                graph.program_source, idle_source, transition=True
                            )
                        if live and preview:
                            break
                owner.set_demand(bool(preview or live), live=bool(live))
        except Exception:  # noqa: BLE001 - native render callback boundary
            log.warning("Could not reconcile idle screen visibility", exc_info=True)
        finally:
            self._idle_presentations_lock.release()
            if guard is not None:
                guard.release()
        # The owner and transport getters are lock-free snapshots of committed
        # state. Never acquire the owner's scene-commit lock on the render thread,
        # and do not retain the graph borrower guard while publishing IPC events.
        try:
            self._reconcile_idle_health(
                next(
                    (
                        owner.transport_error_code
                        for owner, _source in tuple(self._idle_presentations.values())
                        if owner.transport_error_code
                    ),
                    "",
                )
            )
        except Exception:  # noqa: BLE001 - native render callback boundary
            log.warning("Could not report idle screen transport health", exc_info=True)

    @contextmanager
    def _editor_preview_update(self, *, preserve_scene: bool = False) -> Iterator[None]:
        """Retire readback first, then exclude direct native editor borrowers."""
        egress = self._preview_egress
        previous = egress.scene_source if preserve_scene and egress is not None else None
        if egress is not None:
            egress.set_scene_source(None)
        output = self._window_output
        guard = output.hydrate_lock if output is not None else nullcontext()
        try:
            with guard:
                yield
        finally:
            if egress is not None and self._scene_graph is not None:
                egress.set_scene_source(
                    previous if preserve_scene else
                    self._scene_graph.editor_scene_source(self._editor_scene_id)
                )

    def _handle_editor_source_preview(self, request: SceneIpcEnvelope) -> SceneIpcEnvelope:
        def reply(result: SceneSourcePreview) -> SceneIpcEnvelope:
            return _reply(request, "editor_source_preview", {
                "width": result.width, "height": result.height, "error_code": result.error_code,
            })

        graph = self._scene_graph
        if not self._runtime_started or graph is None:
            return reply(SceneSourcePreview(error_code="runtime_unavailable"))
        if (
            request.session_id != self._session_id
            or request.process_generation != self._process_generation
        ):
            return reply(SceneSourcePreview(error_code="session_mismatch"))
        try:
            payload = require_payload_fields(
                request.payload, frozenset({"scene_id", "layer_id"}),
                message_type="editor source preview",
            )
            scene_id = require_text(payload["scene_id"], "scene id", maximum=256)
            layer_id = (
                require_text(payload["layer_id"], "layer id", maximum=256)
                if payload["layer_id"] is not None else None
            )
            if not scene_id or layer_id == "":
                raise SceneIpcError("Missing editor source preview identity")
        except SceneIpcError:
            return reply(SceneSourcePreview(error_code="invalid_request"))
        if request.sequence <= self._editor_preview_sequence:
            return reply(SceneSourcePreview(error_code="stale_request"))
        # Clears invalidate editor intent even after a document/scene changed.
        # They borrow no new source, so cleanup also remains safe after deadline.
        if layer_id is not None:
            if request.document_revision != self._document_revision:
                return reply(SceneSourcePreview(error_code="stale_revision"))
            if scene_id != self._editor_scene_id or scene_id not in graph.scene_ids:
                return reply(SceneSourcePreview(error_code="unknown_layer"))
        # Even an expired request consumes its ordering position: an older set
        # must not resurrect a preview after a newer clear timed out in transit.
        self._editor_preview_sequence = request.sequence
        if layer_id is not None and request.deadline_monotonic_ms <= int(time.monotonic() * 1000):
            return reply(SceneSourcePreview(error_code="deadline_exceeded"))
        try:
            with self._editor_preview_update():
                if layer_id is not None and request.deadline_monotonic_ms <= int(time.monotonic() * 1000):
                    return reply(SceneSourcePreview(error_code="deadline_exceeded"))
                result = graph.set_editor_source_preview(scene_id, layer_id)
            return reply(result)
        except Exception:  # noqa: BLE001 - native editing failure is a typed preview error
            log.warning("Could not update editor source preview", exc_info=True)
            return reply(SceneSourcePreview(error_code="source_unavailable"))

    def _playing_media_sources(self) -> list[Any]:
        sources = []
        for media in (getattr(self, "_media_source", None), getattr(self, "_bg_media_source", None)):
            source = getattr(media, "source", None) if media is not None else None
            if source is not None:
                sources.append(source)
        return sources

    def _handle_start_meeting_output(self, request: SceneIpcEnvelope) -> SceneIpcEnvelope:
        from solin.core.scenes.libobs_meeting_output import MeetingOutputRequest

        output = self._meeting_output
        if not self._runtime_started or output is None:
            return _ack(request, applied=False, error_code="runtime_unavailable",
                        error_message="the libobs runtime is not running")
        try:
            settings = MeetingOutputRequest.from_payload(request.payload)
        except (TypeError, ValueError):
            return _ack(request, applied=False, error_code="invalid_settings",
                        error_message="invalid meeting output settings")
        if output.start(settings):
            return _ack(request, applied=True)
        return _ack(request, applied=False, error_code="meeting_output_failed",
                    error_message="could not start the meeting output")

    def _handle_stop_meeting_output(self, request: SceneIpcEnvelope) -> SceneIpcEnvelope:
        output = self._meeting_output
        if output is not None:
            output.stop()
        return _ack(request, applied=True)

    def _emit_meeting_output_state(self, state: str, detail: str = "") -> None:
        sink = self._event_sink
        if sink is None:
            return
        sink(SceneIpcEnvelope(
            message_type="meeting_output_state",
            request_id="event-meeting-output",
            session_id=self._session_id,
            process_generation=self._process_generation,
            sequence=0,
            document_revision=0,
            deadline_monotonic_ms=int(time.monotonic() * 1000) + 2000,
            payload={"state": state, "detail": str(detail)[:512].replace("\n", " ")},
        ))

    def _handle_start_recording(self, request: SceneIpcEnvelope) -> SceneIpcEnvelope:
        recorder = self._recorder
        if not self._runtime_started or recorder is None:
            return _ack(request, applied=False, error_code="runtime_unavailable",
                        error_message="the libobs runtime is not running")
        path = str(request.payload.get("path") or "")
        if not path:
            return _ack(request, applied=False, error_code="invalid_path",
                        error_message="a recording path is required")
        # Bind the selected microphone + system audio into the mix BEFORE the
        # recorder starts, so the file carries them from the first frame. An
        # audio-selection failure must not block the recording itself — it is
        # surfaced as a warning on the recording-state event instead.
        warnings = self._apply_recording_audio(request.payload)
        if recorder.start(path):
            self._emit_recording_state("recording", path=path, warnings=warnings)
            return _ack(request, applied=True)
        # The recording never began, so drop the capture sources we just bound —
        # a failed start must not leave the microphone live.
        if self._audio_mixer is not None:
            self._audio_mixer.clear()
        self._emit_recording_state("failed", path=path, error_code="recording_start_failed",
                                   message="could not start the recording output")
        return _ack(request, applied=False, error_code="recording_start_failed",
                    error_message="could not start the recording output")

    def _handle_stop_recording(self, request: SceneIpcEnvelope) -> SceneIpcEnvelope:
        recorder = self._recorder
        if recorder is not None:
            recorder.stop()
        # Drop the capture sources so an unselected microphone is never left live
        # once recording ends.
        if self._audio_mixer is not None:
            self._audio_mixer.clear()
        self._emit_recording_state("idle")
        return _ack(request, applied=True)

    def _handle_set_recording_audio(self, request: SceneIpcEnvelope) -> SceneIpcEnvelope:
        mixer = self._audio_mixer
        if not self._runtime_started or mixer is None:
            return _ack(request, applied=False, error_code="runtime_unavailable",
                        error_message="the libobs runtime is not running")
        payload = request.payload
        try:
            warnings = mixer.apply(
                payload.get("microphone") or {}, payload.get("system_audio") or {}
            )
        except Exception:  # noqa: BLE001 - audio source / libobs boundary
            log.warning("libobs set_program_recording_audio failed", exc_info=True)
            return _ack(request, applied=False, error_code="audio_config_failed",
                        error_message="could not apply the audio device selection")
        # A live change during a recording: republish the state so the app sees any
        # device-unavailable warning from the updated libobs capture sources.
        recorder = self._recorder
        if recorder is not None and recorder.active:
            self._emit_recording_state("recording", path=recorder.path, warnings=warnings)
        return _ack(request, applied=True)

    def _apply_recording_audio(self, payload: dict[str, object]) -> dict[str, str]:
        mixer = self._audio_mixer
        if mixer is None:
            return {}
        try:
            return mixer.apply(
                payload.get("microphone") or {}, payload.get("system_audio") or {}
            )
        except Exception:  # noqa: BLE001 - audio must not block the recording
            log.warning("could not apply recording audio selection", exc_info=True)
            return {}

    def _audio_device_list(self) -> dict[str, object]:
        mixer = self._audio_mixer
        if mixer is None:
            return _unavailable_discovery()  # runtime not booted → audio unavailable
        try:
            return mixer.list_devices()
        except Exception:  # noqa: BLE001 - enumeration boundary
            log.warning("libobs audio device enumeration failed", exc_info=True)
            return _unavailable_discovery("audio_enumeration_failed")

    def _reconcile_local_cameras(self, request: SceneIpcEnvelope) -> SceneIpcEnvelope:
        from solin.core.scenes.camera_catalog import camera_catalog

        fields = require_payload_fields(request.payload, frozenset({"topology_revision"}),
                                        message_type=request.message_type)
        revision = require_non_negative_int(fields["topology_revision"], "camera topology revision")
        if not self._runtime_started or time.monotonic() * 1000 >= request.deadline_monotonic_ms:
            return _ack(request, applied=False)
        if self._camera_recovery is None:
            runtime = cast(Any, self._runtime)
            self._camera_recovery = LocalCameraRecovery(
                discover=lambda: (device.device_id for device in camera_catalog().devices(refresh=True)),
                state=runtime.camera_capture_state,
                reconnect=lambda device, generation: runtime.reconnect_camera(
                    device, expected_generation=generation,
                ),
                cancel_discovery=camera_catalog().cancel_discovery,
            )
            self._set_camera_recovery_targets()
        if self._camera_recovery.changed(revision):
            camera_catalog().invalidate()
        return _ack(request, applied=True)

    def _local_camera_list(self) -> dict[str, object]:
        from solin.core.scenes.camera_catalog import camera_catalog
        from solin.core.scenes.v4l2_camera_discovery import serialize_camera_device

        try:
            devices = camera_catalog().devices(refresh=True)
        except Exception:  # noqa: BLE001 - enumeration boundary
            log.warning("libobs camera enumeration failed", exc_info=True)
            return _unavailable_discovery("camera_enumeration_failed")
        self._camera_generation += 1
        return {
            "supported": True,
            "ready": True,
            "generation": self._camera_generation,
            "devices": [serialize_camera_device(device) for device in devices],
            "error_code": "",
        }

    def _local_camera_modes(self, query: LocalCameraModeQuery) -> dict[str, object]:
        from solin.core.scenes.camera_catalog import camera_catalog
        from solin.core.scenes.v4l2_camera_discovery import serialize_camera_device

        try:
            device = camera_catalog().modes(query)
        except Exception:  # noqa: BLE001 - native capability failures are contextual metadata results
            log.warning("Camera resolution capability query failed", exc_info=True)
            return _unavailable_discovery("capability_query_failed")
        return {
            "supported": True, "ready": True, "generation": self._camera_generation,
            "devices": [serialize_camera_device(device)], "error_code": "",
        }

    def _bind_camera_health(
        self, request: SceneIpcEnvelope, source_records: list[object] | tuple[object, ...],
    ) -> None:
        from solin.core.scenes.camera_catalog import camera_configuration_id
        from solin.core.scenes.model import LocalCameraConfig

        bindings = []
        for source in source_records:
            if not isinstance(source, dict) or source.get("type") != "local_camera":
                continue
            config = LocalCameraConfig.from_record(source.get("configuration", {}))
            if config.device_id:
                bindings.append(_CameraHealthBinding(
                    str(source["id"]), config.device_id, camera_configuration_id(config),
                ))
        self._camera_health_bindings = tuple(bindings)
        self._set_camera_recovery_targets()
        self._camera_health_request = request
        self._camera_health.clear()
        self._publish_camera_health()

    def _set_camera_recovery_targets(self) -> None:
        if self._camera_recovery is not None:
            self._camera_recovery.set_targets({binding.device_id: binding.configuration_id
                                              for binding in self._camera_health_bindings})

    def _poll_camera_recovery(self) -> None:
        if self._camera_recovery is not None:
            self._camera_recovery.poll()

    def _publish_camera_health(self) -> None:
        """Publish observed capture state, fenced by configuration and native generation."""
        request, bindings = self._camera_health_request, self._camera_health_bindings
        sink, runtime = self._event_sink, self._runtime
        if request is None or sink is None or runtime is None:
            return
        if (request.session_id, request.process_generation) != (
            self._session_id, self._process_generation,
        ):
            return
        for binding in bindings:
            previous = self._camera_health.get(binding.source_id)
            try:
                state = cast(Any, runtime).camera_capture_state(binding.device_id)
                health = (
                    (state.generation, str(state.status), state.error_code, state.width, state.height)
                    if state is not None else (0, "starting", "", 0, 0)
                )
            except Exception:  # noqa: BLE001 - observation must not break heartbeat handling
                if previous is None or previous[2] != "camera_observation_failed":
                    log.warning("Could not read camera capture health", exc_info=True)
                health = (
                    previous[0] if previous is not None else 0,
                    "failed", "camera_observation_failed", 0, 0,
                )
            # An observation may complete after a newer document was installed.
            if request is not self._camera_health_request or bindings is not self._camera_health_bindings:
                return
            generation, status, code, width, height = health
            if (status == "starting" and self._camera_recovery is not None
                    and self._camera_recovery.recovering(binding.device_id)):
                # Requesting a native restart does not establish capture health.
                # Preserve the warning until new frames confirm recovery.
                status, code = "failed", "camera_no_frames"
            if status not in {"starting", "ready", "failed"}:
                status, code = "failed", "camera_observation_failed"
            if status == "failed":
                code = code or "camera_capture_failed"
            else:
                code = ""
            health = generation, status, code, width, height
            if previous == health or (previous is not None and generation < previous[0]):
                continue
            messages = {
                "camera_no_frames": "The camera is not delivering video frames",
                "camera_mode_mismatch": (
                    f"The camera delivered {width}×{height} instead of the requested resolution"
                ),
                "camera_update_failed": "The camera configuration could not be applied",
                "camera_observation_failed": "The camera capture state could not be observed",
            }
            sink(_reply(request, "source_health", {
                "source_id": binding.source_id,
                "configuration_id": binding.configuration_id,
                "status": status,
                "error_code": code,
                "message": messages.get(code, "Camera capture failed" if code else ""),
            }))
            self._camera_health[binding.source_id] = health

    def _emit_recording_state(
        self,
        status: str,
        *,
        path: str = "",
        error_code: str = "",
        message: str = "",
        warnings: dict[str, str] | None = None,
    ) -> None:
        sink = self._event_sink
        if sink is None:
            return
        warnings = warnings or {}
        envelope = SceneIpcEnvelope(
            message_type="program_recording_state",
            request_id="event-recording",
            session_id=self._session_id,
            process_generation=self._process_generation,
            sequence=0,
            document_revision=0,
            deadline_monotonic_ms=int(time.monotonic() * 1000) + 2000,
            payload={
                "status": status,
                "path": path,
                "error_code": error_code,
                "message": message,
                "microphone_warning": str(warnings.get("microphone", "")),
                "system_audio_warning": str(warnings.get("system_audio", "")),
                "dropped_frames": 0,
                "duplicated_frames": 0,
                "frame_feed_p95_ns": 0,
            },
        )
        try:
            sink(envelope)
        except Exception:  # noqa: BLE001 - an event write must not break handling
            log.debug("could not emit program recording state", exc_info=True)

    def _emit_frame_egress_ready(
        self,
        channel_id: str,
        generation: int,
        handle_token: str,
    ) -> None:
        """Emit a one-shot readiness edge after a frame egress publishes."""
        sink = self._event_sink
        if sink is None:
            return
        envelope = SceneIpcEnvelope(
            message_type="frame_egress_ready",
            request_id="event-frame-egress-ready",
            session_id=self._session_id,
            process_generation=self._process_generation,
            sequence=0,
            document_revision=0,
            deadline_monotonic_ms=int(time.monotonic() * 1000) + 2000,
            payload={
                "channel_id": channel_id,
                "generation": generation,
                "handle_token": handle_token,
            },
        )
        try:
            sink(envelope)
        except Exception:  # noqa: BLE001 - an event write must not break rendering
            log.debug("could not emit frame egress readiness", exc_info=True)

    # ── media control (Fork A: libobs decodes; app drives over IPC) ─────────

    def _media_slot(self, payload: Mapping[str, object]) -> int:
        from solin.core.scenes.media_control import MEDIA_SLOT_BACKGROUND

        return MEDIA_SLOT_BACKGROUND if _payload_int(payload, "slot") else 0

    def _media_for_slot(self, slot: int) -> Any | None:
        return self._bg_media_source if slot else self._media_source

    def _handle_open_media(self, request: SceneIpcEnvelope) -> SceneIpcEnvelope:
        payload = request.payload
        slot = self._media_slot(payload)
        media = self._media_for_slot(slot)
        if not self._runtime_started or media is None:
            return _ack(request, applied=False, error_code="runtime_unavailable",
                        error_message="the libobs runtime is not running")
        path = str(payload.get("path") or "")
        if not path:
            return _ack(request, applied=False, error_code="invalid_path",
                        error_message="a media path is required")
        is_local = bool(payload.get("is_local_file", True))
        autoplay = bool(payload.get("autoplay", True))
        volume = _payload_int(payload, "volume_percent", 100)
        speed = _payload_int(payload, "speed_percent", 100) or 100
        trim_start = max(0, _payload_int(payload, "trim_start_ms"))
        trim_end = max(0, _payload_int(payload, "trim_end_ms"))
        epoch = _content_media_epoch(payload)
        with self._media_lock:
            if not slot:
                retired = media.detach_presentation()
                if retired is not None:
                    self._retired_media_sources.append(retired)
                self._media_epoch = None
                self._reported_video_epoch = None
            opened = media.open(
                path,
                autoplay=autoplay,
                is_local_file=is_local,
                volume_percent=volume,
                speed_percent=speed,
            )
            if opened and not slot:
                if self._scene_graph is not None:
                    for token, binding in tuple(self._prepared_content.items()):
                        if binding.selector != (ContentSourceKind.NATIVE_MEDIA, epoch) and (
                            epoch is not None or binding.kind is ContentSourceKind.NATIVE_MEDIA
                        ):
                            self._scene_graph.discard(token)
                            self._prepared_content.pop(token, None)
                self._media_epoch = epoch
                # Decode privately. Only an accepted Take publishes this source;
                # replacing a live Content item would erase the transition origin.
                self._media_trim_start_ms = trim_start
                self._media_trim_end_from_ms = trim_end  # from the end
                self._media_trim_ended = False
                if trim_start:
                    media.seek(trim_start)
                if epoch is not None and epoch == self._requested_video_epoch:
                    media.require_video_frame()
                self._collect_retired_presentations()
        if not opened:
            self._emit_media_state(7, 0, 0, path, slot=slot, error_code="media_open_failed")
            return _ack(request, applied=False, error_code="media_open_failed",
                        error_message="could not open the media source")
        self._ensure_media_poller()
        self._emit_media_state_now(slot)
        return _ack(request, applied=True)

    def _handle_control_media(self, request: SceneIpcEnvelope) -> SceneIpcEnvelope:
        slot = self._media_slot(request.payload)
        media = self._media_for_slot(slot)
        if not self._runtime_started or media is None:
            return _ack(request, applied=False, error_code="runtime_unavailable",
                        error_message="the libobs runtime is not running")
        action = str(request.payload.get("action") or "")
        position_ms = max(0, _payload_int(request.payload, "position_ms"))
        trim_start = self._media_trim_start_ms if not slot else 0
        reverted = False
        with self._media_lock:
            if action == "play":
                media.play()
            elif action == "pause":
                media.pause()
            elif action == "stop":
                media.stop()
            elif action == "restart":
                media.restart()
                if not slot:
                    self._media_trim_ended = False
                    if trim_start:
                        media.seek(trim_start)
            elif action == "seek":
                media.seek(position_ms + trim_start)  # trim-relative → absolute
                if not slot:
                    self._media_trim_ended = False  # re-evaluated on the next poll
            elif action == "close":
                if slot:
                    media.close()
                else:
                    retired = media.detach_presentation()
                    self._media_epoch = None
                    if retired is not None:
                        self._retired_media_sources.append(retired)
                if not slot:
                    self._media_trim_start_ms = 0
                    self._media_trim_end_from_ms = 0
                    self._media_trim_ended = False
                reverted = True
            else:
                return _ack(request, applied=False, error_code="invalid_media_action",
                            error_message=f"unknown media action {action!r}")
        if reverted:
            self._maybe_stop_media_poller()
            self._emit_media_state(5, 0, 0, "", slot=slot)  # STOPPED, so the app clears state
        else:
            self._emit_media_state_now(slot)
        return _ack(request, applied=True)

    def _handle_set_media_properties(self, request: SceneIpcEnvelope) -> SceneIpcEnvelope:
        slot = self._media_slot(request.payload)
        media = self._media_for_slot(slot)
        if not self._runtime_started or media is None:
            return _ack(request, applied=False, error_code="runtime_unavailable",
                        error_message="the libobs runtime is not running")
        volume = _payload_int(request.payload, "volume_percent", 100)
        speed = _payload_int(request.payload, "speed_percent", 100) or 100
        with self._media_lock:
            media.set_volume(volume)
            media.set_speed(speed)
        return _ack(request, applied=True)

    def _effective_content_source(self) -> Any | None:
        """Hydration preserves the committed presentation, including staged media."""
        graph = self._scene_graph
        if graph is not None and graph.content_source is not None:
            return graph.content_source
        return None

    def _release_retired_media_source(self, source: Any) -> None:
        try:
            source.media_stop()
        except Exception:  # noqa: BLE001 - cleanup must still release the native reference
            log.warning("Could not stop the retired media presentation", exc_info=True)
        try:
            source.release()
        except Exception:  # noqa: BLE001 - libobs boundary
            log.warning("Could not release the retired media presentation", exc_info=True)
        self._retired_media_sources.remove(source)

    def _collect_retired_presentations(self) -> None:
        """Collect on the control thread after all libobs showing refs disappear.

        Showing includes both transition origins and destinations, projection,
        previews and thumbnails. A Take acknowledgement only starts a transition;
        neither that acknowledgement nor its configured duration proves retirement.
        """
        consumer = self._content_consumer
        graph = self._scene_graph
        if graph is not None:
            preview = self._preview_egress
            projection = self._projection_route
            try:
                graph.collect_retired_scenes(tuple(
                    source for source in (
                        graph.active_scene_source,
                        preview.scene_source if preview is not None else None,
                        projection.scene_source if projection is not None else None,
                    ) if source is not None
                ))
            except Exception:  # noqa: BLE001 - cleanup must not break supervision
                log.warning("Could not collect retired content scenes", exc_info=True)
        for source in tuple(self._retired_media_sources):
            try:
                if source.showing:
                    continue
                if graph is not None and any(reference is source for reference in graph.content_sources):
                    continue
                self._release_retired_media_source(source)
            except Exception:  # noqa: BLE001 - cleanup must not break supervision
                log.warning("Could not collect the retired media presentation", exc_info=True)
        if consumer is not None:
            try:
                consumer.collect_unused_sources(graph.content_sources if graph is not None else ())
            except Exception:  # noqa: BLE001 - cleanup must not break supervision
                log.warning("Could not collect retired content presentations", exc_info=True)
        for wrapper in tuple(self._retired_frame_sources):
            try:
                source = wrapper.source
                if source.showing or graph is not None and any(
                    reference is source for reference in graph.content_sources
                ):
                    continue
                wrapper.release()
                self._retired_frame_sources.remove(wrapper)
            except Exception:  # noqa: BLE001 - cleanup must not break supervision
                log.warning("Could not collect a detached content source", exc_info=True)
        pending_epochs = {
            binding.epoch
            for binding in self._prepared_content.values()
            if binding.kind is ContentSourceKind.IDLE
        }
        with self._idle_presentations_lock:
            for key, (owner, source) in tuple(self._idle_presentations.items()):
                if key[0] in pending_epochs or source.showing:
                    continue
                if graph is not None and any(
                    reference is source for reference in graph.content_sources
                ):
                    continue
                try:
                    owner.close()
                    del self._idle_presentations[key]
                except Exception:  # noqa: BLE001 - preserve ownership for a later cleanup retry
                    log.warning("Could not collect the retired idle presentation", exc_info=True)

    def _sample_media(self, slot: int = 0) -> tuple[int, int, int, str] | None:
        """Read (state, position_ms, duration_ms, path) for ``slot``, or None.

        The foreground slot (0) is trim-adjusted: positions/durations are reported
        relative to the trim window and the trim end is enforced by pausing and
        reporting ENDED. The background slot (1) has no trim and is reported raw.
        """
        from solin.core.scenes.libobs_media_source import STATE_ENDED, STATE_PLAYING

        with self._media_lock:
            media = self._media_for_slot(slot)
            if media is None or media.source is None:
                return None
            # A rate change rebuilds the decoder on a later tick, so the position
            # it threw away can only be restored once that has happened.
            media.apply_pending_resume()
            position = media.position_ms
            state = media.state
            duration = media.duration_ms
            path = media.path
            if slot:
                # Background audio: raw, no trim.
                return (state, max(0, position), max(0, duration), path)
            start = self._media_trim_start_ms
            trim_from_end = self._media_trim_end_from_ms
            # Resolve the trim end against the real duration (the app can't know it
            # before decode); trim_end is "ms removed from the end". Only enforce a
            # window that is actually inside the media and wider than the start —
            # a bogus/oversized trim (end past the real duration) is ignored.
            effective_end = 0
            if trim_from_end and duration:
                candidate = duration - trim_from_end
                if candidate > start:
                    effective_end = candidate
            if effective_end and position >= effective_end and state == STATE_PLAYING:
                media.pause()
                self._media_trim_ended = True
            if self._media_trim_ended:
                state = STATE_ENDED  # latched: keep reporting ENDED past the trim end
        window_end = effective_end if effective_end else duration
        rel_position = max(0, position - start)
        rel_duration = max(0, window_end - start) if window_end else 0
        state, rel_position, rel_duration = self._survive_disconnect(
            slot, state, rel_position, rel_duration, path
        )
        return (state, rel_position, rel_duration, path)

    # A stream that drops mid-play reports ENDED, exactly like a video that
    # finished. Below this fraction of the duration, treat it as a disconnect.
    _ENDED_IS_REALLY_THE_END = 0.98

    def _survive_disconnect(
        self,
        slot: int,
        state: int,
        position: int,
        duration: int,
        path: str,
    ) -> tuple[int, int, int]:
        """Report a mid-stream disconnect as buffering, not as end-of-media.

        media-playback ends its decode thread on any read error and ffmpeg_source
        turns that into ENDED — indistinguishable from a video that actually
        finished. Taken at face value the app advances the playlist, so a moment of
        bad wifi skipped to the next item mid-meeting. ffmpeg_source is already
        rebuilding the media underneath, so the honest report is "buffering", and
        the last known position is held so the transport does not snap to zero.
        """
        from solin.core.scenes.libobs_media_source import (
            STATE_BUFFERING,
            STATE_ENDED,
            _is_remote,
        )

        if state == STATE_ENDED and _is_remote(path) and not self._media_trim_ended:
            last_position, last_duration = self._media_last_progress.get(slot, (0, 0))
            reference = duration or last_duration
            reached = position or last_position
            if reference and reached < reference * self._ENDED_IS_REALLY_THE_END:
                return (STATE_BUFFERING, reached, reference)
        if position or duration:
            self._media_last_progress[slot] = (position, duration)
        return (state, position, duration)

    def _emit_media_state_now(self, slot: int = 0) -> None:
        snapshot = self._sample_media(slot)
        if snapshot is not None:
            self._emit_media_state(*snapshot, slot=slot)

    def _emit_media_state(
        self,
        state: int,
        position_ms: int,
        duration_ms: int,
        path: str,
        *,
        slot: int = 0,
        error_code: str = "",
    ) -> None:
        sink = self._event_sink
        if sink is None:
            return
        envelope = SceneIpcEnvelope(
            message_type="media_playback_state",
            request_id="event-media",
            session_id=self._session_id,
            process_generation=self._process_generation,
            sequence=0,
            document_revision=0,
            deadline_monotonic_ms=int(time.monotonic() * 1000) + 2000,
            payload={
                "state": max(0, int(state)),
                "position_ms": max(0, int(position_ms)),
                "duration_ms": max(0, int(duration_ms)),
                "path": str(path or ""),
                "error_code": str(error_code or ""),
                "slot": int(slot),
            },
        )
        try:
            sink(envelope)
        except Exception:  # noqa: BLE001 - an event write must not break handling
            log.debug("could not emit media playback state", exc_info=True)

    def _maybe_stop_media_poller(self) -> None:
        """Stop the poller only when neither media slot has an open source."""
        fg = self._media_source
        bg = self._bg_media_source
        fg_open = fg is not None and fg.source is not None
        bg_open = bg is not None and bg.source is not None
        if not fg_open and not bg_open:
            self._stop_media_poller()

    def _ensure_media_poller(self) -> None:
        if self._media_poller is not None and self._media_poller.is_alive():
            return
        self._media_poll_stop.clear()
        self._media_poller = threading.Thread(
            target=self._media_poll_loop, name="solin-media-poll", daemon=True
        )
        self._media_poller.start()

    def _stop_media_poller(self) -> None:
        self._media_poll_stop.set()
        poller, self._media_poller = self._media_poller, None
        if poller is not None and poller is not threading.current_thread():
            poller.join(timeout=1.0)

    def _media_poll_loop(self) -> None:
        from solin.core.scenes.media_control import MEDIA_SLOT_BACKGROUND

        while not self._media_poll_stop.wait(_MEDIA_POLL_INTERVAL_S):
            for slot in (0, MEDIA_SLOT_BACKGROUND):
                snapshot = self._sample_media(slot)
                if snapshot is not None:
                    self._emit_media_state(*snapshot, slot=slot)
            self._emit_video_ready()

    def _emit_video_ready(self) -> None:
        # Loading is asynchronous. Publish readiness from the control poller,
        # keeping IPC writes and decoder waits out of the native render callback.
        with self._media_lock:
            epoch = self._media_epoch
            media = self._media_source
            if (
                epoch is None or epoch != self._requested_video_epoch
                or epoch == self._reported_video_epoch or media is None
                or not media.video_frame_ready or self._event_sink is None
            ):
                return
            sink = self._event_sink
        try:
            sink(SceneIpcEnvelope(
                message_type="media_video_ready", request_id="event-media-video-ready",
                session_id=self._session_id, process_generation=self._process_generation,
                sequence=0, document_revision=0,
                deadline_monotonic_ms=int(time.monotonic() * 1000) + 2000,
                payload={"content_media_epoch": epoch},
            ))
        except Exception:  # noqa: BLE001 - an event write must not kill the media poller
            log.debug("could not emit media video readiness", exc_info=True)
            return
        with self._media_lock:
            if self._media_epoch == epoch:
                self._reported_video_epoch = epoch

    def _handle_preview_layer_geometry(self, request: SceneIpcEnvelope) -> SceneIpcEnvelope:
        graph = self._scene_graph
        if not self._runtime_started or graph is None:
            return _ack(request, applied=False, error_code="runtime_unavailable",
                        error_message="the libobs runtime is not running")
        payload = request.payload
        scene_id = str(payload.get("scene_id") or "")
        layer = payload.get("layer") or {}
        layer_id = str(layer.get("id") or "") if isinstance(layer, dict) else ""
        if not isinstance(layer, dict):
            return _ack(request, applied=False, error_code="invalid_layer_geometry",
                        error_message="layer must be an object")
        # Apply the transform to the live scene item — no re-hydrate, so a resize
        # or move takes effect without rebuilding sources (the camera stays up).
        try:
            applied = graph.apply_layer_geometry(scene_id, layer_id, layer)
        except SceneValidationError as exc:
            return _ack(request, applied=False, error_code="invalid_layer_geometry",
                        error_message=str(exc))
        if not applied:
            return _ack(request, applied=False, error_code="unknown_layer",
                        error_message="no such built layer")
        return _ack(request, applied=True)

    def _handle_set_output_enabled(self, request: SceneIpcEnvelope) -> SceneIpcEnvelope:
        payload = request.payload
        bus_id = payload.get("bus_id")
        enabled = bool(payload.get("enabled"))
        if bus_id != "virtual_camera":
            # media_windows output is driven by set_window_targets; just ack.
            return _ack(request, applied=True)
        camera = self._virtual_camera
        if not self._runtime_started or camera is None:
            return _ack(request, applied=False, error_code="runtime_unavailable",
                        error_message="the libobs runtime is not running")
        self._output_health_request = request
        self._output_health = None
        if enabled:
            camera.start()
        else:
            camera.stop()
        # ACK confirms the desired state accepted by the supervisor. Readiness
        # and exhausted recovery are delivered independently as output health.
        self._publish_output_health()
        return _ack(request, applied=True)

    def poll_outputs(self) -> None:
        """Reconcile outputs on the control thread during sustained command traffic.

        The client suppresses heartbeats while commands are pending. Sampling
        after command dispatch keeps output recovery live in that case too.
        """
        self._poll_camera_recovery()
        if time.monotonic() >= self._next_output_poll_at:
            self._publish_output_health(poll=True)

    def _publish_output_health(
        self, *, poll: bool = False, state: OutputHealth | None = None,
    ) -> None:
        camera, request = self._virtual_camera, self._output_health_request
        if camera is None or request is None:
            return
        if poll:
            self._next_output_poll_at = time.monotonic() + _OUTPUT_POLL_INTERVAL_S
        if state is None:
            try:
                state = camera.poll() if poll else camera.health
            except Exception:  # noqa: BLE001 - output observation must not break supervision
                log.warning("Could not reconcile virtual camera health", exc_info=True)
                state = OutputHealth(OutputHealthStatus.FAILED, "output_observation_failed")
        if not isinstance(state, OutputHealth):
            state = OutputHealth(OutputHealthStatus.FAILED, "output_observation_failed")
        if state == self._output_health:
            return
        sink = self._event_sink
        if sink is None:
            return
        try:
            sink(_reply(request, "output_health", {
                "bus_id": "virtual_camera", **state.to_record(),
            }))
        except Exception:  # noqa: BLE001 - retry delivery on the next heartbeat
            log.warning("Could not publish virtual camera health", exc_info=True)
            return
        self._output_health = state
        log.info("Virtual camera output state: status=%s code=%s generation=%d",
                 state.status, state.error_code, state.generation)

    def _handle_hydrate(self, request: SceneIpcEnvelope) -> SceneIpcEnvelope:
        graph = self._scene_graph
        if not self._runtime_started or graph is None:
            return _ack(request, applied=False, error_code="runtime_unavailable",
                        error_message="the libobs runtime is not running")
        if request.sequence < self._editor_preview_sequence:
            return _ack(request, applied=False, error_code="stale_request",
                        error_message="the graph update was superseded by newer editor state")
        self._idle_suspended = True
        source_records: list[object] | tuple[object, ...] = ()
        try:
            self._document_revision = None
            self._camera_health_bindings = ()
            self._camera_health_request = None
            self._camera_health.clear()
            self._editor_preview_sequence = max(self._editor_preview_sequence, request.sequence)
            payload = request.payload
            self._prepared_content.clear()
            # Stop the preview render on the old (about-to-be-released) scenes
            # before rebuilding — the egress renders a borrowed scene source.
            if self._preview_egress is not None:
                self._preview_egress.set_scene_source(None)
            self._reconcile_content_ingress(payload.get("content_ingress"))
            # Thumbnail show refs point at borrowed scene sources that this rebuild
            # is about to destroy; drop them first and retake them afterwards.
            render_feeds = self._scene_render_feeds()
            for feed in render_feeds:
                feed.suspend()
            active_scenes = _payload_object(payload.get("active_scenes") or {})
            document = _payload_object(payload.get("document") or {})
            raw_sources = document.get("sources", ())
            if not isinstance(raw_sources, (list, tuple)):
                raise SceneIpcError("Invalid sources")
            source_records = raw_sources
            self._idle_definitions = tuple(
                str(source["id"])
                for source in source_records
                if isinstance(source, dict)
                and source.get("type") == "solin_content"
                and (source.get("configuration") or {}).get("mode", "presentation")
                == "presentation"
            )
            self._render_demand = _payload_object(payload.get("render_enabled") or {})
            if self._idle_source is not None and (
                self._idle_definitions
                or payload.get("idle_screen", IdleScreenState().to_record())
                != IdleScreenState().to_record()
            ):
                from solin.core.scenes.libobs_idle_source import IdleScreenError

                state = IdleScreenState.from_record(
                    payload.get("idle_screen", IdleScreenState().to_record()),
                )
                try:
                    self._idle_source.apply(state, request.deadline_monotonic_ms, recover=True)
                    self._publish_idle_health(request, "")
                except IdleScreenError as exc:
                    self._publish_idle_health(request, exc.error_code)
                if self._idle_definitions and self._idle_callback is None:
                    self._idle_callback = cast(Any, self._runtime).ob.add_main_render_callback(
                        self._reconcile_idle_demand,
                    )
            # An open media source owns the content slot; otherwise the BGRA
            # frame-ingress source does (both may be absent → placeholder).
            # Hold the window-output lock so a per-scene draw callback on the
            # graphics thread can't resolve a scene while it is being released.
            output = self._window_output
            rebuild_guard = output.hydrate_lock if output is not None else nullcontext()

            def configure_outputs() -> None:
                # Neither graph generation is routed to the main Program here,
                # and the preview has no borrowed source. Projection can retain
                # its old scene refs; the rebuild guard protects per-scene draws.
                # OBS registration takes the video-mix mutex, so configure before
                # activating the new main Program.
                if self._projection_route is not None:
                    self._projection_route.prepare_all()
                if self._program_egress is not None:
                    self._program_egress.configure(payload.get("program_egress"))
                if self._preview_egress is not None:
                    self._preview_egress.configure(payload.get("preview_egress"))

            with rebuild_guard:
                content_source = self._effective_content_source()
                media_only_source = graph.media_only_source
                if content_source is None and self._idle_definitions:
                    content_source = self._resolve_content_source(
                        (ContentSourceKind.IDLE, self._content_presentation_epoch or 0),
                        deadline=request.deadline_monotonic_ms / 1000,
                    )
                    media_only_source = self._ensure_empty_content_source()
                elif content_source is None and any(
                    isinstance(source, dict) and source.get("type") == "solin_content"
                    for source in source_records
                ):
                    content_source = media_only_source = self._ensure_empty_content_source()
                graph.hydrate(
                    document,
                    active_scenes,
                    content_source,
                    # Sent beside the document, never inside it: the document
                    # record is persisted and cached, and a password must be in
                    # neither. Absent on older payloads, hence the default.
                    _payload_object(payload.get("source_credentials") or {}),
                    before_activate=configure_outputs,
                    media_only_source=media_only_source,
                )
                self._document_revision = request.document_revision
            self._bind_camera_health(request, source_records)
            self._prepare_camera_stills()
            for feed in render_feeds:
                feed.resume()
            self._reconcile_projection(active_scenes)
            self._reconcile_preview_source(
                active_scenes,
                _payload_object(payload.get("render_enabled") or {}),
            )
            # Hydrate is the full-state sync. After a restart the sidecar is fresh
            # and holds no window displays, while the app's window-target cache
            # still equals the recomputed targets and so never re-dispatches. Re-
            # bind the projection displays here so one hydrate restores them too.
            # set_targets reconciles idempotently (a no-op when unchanged), so this
            # adds no churn on the structural-edit re-hydrate path.
            output = self._window_output
            if output is not None:
                targets = payload.get("window_targets") or []
                try:
                    output.set_targets(targets if isinstance(targets, list) else [])
                except Exception:  # noqa: BLE001 - a bad target must not fail the hydrate
                    log.warning("libobs hydrate window-target rebind failed", exc_info=True)
            # Likewise restore the virtual-camera output from the snapshot's
            # desired output state, so the meeting's program output comes back on
            # its own after a restart. start()/stop() are idempotent, so this is a
            # no-op on the structural-edit re-hydrate path when it's already right.
            camera = self._virtual_camera
            output_enabled = _payload_object(payload.get("output_enabled") or {})
            if camera is not None and "virtual_camera" in output_enabled:
                # The graph's ACK describes its own application. Output readiness
                # and restoration failures have an independent, observed state.
                self._output_health_request = request
                self._output_health = None
                try:
                    if output_enabled["virtual_camera"]:
                        camera.start()
                    else:
                        camera.stop()
                except Exception:  # noqa: BLE001 - output failure is independent of the graph
                    log.warning("libobs hydrate virtual-camera restore failed", exc_info=True)
                    self._publish_output_health(state=OutputHealth(
                        OutputHealthStatus.FAILED, "output_restore_failed",
                    ))
                else:
                    self._publish_output_health()
        except Exception:  # noqa: BLE001 - a bad document must not crash the engine
            log.warning("libobs hydrate failed", exc_info=True)
            # A rejected camera update still belongs to the requested mode.
            # Publish that failure against its new fingerprint, not the graph
            # that preceded this transaction.
            try:
                self._bind_camera_health(request, source_records)
            except (ValueError, KeyError):
                log.debug("Rejected document has no valid camera health bindings", exc_info=True)
            # Never leave the thumbnails suspended: a failed rebuild would freeze
            # every card until the next successful hydrate.
            try:
                self._prepare_camera_stills()
            except Exception:  # noqa: BLE001 - best-effort recovery
                log.debug("camera stills after failed hydrate errored", exc_info=True)
            for feed in self._scene_render_feeds():
                try:
                    feed.resume()
                except Exception:  # noqa: BLE001 - best-effort recovery
                    log.debug("thumbnail resume after failed hydrate errored", exc_info=True)
            return _ack(
                request,
                applied=False,
                error_code="hydrate_failed",
                error_message="could not build the libobs scene graph",
            )
        finally:
            self._idle_suspended = False
            self._reconcile_idle_demand()
        return _ack(request, applied=True)

    def _reconcile_projection(self, active_scenes: Mapping[str, object]) -> None:
        """Point the projection output at its bus's scene (a cut, on hydrate)."""
        route = self._projection_route
        graph = self._scene_graph
        if route is None or graph is None:
            return
        scene_id = str(active_scenes.get("media_windows") or "")
        source = graph.scene_source(scene_id) if scene_id else None
        route.set_scene(scene_id, source)

    def _reconcile_preview_source(
        self,
        active_scenes: Mapping[str, object],
        render_enabled: Mapping[str, object] | None = None,
    ) -> None:
        egress = self._preview_egress
        graph = self._scene_graph
        if graph is None:
            return
        self._editor_scene_id = str(active_scenes.get("editor") or "")
        if egress is None:
            return
        source = graph.editor_scene_source(self._editor_scene_id)
        egress.set_scene_source(source)
        # Hydrate is the full-state sync: after a restart the sidecar is fresh and
        # the egress defaults to disabled, while the app's edge-triggered render
        # cache still reads "enabled" and so never re-sends set_render_enabled.
        # Apply the snapshot's render demand here so one hydrate fully restores the
        # editor preview (source *and* enable) — the canvas updates after a restart.
        egress.set_enabled(bool(render_enabled and render_enabled.get("editor", False)))

    def _handle_set_render_enabled(self, request: SceneIpcEnvelope) -> SceneIpcEnvelope:
        # Only the editor bus gates its preview egress. Delivery outputs keep
        # independent demand and native references.
        payload = request.payload
        self._render_demand[str(payload.get("bus_id") or "")] = bool(payload.get("enabled"))
        if payload.get("bus_id") == "editor" and self._preview_egress is not None:
            self._preview_egress.set_enabled(bool(payload.get("enabled")))
        self._reconcile_idle_demand()
        return _ack(request, applied=True)

    def _reconcile_content_ingress(self, descriptor: object) -> None:
        from solin.core.scenes.content_frame_consumer import (
            SHARED_MEMORY_BGRA,
            ContentFrameConsumer,
        )

        if not isinstance(descriptor, dict) or descriptor.get("transport") != SHARED_MEMORY_BGRA:
            self._stop_content_consumer(retain_sources=True)  # no supported content channel
            return
        token = descriptor.get("handle_token")
        current = self._content_consumer
        if current is not None and current.handle_token == token:
            return  # already consuming this exact channel
        self._stop_content_consumer(retain_sources=True)
        consumer = ContentFrameConsumer(self._runtime, descriptor)
        if consumer.start():
            self._content_consumer = consumer

    def _stop_content_consumer(self, *, retain_sources: bool = False) -> None:
        consumer, self._content_consumer = self._content_consumer, None
        if consumer is not None:
            try:
                if retain_sources:
                    self._retired_frame_sources.extend(consumer.detach_sources())
                else:
                    self._retired_frame_sources.extend(consumer.stop())
            except Exception:  # noqa: BLE001 - shutdown must not raise
                log.warning("content frame consumer stop errored", exc_info=True)

    def _current_content_source(self, binding: tuple[ContentSourceKind, int]) -> Any | None:
        kind, epoch = binding
        if kind is ContentSourceKind.IDLE:
            if not self._idle_definitions:
                return self._empty_content_source
            idle = self._idle_source
            presentation = (
                self._idle_presentations.get((epoch, idle.state.revision))
                if idle is not None
                else None
            )
            return presentation[1] if presentation is not None else None
        if kind is ContentSourceKind.NATIVE_MEDIA:
            media = self._media_source
            return media.source if media is not None and self._media_epoch == epoch else None
        consumer = self._content_consumer
        return consumer.source_for_epoch(epoch) if consumer is not None else None

    def _resolve_content_source(
        self,
        binding: tuple[ContentSourceKind, int],
        *,
        deadline: float,
    ) -> Any | None:
        kind, epoch = binding
        if kind is ContentSourceKind.IDLE:
            if not self._idle_definitions:
                return self._ensure_empty_content_source()
            idle = self._idle_source
            if idle is None:
                return None
            source = self._current_content_source(binding)
            if source is None:
                from solin.core.scenes.libobs_idle_source import IdleScreenError

                state = idle.state
                # A metadata-only fallback update does not enter idle again.
                # Keep its clock and immutable pixels; the token still binds the
                # newly accepted configuration revision.
                with self._idle_presentations_lock:
                    for key, (owner, cached_source) in tuple(self._idle_presentations.items()):
                        if (key[0] == epoch and not idle.error_code and not owner.error_code
                                and owner.presentation_key == idle.presentation_key):
                            del self._idle_presentations[key]
                            self._idle_presentations[(epoch, state.revision)] = owner, cached_source
                            return cached_source
                try:
                    owner = idle.prepare_presentation(int(deadline * 1000))
                except IdleScreenError:
                    return None
                if idle.state != state:
                    owner.close()
                    return None
                source = owner.source
                with self._idle_presentations_lock:
                    self._idle_presentations[(epoch, state.revision)] = owner, source
            return source
        if kind is ContentSourceKind.NATIVE_MEDIA:
            media = self._media_source
            if media is None or self._media_epoch != epoch or not media.wait_for_video_frame(deadline=deadline):
                return None
        else:
            consumer = self._content_consumer
            if consumer is None or not consumer.wait_for_epoch(epoch, deadline=deadline):
                return None
        return self._current_content_source(binding)

    def _commit_content_presentation(
        self, presentation: PreparedContentPresentation, *, preserve_editor: bool,
    ) -> None:
        """Publish the canonical graph and rebind its offscreen borrowers."""
        graph = self._scene_graph
        assert graph is not None
        if presentation.changes_graph:
            with self._editor_preview_update(
                preserve_scene=preserve_editor and not graph.has_editor_source_preview,
            ):
                presentation.commit()
            for feed in self._scene_render_feeds():
                feed.refresh_scene_sources()
        self._collect_retired_presentations()

    def _handle_clear_content_presentation(self, request: SceneIpcEnvelope) -> SceneIpcEnvelope:
        graph = self._scene_graph
        if not self._runtime_started or graph is None:
            return _ack(request, applied=False, error_code="runtime_unavailable",
                        error_message="the libobs runtime is not running")
        if request.session_id != self._session_id or request.process_generation != self._process_generation:
            return _ack(request, applied=False, error_code="session_mismatch",
                        error_message="the content presentation belongs to another process")
        payload = require_payload_fields(
            request.payload, frozenset({"content_media_epoch"}),
            message_type="clear content presentation",
        )
        epoch = _content_media_epoch(payload)
        if epoch is None:
            raise SceneIpcError("A content media epoch is required")
        if (
            request.document_revision != self._document_revision
            or self._content_presentation_epoch is not None and epoch < self._content_presentation_epoch
        ):
            return _ack(request, applied=False, error_code="stale_request",
                        error_message="the content presentation was superseded")
        source = self._resolve_content_source(
            (ContentSourceKind.IDLE, epoch),
            deadline=request.deadline_monotonic_ms / 1000,
        )
        empty = self._ensure_empty_content_source()
        if source is None or empty is None:
            return _ack(
                request,
                applied=False,
                error_code="source_unavailable",
                error_message="the idle presentation could not be prepared",
            )
        with graph.content_presentation(source, media_only_source=empty) as presentation:
            if self._current_content_source((ContentSourceKind.IDLE, epoch)) is not source:
                return _ack(
                    request,
                    applied=False,
                    error_code="stale_request",
                    error_message="the idle presentation was superseded",
                )
            # Canonical state is prepared without modifying an outgoing scene.
            self._commit_content_presentation(presentation, preserve_editor=False)
        self._content_presentation_epoch = epoch
        return _ack(request, applied=True)

    def _ensure_empty_content_source(self) -> Any | None:
        if self._empty_content_source is None:
            runtime = cast(Any, self._runtime)
            try:
                self._empty_content_source = runtime.ob.Source.create(
                    "color_source_v3",
                    "solin-empty-content",
                    {"color": 0, "width": runtime.video.width, "height": runtime.video.height},
                )
            except Exception:  # noqa: BLE001 - preserve committed content on native allocation failure
                log.warning("Could not create the empty content source", exc_info=True)
        return self._empty_content_source

    def _handle_prepare_scene(self, request: SceneIpcEnvelope) -> SceneIpcEnvelope:
        graph = self._scene_graph
        if not self._runtime_started or graph is None:
            return _reply(request, "error", {
                "error_code": "runtime_unavailable",
                "error_message": "the libobs runtime is not running",
            })
        payload = request.payload
        bus_id = payload.get("bus_id")
        scene_id = str(payload.get("scene_id") or "")
        transition = _payload_object(payload.get("transition") or {})
        kind = str(transition.get("kind", "cut"))
        duration_ms = _payload_int(transition, "duration_ms")
        expected_epoch = _content_media_epoch(payload)
        idle_revision = self._idle_source.state.revision if self._idle_source is not None else None
        try:
            source_kind = ContentSourceKind(str(payload.get("content_source_kind", "frames")))
        except ValueError as error:
            raise SceneIpcError("Invalid content source kind") from error
        if expected_epoch is not None and source_kind in (
            ContentSourceKind.FRAMES,
            ContentSourceKind.IDLE,
        ):
            if (
                self._resolve_content_source(
                    (source_kind, expected_epoch),
                    deadline=request.deadline_monotonic_ms / 1000,
                )
                is None
            ):
                return _reply(
                    request,
                    "error",
                    {
                        "error_code": "source_unavailable",
                        "error_message": "The requested content presentation is not ready",
                    },
                )
        result = None
        try:
            result = graph.prepare(scene_id, kind, duration_ms, str(bus_id or ""))
            if result is not None and bus_id == "media_windows":
                route = self._projection_route
                if route is None:
                    raise RuntimeError("The projection route is not running")
                route.require_prepared_transition(result["kind"])
        except Exception:  # noqa: BLE001 - a resource failure must not alter live output
            if result is not None:
                graph.discard(result["token"])
            log.warning("Could not prepare the scene transition", exc_info=True)
            return _reply(request, "error", {
                "error_code": "transition_unavailable",
                "error_message": "The requested scene transition could not be prepared",
            })
        if result is None:
            return _reply(request, "error", {
                "error_code": "unknown_scene",
                "error_message": f"no scene {scene_id!r}",
            })
        if expected_epoch is not None:
            self._prepared_content[result["token"]] = _PreparedContentBinding(
                source_kind, expected_epoch,
                idle_revision if source_kind is ContentSourceKind.IDLE else None,
            )
            if source_kind is ContentSourceKind.NATIVE_MEDIA:
                with self._media_lock:
                    self._requested_video_epoch = expected_epoch
                    if self._media_epoch == expected_epoch and self._media_source is not None:
                        self._media_source.require_video_frame()
        return _reply(request, "scene_prepared", {
            "bus_id": bus_id,
            "scene_id": scene_id,
            "preparation_token": result["token"],
            "transition": {"kind": result["kind"], "duration_ms": duration_ms},
            "fallback_applied": result["fallback_applied"],
            "fallback_reason": result["fallback_reason"],
        })

    def _handle_take_prepared(self, request: SceneIpcEnvelope) -> SceneIpcEnvelope:
        graph = self._scene_graph
        if not self._runtime_started or graph is None:
            return _ack(request, applied=False, error_code="runtime_unavailable",
                        error_message="the libobs runtime is not running")
        payload = request.payload
        token = str(payload.get("preparation_token") or "")
        bus_id = str(payload.get("bus_id") or "")
        # The token remembers the output it was prepared for. Honour that over the
        # payload so a duplicated or reordered take can never move a different
        # output than the one it was staged against.
        route = graph.pending_route(token)
        if route is None:
            return _ack(request, applied=False, error_code="unknown_preparation",
                        error_message="no such prepared scene")
        if route != bus_id:
            return _ack(request, applied=False, error_code="bus_mismatch",
                        error_message="the prepared scene belongs to another output")
        if bus_id == "editor" and request.sequence < self._editor_preview_sequence:
            graph.discard(token)
            self._prepared_content.pop(token, None)
            return _ack(request, applied=False, error_code="stale_request",
                        error_message="the editor selection was superseded")
        binding = self._prepared_content.get(token)
        prepared_idle_revision = binding.idle_revision if binding is not None else None
        if prepared_idle_revision is not None and (
            self._idle_source is None or self._idle_source.state.revision != prepared_idle_revision
        ):
            graph.discard(token)
            self._prepared_content.pop(token, None)
            return _ack(
                request,
                applied=False,
                error_code="source_unavailable",
                error_message="The prepared idle configuration was superseded",
            )
        if binding is not None and (
            self._content_presentation_epoch is not None
            and binding.epoch < self._content_presentation_epoch
        ):
            graph.discard(token)
            self._prepared_content.pop(token, None)
            return _ack(
                request,
                applied=False,
                error_code="source_unavailable",
                error_message="The prepared content presentation was superseded",
            )
        content_source = None
        if binding is not None:
            content_source = self._resolve_content_source(
                binding.selector,
                deadline=request.deadline_monotonic_ms / 1000,
            )
            if content_source is None:
                graph.discard(token)
                self._prepared_content.pop(token, None)
                return _ack(
                    request,
                    applied=False,
                    error_code="source_unavailable",
                    error_message="The prepared content presentation is no longer ready",
                )
        replacement = (
            graph.content_presentation(
                content_source,
                media_only_source=(
                    self._ensure_empty_content_source()
                    if binding is not None and binding.kind is ContentSourceKind.IDLE
                    else content_source
                ),
            )
            if binding is not None
            else nullcontext(None)
        )
        with replacement as presentation:
            if binding is not None and self._current_content_source(binding.selector) is not content_source:
                graph.discard(token)
                self._prepared_content.pop(token, None)
                return _ack(
                    request,
                    applied=False,
                    error_code="source_unavailable",
                    error_message="The prepared content presentation was superseded while staging",
                )
            scene_id = graph.pending_scene(token) or ""
            scene_source = presentation.scene_source(scene_id) if presentation is not None else None
            applied = False
            if bus_id == "editor":
                # The editor channel drives no output: taking one of its scenes only
                # re-points the off-screen preview egress, so the token is dropped
                # rather than executed.
                if not graph.discard(token):
                    return _ack(
                        request,
                        applied=False,
                        error_code="unknown_preparation",
                        error_message="no such prepared scene",
                    )
                with self._editor_preview_update():
                    graph.clear_editor_source_preview()
                    self._editor_scene_id = scene_id
                self._editor_preview_sequence = max(self._editor_preview_sequence, request.sequence)
                applied = True
            elif bus_id == "media_windows":
                # Projection animates on its OWN transition, independent of the program.
                applied = graph.take_projection(
                    token, self._projection_route, scene_source=scene_source
                )
            else:
                applied = graph.take(token, scene_source=scene_source)
            self._prepared_content.pop(token, None)
            if applied:
                if presentation is not None:
                    self._commit_content_presentation(
                        presentation, preserve_editor=bus_id != "editor"
                    )
                    assert binding is not None
                    self._content_presentation_epoch = binding.epoch
                    if presentation.changes_graph:
                        self._editor_preview_sequence = max(
                            self._editor_preview_sequence, request.sequence
                        )
                return _ack(request, applied=True)
        return _ack(request, applied=False, error_code="unknown_preparation",
                    error_message="no such prepared scene")

    def _scene_render_feeds(self) -> tuple[LibobsThumbnailEgress, ...]:
        """The card atlas and every scene window's feed, in a stable order."""
        feeds = tuple(self._scene_window_feeds.values())
        return feeds if self._thumbnail_egress is None else (self._thumbnail_egress, *feeds)

    def _handle_set_thumbnail_egress(self, request: SceneIpcEnvelope) -> SceneIpcEnvelope:
        if not self._runtime_started or self._thumbnail_egress is None:
            return _ack(request, applied=False, error_code="runtime_unavailable",
                        error_message="the libobs runtime is not running")
        payload = request.payload
        channel_id = payload.get("channel_id") or SCENE_THUMBNAILS_CHANNEL
        if not is_scene_render_channel(channel_id):
            return _ack(request, applied=False, error_code="thumbnail_egress_failed",
                        error_message="unknown thumbnail channel")
        descriptor = payload.get("thumbnail_egress")
        scene_ids = payload.get("scene_ids") or []
        try:
            if not isinstance(scene_ids, (list, tuple)):
                raise SceneIpcError("Invalid scene_ids")
            camera_id = camera_still_camera_id(channel_id)
            if camera_id and descriptor is not None:
                graph = self._scene_graph
                if graph is None or not graph.prepare_camera_still(camera_id):
                    return _ack(request, applied=False, error_code="camera_unavailable",
                                error_message="no scene shows that camera")
            egress = self._scene_render_feed(str(channel_id), create=descriptor is not None)
            if egress is not None:
                egress.configure(
                    descriptor,
                    tuple(str(scene_id) for scene_id in scene_ids),
                    _payload_int(payload, "cell_width"),
                    _payload_int(payload, "cell_height"),
                )
            if descriptor is None and channel_id != SCENE_THUMBNAILS_CHANNEL:
                self._close_scene_window_feed(str(channel_id))
        except Exception:  # noqa: BLE001 - thumbnails must never break the engine
            log.warning("could not configure the thumbnail egress", exc_info=True)
            return _ack(request, applied=False, error_code="thumbnail_egress_failed",
                        error_message="could not attach the thumbnail channel")
        return _ack(request, applied=True)

    def _scene_render_feed(self, channel_id: str, *, create: bool) -> LibobsThumbnailEgress | None:
        if channel_id == SCENE_THUMBNAILS_CHANNEL:
            return self._thumbnail_egress
        feed = self._scene_window_feeds.get(channel_id)
        if feed is not None or not create:
            return feed
        factory = (
            self._camera_still_factory if camera_still_camera_id(channel_id)
            else self._scene_render_factory
        )
        if factory is None:
            return None
        if len(self._scene_window_feeds) >= _MAXIMUM_SCENE_WINDOW_FEEDS:
            raise SceneIpcError("Too many scene window feeds")
        feed = factory()
        self._scene_window_feeds[channel_id] = feed
        return feed

    def _close_scene_window_feed(self, channel_id: str) -> None:
        feed = self._scene_window_feeds.pop(channel_id, None)
        if feed is None:
            return
        try:
            feed.shutdown()
        except Exception:  # noqa: BLE001 - a closing window must not break the engine
            log.warning("scene window feed shutdown errored", exc_info=True)
            return  # still rendering, perhaps: keep what it renders alive
        camera_id = camera_still_camera_id(channel_id)
        graph = self._scene_graph
        if camera_id and graph is not None:
            graph.release_camera_still(camera_id)

    def _prepare_camera_stills(self) -> None:
        """Rebuild the still scenes of open camera feeds after a graph rebuild."""
        graph = self._scene_graph
        if graph is None:
            return
        for channel_id in self._scene_window_feeds:
            camera_id = camera_still_camera_id(channel_id)
            if camera_id:
                graph.prepare_camera_still(camera_id)

    def _handle_set_window_targets(self, request: SceneIpcEnvelope) -> SceneIpcEnvelope:
        output = self._window_output
        if not self._runtime_started or output is None:
            return _ack(request, applied=False, error_code="runtime_unavailable",
                        error_message="the libobs runtime is not running")
        try:
            targets = request.payload.get("window_targets") or []
            output.set_targets(targets if isinstance(targets, list) else [])
        except Exception:  # noqa: BLE001 - a bad target must not crash the engine
            log.warning("libobs set_window_targets failed", exc_info=True)
            return _ack(request, applied=False, error_code="window_targets_failed",
                        error_message="could not bind the window displays")
        return _ack(request, applied=True)

    def shutdown(self) -> None:
        from solin.core.scenes.camera_catalog import camera_catalog

        recovery, self._camera_recovery = self._camera_recovery, None
        if recovery is not None:
            try:
                recovery.close()
            except Exception:  # noqa: BLE001 - continue releasing other runtime owners
                log.warning("Local camera recovery shutdown failed", exc_info=True)

        self._camera_health_bindings = ()
        self._camera_health_request = None
        self._camera_health.clear()
        camera_catalog().invalidate()
        self._idle_suspended = True
        callback, self._idle_callback = self._idle_callback, None
        if callback is not None and self._runtime is not None:
            try:
                cast(Any, self._runtime).ob.remove_main_render_callback(callback)
            except Exception:  # noqa: BLE001 - release the remaining runtime owners
                log.warning("Idle render callback removal failed", exc_info=True)
        self._document_revision = None
        self._editor_scene_id = ""
        self._editor_preview_sequence = -1
        # Release in reverse dependency order: virtual-camera output → window
        # displays (they hold GL surfaces on the context) → scene graph → the
        # content consumer → the runtime/context itself.
        meeting, self._meeting_output = self._meeting_output, None
        if meeting is not None:
            try:
                meeting.stop()
            except Exception:  # noqa: BLE001 - shutdown must not raise out of the sidecar
                log.warning("meeting output stop errored", exc_info=True)
        recorder, self._recorder = self._recorder, None
        if recorder is not None:
            try:
                recorder.stop()
            except Exception:  # noqa: BLE001 - shutdown must not raise out of the sidecar
                log.warning("recorder stop errored", exc_info=True)
        mixer, self._audio_mixer = self._audio_mixer, None
        if mixer is not None:
            try:
                mixer.shutdown()
            except Exception:  # noqa: BLE001 - shutdown must not raise out of the sidecar
                log.warning("audio mixer shutdown errored", exc_info=True)
        # Stop the media poller before releasing the source it samples, and release
        # our media ref before the scene graph tears down (scenes hold their own ref).
        self._stop_media_poller()
        media, self._media_source = self._media_source, None
        if media is not None:
            try:
                media.close()
            except Exception:  # noqa: BLE001 - shutdown must not raise out of the sidecar
                log.warning("media source close errored", exc_info=True)
        camera, self._virtual_camera = self._virtual_camera, None
        if camera is not None:
            try:
                camera.stop()
            except Exception:  # noqa: BLE001 - shutdown must not raise out of the sidecar
                log.warning("virtual camera stop errored", exc_info=True)
        output, self._window_output = self._window_output, None
        if output is not None:
            try:
                output.shutdown()
            except Exception:  # noqa: BLE001 - shutdown must not raise out of the sidecar
                log.warning("libobs window output shutdown errored", exc_info=True)
        # Stop the preview render before releasing the scenes it borrows.
        egress, self._preview_egress = self._preview_egress, None
        if egress is not None:
            try:
                egress.shutdown()
            except Exception:  # noqa: BLE001 - shutdown must not raise out of the sidecar
                log.warning("preview egress shutdown errored", exc_info=True)
        program_egress, self._program_egress = self._program_egress, None
        if program_egress is not None:
            try:
                program_egress.shutdown()
            except Exception:  # noqa: BLE001 - shutdown must not raise out of the sidecar
                log.warning("program egress shutdown errored", exc_info=True)
        for channel_id in tuple(self._scene_window_feeds):
            self._close_scene_window_feed(channel_id)
        thumbnails, self._thumbnail_egress = self._thumbnail_egress, None
        if thumbnails is not None:
            try:
                thumbnails.shutdown()
            except Exception:  # noqa: BLE001 - shutdown must not raise out of the sidecar
                log.warning("thumbnail egress shutdown errored", exc_info=True)
        route, self._projection_route = self._projection_route, None
        if route is not None:
            try:
                route.shutdown()
            except Exception:  # noqa: BLE001 - shutdown must not raise out of the sidecar
                log.warning("projection route shutdown errored", exc_info=True)
        graph, self._scene_graph = self._scene_graph, None
        if graph is not None:
            try:
                graph.shutdown()
            except Exception:  # noqa: BLE001 - shutdown must not raise out of the sidecar
                log.warning("libobs scene graph shutdown errored", exc_info=True)
        # Stop the content pump + release its source only after the scenes that
        # referenced it are gone.
        for source in tuple(self._retired_media_sources):
            self._release_retired_media_source(source)
        self._prepared_content.clear()
        self._stop_content_consumer()
        for wrapper in tuple(self._retired_frame_sources):
            try:
                wrapper.release()
            except Exception:  # noqa: BLE001 - shutdown must release remaining sources
                log.warning("Could not release a detached content source", exc_info=True)
            else:
                self._retired_frame_sources.remove(wrapper)
        empty, self._empty_content_source = self._empty_content_source, None
        if empty is not None:
            try:
                empty.release()
            except Exception:  # noqa: BLE001 - shutdown must release the remaining runtime
                log.warning("Could not release the empty content source", exc_info=True)
        self._content_presentation_epoch = None
        self._media_epoch = None
        idle, self._idle_source = self._idle_source, None
        for owner, _source in tuple(self._idle_presentations.values()):
            try:
                owner.close()
            except Exception:  # noqa: BLE001 - shutdown must release all owned presentations
                log.warning("Idle presentation close failed", exc_info=True)
        self._idle_presentations.clear()
        if idle is not None:
            try:
                idle.close()
            except Exception:  # noqa: BLE001 - shutdown must finish releasing the runtime
                log.warning("Idle source close failed", exc_info=True)
        runtime, self._runtime = self._runtime, None
        self._runtime_started = False
        if runtime is None:
            return
        try:
            runtime.shutdown()
        except Exception:  # noqa: BLE001 - shutdown must not raise out of the sidecar
            log.warning(
                "libobs runtime shutdown errored in the scene-engine sidecar",
                exc_info=True,
            )


def serve(
    source: BinaryIO,
    sink: BinaryIO,
    *,
    engine: LibobsSidecarEngine | None = None,
) -> None:
    """Run the control loop until the input stream closes or is corrupted.

    Exiting the loop lets the supervising client observe the process end and
    apply its restart policy. The engine is always shut down on the way out so
    the libobs runtime is released even on an unclean channel. Idle preparation
    and camera discovery use separate workers. Shutdown joins
    both before releasing the engine's native owners.
    """
    from solin.core.scenes.libobs_idle_updates import IdleScreenUpdates
    from solin.core.scenes.camera_catalog import camera_catalog

    active = engine if engine is not None else LibobsSidecarEngine()
    write_lock = threading.Lock()

    def emit(envelope: SceneIpcEnvelope) -> None:
        with write_lock:
            write_envelope(sink, envelope)

    idle_updates = IdleScreenUpdates(
        active.handle, emit, active.cancel_idle_preparation, admit=active.admit_idle_screen,
    )
    camera_discovery = _CameraDiscoveryRequests(
        active._local_camera_list, emit, query_modes=active._local_camera_modes,
        cancel=camera_catalog().cancel_discovery,
    )
    active.set_event_sink(idle_updates.emit_event)
    try:
        while True:
            try:
                request = read_envelope(source)
            except SceneIpcError:
                return
            if request is None:
                return  # clean EOF: the parent closed the pipe or exited
            try:
                if request.message_type == "set_idle_screen":
                    idle_updates.submit(request)
                    continue
                if request.message_type in {"list_local_cameras", "query_local_camera_modes"} and active.runtime_started:
                    camera_discovery.submit(request)
                    continue
                try:
                    response = active.handle(request)
                except Exception:  # noqa: BLE001 - a handler bug must not kill the sidecar
                    log.exception("scene engine handler crashed for %s", request.message_type)
                    response = _reply(request, "error", {
                        "error_code": "handler_error",
                        "error_message": f"the engine failed to handle '{request.message_type}'",
                    })
                if response is not None:
                    emit(response)
            finally:
                active.poll_outputs()
    finally:
        try:
            try:
                camera_discovery.close()
            finally:
                idle_updates.close()
        finally:
            active.set_event_sink(None)
            active.shutdown()


def main() -> int:
    runtime_factory: RuntimeFactory | None = (
        None if os.environ.get(_NO_RUNTIME_ENV) else _default_runtime_factory
    )
    with reserve_protocol_stream() as protocol_out:
        serve(
            sys.stdin.buffer,
            protocol_out,
            engine=LibobsSidecarEngine(runtime_factory),
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
