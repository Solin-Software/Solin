"""libobs scene-engine sidecar.

A separate, supervised process that speaks the scene-engine control protocol
(:mod:`solin.core.scenes.ipc_protocol`, PROTOCOL_VERSION 7) over stdin/stdout, so
it can be driven by the existing :class:`~solin.core.scenes.process_engine.SubprocessSceneEngine`
client — the *same* supervision / heartbeat / restart / IPC machinery ``main``
uses for its native GStreamer engine, pointed at libobs instead.

Stage 1a implemented the lifecycle handshake (``hello`` → ``hello_ack``,
``ping`` → ``heartbeat``, everything else → ``not_implemented``).

Stage 1b adds the **libobs runtime lifecycle**: the sidecar brings the libobs
runtime up on ``hello`` and tears it down when the control channel closes. The
boot is orchestrated through an injectable runtime factory so it is unit-testable
without a real GPU/libobs; a boot failure is non-fatal (the handshake still
completes, the engine simply advertises no capabilities). Capabilities remain
all-false until later stages actually composite/capture/record — booting the
runtime is groundwork, not yet a user-visible capability.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from typing import Any, BinaryIO, Callable

from solin.core.scenes.ipc_protocol import (
    SceneIpcEnvelope,
    SceneIpcError,
    read_envelope,
    write_envelope,
)

log = logging.getLogger(__name__)

# Capability flags advertised in the hello handshake. All False for now: the
# engine completes the lifecycle handshake and boots libobs, but composites
# nothing yet. Each flag flips to True as its stage lands:
#   hardware_compositing                -> scene graph + transitions
#   local_cameras / rtsp_cameras        -> camera sources
#   virtual_camera                      -> obs_view -> DShow sink
#   program_recording / audio_*_capture -> recording + audio
#   d3d11_shared_textures               -> not planned (libobs sources are fed directly)
# The key set must match SubprocessSceneEngine's hello contract exactly.
CAPABILITIES: dict[str, bool] = {
    "local_cameras": False,
    "rtsp_cameras": False,
    "hardware_compositing": False,
    "virtual_camera": False,
    "d3d11_shared_textures": False,
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

# A runtime factory yields an object exposing ``ensure_started()`` / ``shutdown()``
# (the :class:`~solin.core.media.obs_runtime.ObsRuntime` contract). It is injected
# so the boot lifecycle can be exercised without a real libobs runtime.
RuntimeFactory = Callable[[], object]


def _default_runtime_factory() -> object:
    from solin.core.media.obs_runtime import obs_runtime

    return obs_runtime()


def _reply(
    request: SceneIpcEnvelope,
    message_type: str,
    payload: dict[str, object],
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
        payload=payload,
    )


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
        self._runtime: object | None = None
        self._runtime_started = False
        self._scene_graph: object | None = None
        self._window_output: object | None = None
        self._projection_route: object | None = None
        self._thumbnail_egress: object | None = None
        self._content_consumer: Any | None = None
        self._preview_egress: Any | None = None
        self._program_egress: Any | None = None
        self._virtual_camera: Any | None = None
        self._recorder: Any | None = None
        self._audio_mixer: Any | None = None
        self._camera_generation = 0
        self._media_source: Any | None = None
        self._bg_media_source: Any | None = None
        self._media_lock = threading.Lock()
        self._media_poller: threading.Thread | None = None
        self._media_poll_stop = threading.Event()
        self._media_trim_start_ms = 0
        self._media_trim_end_from_ms = 0  # ms trimmed from the END (0 = play to natural end)
        self._media_trim_ended = False  # latched once the trim window's end is reached
        self._event_sink: Callable[[SceneIpcEnvelope], None] | None = None
        self._session_id = "not-started"
        self._process_generation = "not-started"

    @property
    def runtime_started(self) -> bool:
        return self._runtime_started

    def set_event_sink(self, sink: Callable[[SceneIpcEnvelope], None] | None) -> None:
        """Register the (thread-safe) writer serve() uses for unsolicited events."""
        self._event_sink = sink

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
            runtime.ensure_started()  # type: ignore[attr-defined]
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
            lambda scene_id: self._scene_graph.scene_source(scene_id)  # type: ignore[attr-defined]
            if self._scene_graph is not None else None
        )
        from solin.core.scenes.libobs_preview_egress import LibobsPreviewEgress
        from solin.core.scenes.libobs_program_egress import LibobsProgramEgress

        from solin.core.scenes.libobs_thumbnail_egress import LibobsThumbnailEgress

        self._preview_egress = LibobsPreviewEgress(runtime)
        self._thumbnail_egress = LibobsThumbnailEgress(
            runtime,
            lambda scene_id: self._scene_graph.scene_source(scene_id)  # type: ignore[attr-defined]
            if self._scene_graph is not None else None,
        )
        self._program_egress = LibobsProgramEgress(runtime)
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

    def handle(self, request: SceneIpcEnvelope) -> SceneIpcEnvelope | None:
        # Remember correlation ids so unsolicited events (recording state) are
        # accepted by the client (it validates session + generation on events).
        self._session_id = request.session_id
        self._process_generation = request.process_generation
        message_type = request.message_type
        if message_type == "hello":
            self._boot_runtime()
            return _reply(request, "hello_ack", self._capabilities())
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
                graph.cancel_all()  # type: ignore[attr-defined]
            return None  # a notification — no response
        if message_type == "reload_yeartext":
            graph = self._scene_graph
            if graph is not None:
                graph.reload_yeartext()  # type: ignore[attr-defined]
            return None  # a notification — no response
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
        # device-unavailable warning (mirrors the native engine's mid-recording event).
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

    def _local_camera_list(self) -> dict[str, object]:
        from solin.core.scenes.v4l2_camera_discovery import (
            discover_local_cameras,
            serialize_camera_device,
        )

        try:
            devices = discover_local_cameras()
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

    # ── media control (Fork A: libobs decodes; app drives over IPC) ─────────

    def _media_slot(self, payload: dict) -> int:
        from solin.core.scenes.media_control import MEDIA_SLOT_BACKGROUND

        return MEDIA_SLOT_BACKGROUND if int(payload.get("slot", 0) or 0) else 0

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
        volume = int(payload.get("volume_percent", 100) or 0)
        speed = int(payload.get("speed_percent", 100) or 0) or 100
        trim_start = max(0, int(payload.get("trim_start_ms", 0) or 0))
        trim_end = max(0, int(payload.get("trim_end_ms", 0) or 0))
        with self._media_lock:
            opened = media.open(path, autoplay=autoplay, is_local_file=is_local,
                                volume_percent=volume, speed_percent=speed)
            if opened and not slot:
                # Foreground slot: apply trim and composite into the scene content.
                self._media_trim_start_ms = trim_start
                self._media_trim_end_from_ms = trim_end  # from the end
                self._media_trim_ended = False
                if trim_start:
                    media.seek(trim_start)
                if self._scene_graph is not None:
                    try:
                        self._scene_graph.set_content_source(media.source)  # type: ignore[attr-defined]
                    except Exception:  # noqa: BLE001 - a scene error must not kill the sidecar
                        log.warning("could not route media into the content slot",
                                    exc_info=True)
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
        position_ms = max(0, int(request.payload.get("position_ms", 0) or 0))
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
                media.close()
                if not slot:
                    self._media_trim_start_ms = 0
                    self._media_trim_end_from_ms = 0
                    self._media_trim_ended = False
                    if self._scene_graph is not None:
                        try:
                            self._scene_graph.set_content_source(  # type: ignore[attr-defined]
                                self._frame_content_source()
                            )
                        except Exception:  # noqa: BLE001 - a scene error must not kill the sidecar
                            log.warning("could not revert the content slot", exc_info=True)
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
        volume = int(request.payload.get("volume_percent", 100) or 0)
        speed = int(request.payload.get("speed_percent", 100) or 0) or 100
        with self._media_lock:
            media.set_volume(volume)
            media.set_speed(speed)
        return _ack(request, applied=True)

    def _frame_content_source(self) -> Any | None:
        consumer = self._content_consumer
        return consumer.source if consumer is not None else None

    def _effective_content_source(self) -> Any | None:
        """The content-slot source: an open media source wins over BGRA ingress."""
        media = self._media_source
        if media is not None and media.source is not None:
            return media.source
        return self._frame_content_source()

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
        return (state, rel_position, rel_duration, path)

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

    def _handle_preview_layer_geometry(self, request: SceneIpcEnvelope) -> SceneIpcEnvelope:
        graph = self._scene_graph
        if not self._runtime_started or graph is None:
            return _ack(request, applied=False, error_code="runtime_unavailable",
                        error_message="the libobs runtime is not running")
        payload = request.payload
        scene_id = str(payload.get("scene_id") or "")
        layer = payload.get("layer") or {}
        layer_id = str(layer.get("id") or "") if isinstance(layer, dict) else ""
        rect = layer.get("rect") if isinstance(layer, dict) else None
        # Apply the transform to the live scene item — no re-hydrate, so a resize
        # or move takes effect without rebuilding sources (the camera stays up).
        applied = graph.apply_layer_geometry(scene_id, layer_id, rect or {})  # type: ignore[attr-defined]
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
        if enabled:
            if camera.start():
                return _ack(request, applied=True)
            return _ack(request, applied=False, error_code="virtual_camera_unavailable",
                        error_message="the virtual camera device is not available")
        camera.stop()
        return _ack(request, applied=True)

    def _handle_hydrate(self, request: SceneIpcEnvelope) -> SceneIpcEnvelope:
        graph = self._scene_graph
        if not self._runtime_started or graph is None:
            return _ack(request, applied=False, error_code="runtime_unavailable",
                        error_message="the libobs runtime is not running")
        try:
            payload = request.payload
            self._reconcile_content_ingress(payload.get("content_ingress"))
            # Stop the preview render on the old (about-to-be-released) scenes
            # before rebuilding — the egress renders a borrowed scene source.
            if self._preview_egress is not None:
                self._preview_egress.set_scene_source(None)
            active_scenes = payload.get("active_scenes") or {}
            # An open media source owns the content slot; otherwise the BGRA
            # frame-ingress source does (both may be absent → placeholder).
            # Hold the window-output lock so a per-scene draw callback on the
            # graphics thread can't resolve a scene while it is being released.
            from contextlib import nullcontext

            output = self._window_output
            rebuild_guard = output.hydrate_lock if output is not None else nullcontext()
            with rebuild_guard:
                graph.hydrate(  # type: ignore[attr-defined]
                    payload.get("document") or {},
                    active_scenes,
                    self._effective_content_source(),
                )
            self._reconcile_projection(active_scenes)
            self._reconcile_preview_egress(
                payload.get("preview_egress"),
                active_scenes,
                payload.get("render_enabled") or {},
            )
            if self._program_egress is not None:
                self._program_egress.configure(payload.get("program_egress"))
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
                    output.set_targets(targets if isinstance(targets, list) else [])  # type: ignore[attr-defined]
                except Exception:  # noqa: BLE001 - a bad target must not fail the hydrate
                    log.warning("libobs hydrate window-target rebind failed", exc_info=True)
            # Likewise restore the virtual-camera output from the snapshot's
            # desired output state, so the meeting's program output comes back on
            # its own after a restart. start()/stop() are idempotent, so this is a
            # no-op on the structural-edit re-hydrate path when it's already right.
            camera = self._virtual_camera
            output_enabled = payload.get("output_enabled") or {}
            if camera is not None and "virtual_camera" in output_enabled:
                try:
                    if output_enabled["virtual_camera"]:
                        camera.start()
                    else:
                        camera.stop()
                except Exception:  # noqa: BLE001 - vcam must not fail the hydrate
                    log.warning("libobs hydrate virtual-camera restore failed", exc_info=True)
        except Exception:  # noqa: BLE001 - a bad document must not crash the engine
            log.warning("libobs hydrate failed", exc_info=True)
            return _ack(request, applied=False, error_code="hydrate_failed",
                        error_message="could not build the libobs scene graph")
        return _ack(request, applied=True)

    def _reconcile_projection(self, active_scenes: dict) -> None:
        """Point the projection output at its bus's scene (a cut, on hydrate)."""
        route = self._projection_route
        graph = self._scene_graph
        if route is None or graph is None:
            return
        scene_id = str(
            active_scenes.get("media_windows")
            or active_scenes.get("virtual_camera")
            or ""
        )
        source = graph.scene_source(scene_id) if scene_id else None  # type: ignore[attr-defined]
        route.set_scene(scene_id, source)

    def _reconcile_preview_egress(
        self,
        descriptor: object,
        active_scenes: dict,
        render_enabled: dict | None = None,
    ) -> None:
        egress = self._preview_egress
        graph = self._scene_graph
        if egress is None or graph is None:
            return
        egress.configure(descriptor)  # attach/detach the writer to the app's block
        # The editor previews the MEDIA_WINDOWS (edit) bus scene.
        preview_scene_id = (
            active_scenes.get("editor")
            or active_scenes.get("media_windows")
            or active_scenes.get("virtual_camera")
        )
        source = graph.scene_source(preview_scene_id) if preview_scene_id else None  # type: ignore[attr-defined]
        egress.set_scene_source(source)
        # Hydrate is the full-state sync: after a restart the sidecar is fresh and
        # the egress defaults to disabled, while the app's edge-triggered render
        # cache still reads "enabled" and so never re-sends set_render_enabled.
        # Apply the snapshot's render demand here so one hydrate fully restores the
        # editor preview (source *and* enable) — the canvas updates after a restart.
        for key in ("editor", "media_windows"):
            if render_enabled and key in render_enabled:
                egress.set_enabled(bool(render_enabled[key]))
                break

    def _handle_set_render_enabled(self, request: SceneIpcEnvelope) -> SceneIpcEnvelope:
        # The MEDIA_WINDOWS bus render demand gates the editor preview egress;
        # the program bus always composites, so just ack it.
        payload = request.payload
        if payload.get("bus_id") == "editor" and self._preview_egress is not None:
            self._preview_egress.set_enabled(bool(payload.get("enabled")))
        return _ack(request, applied=True)

    def _reconcile_content_ingress(self, descriptor: object) -> None:
        from solin.core.scenes.content_frame_consumer import (
            SHARED_MEMORY_BGRA,
            ContentFrameConsumer,
        )

        if not isinstance(descriptor, dict) or descriptor.get("transport") != SHARED_MEMORY_BGRA:
            self._stop_content_consumer()  # no supported content channel
            return
        token = descriptor.get("handle_token")
        current = self._content_consumer
        if current is not None and current.handle_token == token:
            return  # already consuming this exact channel
        self._stop_content_consumer()
        consumer = ContentFrameConsumer(self._runtime, descriptor)
        if consumer.start():
            self._content_consumer = consumer

    def _stop_content_consumer(self) -> None:
        consumer, self._content_consumer = self._content_consumer, None
        if consumer is not None:
            try:
                consumer.stop()
            except Exception:  # noqa: BLE001 - shutdown must not raise
                log.warning("content frame consumer stop errored", exc_info=True)

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
        transition = payload.get("transition") or {}
        kind = str(transition.get("kind", "cut"))
        duration_ms = int(transition.get("duration_ms", 0) or 0)
        result = graph.prepare(scene_id, kind, duration_ms, str(bus_id or ""))  # type: ignore[attr-defined]
        if result is None:
            return _reply(request, "error", {
                "error_code": "unknown_scene",
                "error_message": f"no scene {scene_id!r}",
            })
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
        route = graph.pending_route(token)  # type: ignore[attr-defined]
        if route is None:
            return _ack(request, applied=False, error_code="unknown_preparation",
                        error_message="no such prepared scene")
        if route != bus_id:
            return _ack(request, applied=False, error_code="bus_mismatch",
                        error_message="the prepared scene belongs to another output")
        if bus_id == "editor":
            # The editor channel drives no output: taking one of its scenes only
            # re-points the off-screen preview egress, so the token is dropped
            # rather than executed.
            if not graph.discard(token):  # type: ignore[attr-defined]
                return _ack(request, applied=False, error_code="unknown_preparation",
                            error_message="no such prepared scene")
            egress = self._preview_egress
            if egress is not None:
                scene_id = str(payload.get("scene_id") or "")
                source = graph.scene_source(scene_id) if scene_id else None  # type: ignore[attr-defined]
                egress.set_scene_source(source)
            return _ack(request, applied=True)
        if bus_id == "media_windows":
            # Projection animates on its OWN transition, independent of the program.
            if graph.take_projection(token, self._projection_route):  # type: ignore[attr-defined]
                return _ack(request, applied=True)
            return _ack(request, applied=False, error_code="unknown_preparation",
                        error_message="no such prepared scene")
        if graph.take(token):  # type: ignore[attr-defined]
            return _ack(request, applied=True)
        return _ack(request, applied=False, error_code="unknown_preparation",
                    error_message="no such prepared scene")

    def _handle_set_thumbnail_egress(self, request: SceneIpcEnvelope) -> SceneIpcEnvelope:
        egress = self._thumbnail_egress
        if not self._runtime_started or egress is None:
            return _ack(request, applied=False, error_code="runtime_unavailable",
                        error_message="the libobs runtime is not running")
        payload = request.payload
        scene_ids = payload.get("scene_ids") or []
        try:
            egress.configure(  # type: ignore[attr-defined]
                payload.get("thumbnail_egress"),
                tuple(str(scene_id) for scene_id in scene_ids),
                int(payload.get("cell_width") or 0),
                int(payload.get("cell_height") or 0),
            )
        except Exception:  # noqa: BLE001 - thumbnails must never break the engine
            log.warning("could not configure the thumbnail egress", exc_info=True)
            return _ack(request, applied=False, error_code="thumbnail_egress_failed",
                        error_message="could not attach the thumbnail channel")
        return _ack(request, applied=True)

    def _handle_set_window_targets(self, request: SceneIpcEnvelope) -> SceneIpcEnvelope:
        output = self._window_output
        if not self._runtime_started or output is None:
            return _ack(request, applied=False, error_code="runtime_unavailable",
                        error_message="the libobs runtime is not running")
        try:
            targets = request.payload.get("window_targets") or []
            output.set_targets(targets if isinstance(targets, list) else [])  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - a bad target must not crash the engine
            log.warning("libobs set_window_targets failed", exc_info=True)
            return _ack(request, applied=False, error_code="window_targets_failed",
                        error_message="could not bind the window displays")
        return _ack(request, applied=True)

    def shutdown(self) -> None:
        # Release in reverse dependency order: virtual-camera output → window
        # displays (they hold GL surfaces on the context) → scene graph → the
        # content consumer → the runtime/context itself.
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
                output.shutdown()  # type: ignore[attr-defined]
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
                graph.shutdown()  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001 - shutdown must not raise out of the sidecar
                log.warning("libobs scene graph shutdown errored", exc_info=True)
        # Stop the content pump + release its source only after the scenes that
        # referenced it are gone.
        self._stop_content_consumer()
        runtime, self._runtime = self._runtime, None
        self._runtime_started = False
        if runtime is None:
            return
        try:
            runtime.shutdown()  # type: ignore[attr-defined]
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
    the libobs runtime is released even on an unclean channel.
    """
    active = engine if engine is not None else LibobsSidecarEngine()
    write_lock = threading.Lock()

    def emit(envelope: SceneIpcEnvelope) -> None:
        with write_lock:
            write_envelope(sink, envelope)

    active.set_event_sink(emit)
    try:
        while True:
            try:
                request = read_envelope(source)
            except SceneIpcError:
                return
            if request is None:
                return  # clean EOF: the parent closed the pipe or exited
            try:
                response = active.handle(request)
            except Exception:  # noqa: BLE001 - a handler bug must not kill the sidecar
                log.exception("scene engine handler crashed for %s", request.message_type)
                response = _reply(request, "error", {
                    "error_code": "handler_error",
                    "error_message": f"the engine failed to handle '{request.message_type}'",
                })
            if response is not None:
                with write_lock:
                    write_envelope(sink, response)
    finally:
        active.set_event_sink(None)
        active.shutdown()


def _reserve_protocol_stream() -> BinaryIO:
    """Move the control channel off fd 1 and repoint fd 1 at stderr.

    libobs (and the OBS plugins it loads) write diagnostics to stdout at the C
    level; on the raw fd that would corrupt the length-prefixed frame protocol,
    which owns stdout. We dup the real stdout to a private fd for frames, then
    redirect fd 1 → stderr so any native/print output is drained harmlessly by
    the client's stderr reader instead of breaking the channel.
    """
    stdout_fd = sys.stdout.fileno()
    protocol_fd = os.dup(stdout_fd)
    os.dup2(sys.stderr.fileno(), stdout_fd)
    return os.fdopen(protocol_fd, "wb", buffering=0)


def main() -> int:
    protocol_out = _reserve_protocol_stream()
    runtime_factory: RuntimeFactory | None = (
        None if os.environ.get(_NO_RUNTIME_ENV) else _default_runtime_factory
    )
    serve(
        sys.stdin.buffer,
        protocol_out,
        engine=LibobsSidecarEngine(runtime_factory),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
