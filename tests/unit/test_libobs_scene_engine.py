"""Stage 1a — libobs scene-engine sidecar handshake + factory.

The sidecar's replies are validated with the *real* client-side parsers from
``process_engine`` so the two sides are proven wire-compatible, and one test
spawns the sidecar under the actual ``SubprocessSceneEngine`` supervisor to prove
the full handshake end-to-end.
"""

from __future__ import annotations

import io
import os
import sys
import tempfile
import time
import types
from pathlib import Path

from solin.core.scenes.engine import SceneEngineStatus
from solin.core.scenes.recording import (
    AudioDeviceSelection,
    AudioSelectionMode,
    ProgramRecordingStatus,
)
from solin.core.scenes.ipc_protocol import (
    PROTOCOL_VERSION,
    SceneIpcEnvelope,
    encode_envelope,
    read_envelope,
)
from solin.core.scenes.libobs_engine import (
    ENGINE_SELECTION_ENV,
    create_libobs_scene_engine,
    libobs_scene_engine_selected,
)
from solin.core.scenes.libobs_sidecar import (
    CAPABILITIES,
    LibobsSidecarEngine,
    build_response,
    serve,
)
from solin.core.scenes.media_control import (
    MediaPlaybackState,
)
from solin.core.scenes.process_engine import (
    SceneEngineCommandRejectedError,
    _ack_from_envelope,
    _audio_device_discovery_from_envelope,
    _capabilities_from_envelope,
    _command_error_from_envelope,
    _heartbeat_from_envelope,
    _local_camera_discovery_from_envelope,
    _media_playback_event_from_envelope,
    _program_recording_event_from_envelope,
)

_SIDECAR_MODULE = "solin.core.scenes.libobs_sidecar"


# The engine only accepts an absolute .mp4 recording path (see recording.py's
# ProgramRecordingNativeState). A POSIX literal is not absolute on Windows —
# Path("/tmp/x.mp4").is_absolute() is False there — so build the fixtures from the
# platform's temp directory instead, which is absolute on every host.
_RECORDING_DIR = Path(tempfile.gettempdir())


def _recording_path(name: str) -> str:
    """An absolute, platform-valid .mp4 path for recording fixtures."""
    return str(_RECORDING_DIR / name)


def _request(
    message_type: str,
    payload: dict[str, object] | None = None,
    *,
    request_id: str = "req-1",
    sequence: int = 0,
    document_revision: int = 0,
) -> SceneIpcEnvelope:
    return SceneIpcEnvelope(
        message_type=message_type,
        request_id=request_id,
        session_id="sess-1",
        process_generation="gen-1",
        sequence=sequence,
        document_revision=document_revision,
        deadline_monotonic_ms=123_456,
        payload=payload or {},
    )


def _assert_correlation_echoed(response: SceneIpcEnvelope, request: SceneIpcEnvelope) -> None:
    assert response.request_id == request.request_id
    assert response.session_id == request.session_id
    assert response.process_generation == request.process_generation
    assert response.sequence == request.sequence
    assert response.document_revision == request.document_revision
    assert response.deadline_monotonic_ms == request.deadline_monotonic_ms


# ── handshake responses ──────────────────────────────────────────────────────


def test_hello_returns_capabilities_parseable_by_client():
    request = _request("hello", {"parent_process_id": 42})
    response = build_response(request)

    assert response is not None
    assert response.message_type == "hello_ack"
    _assert_correlation_echoed(response, request)

    # The real client parser must accept the payload as valid capabilities.
    capabilities = _capabilities_from_envelope(response)
    assert capabilities.protocol_version == PROTOCOL_VERSION
    assert capabilities.process_generation == "gen-1"
    # Stage 1a advertises nothing composited yet.
    assert capabilities.hardware_compositing is False
    assert capabilities.virtual_camera is False
    assert capabilities.program_recording is False
    assert capabilities.local_cameras is False


def test_capability_keys_match_client_hello_contract():
    # The hello_ack payload keys must be exactly what the client expects, else
    # _capabilities_from_envelope rejects the handshake.
    from solin.core.scenes.process_engine import _HELLO_FIELDS

    assert set(CAPABILITIES) == set(_HELLO_FIELDS)
    assert all(isinstance(value, bool) for value in CAPABILITIES.values())


def test_ping_returns_heartbeat():
    request = _request("ping")
    response = build_response(request)

    assert response is not None
    assert response.message_type == "heartbeat"
    # the payload must satisfy the client's strict heartbeat validation (a
    # non-negative monotonic timestamp) or the supervisor restarts the sidecar
    assert _heartbeat_from_envelope(response) >= 0
    _assert_correlation_echoed(response, request)


def test_cancel_preparation_is_a_silent_notification():
    assert build_response(_request("cancel_preparation", {"cancelled_request_id": "x"})) is None


def test_unknown_message_returns_not_implemented_error():
    request = _request("hydrate", {"document": {}})
    response = build_response(request)

    assert response is not None
    assert response.message_type == "error"
    _assert_correlation_echoed(response, request)

    # The real client parser must turn it into a command rejection, not a crash.
    error = _command_error_from_envelope(response)
    assert isinstance(error, SceneEngineCommandRejectedError)
    assert error.error_code == "not_implemented"


# ── serve loop ───────────────────────────────────────────────────────────────


def test_serve_processes_frames_then_stops_on_eof():
    source = io.BytesIO(
        encode_envelope(_request("hello", {"parent_process_id": 1}))
        + encode_envelope(_request("ping", request_id="req-2"))
    )
    sink = io.BytesIO()

    serve(source, sink)

    replies = io.BytesIO(sink.getvalue())
    first = read_envelope(replies)
    second = read_envelope(replies)
    assert first is not None and first.message_type == "hello_ack"
    assert second is not None and second.message_type == "heartbeat"
    assert read_envelope(replies) is None  # nothing extra, clean stop on EOF


def test_serve_stops_on_a_corrupt_frame():
    # A truncated length-prefixed frame must end the loop rather than hang/raise.
    source = io.BytesIO(b"\x00\x00\x00\x10short")
    sink = io.BytesIO()

    serve(source, sink)

    assert sink.getvalue() == b""


def test_serve_turns_a_handler_crash_into_an_error_not_a_process_exit():
    class _CrashingEngine(LibobsSidecarEngine):
        def handle(self, request):
            if request.message_type == "hydrate":
                raise RuntimeError("boom")
            return super().handle(request)

    source = io.BytesIO(
        encode_envelope(_request("hydrate", {"document": {}}, request_id="req-1"))
        + encode_envelope(_request("ping", request_id="req-2"))
    )
    sink = io.BytesIO()

    serve(source, sink, engine=_CrashingEngine())

    replies = io.BytesIO(sink.getvalue())
    first = read_envelope(replies)
    second = read_envelope(replies)
    # the crash became an error reply, and the loop kept serving the next request
    assert first is not None and first.message_type == "error"
    assert first.payload["error_code"] == "handler_error"
    assert second is not None and second.message_type == "heartbeat"


# ── libobs runtime lifecycle (Stage 1b) ──────────────────────────────────────


class _FakeRuntime:
    """Stand-in for ObsRuntime: records boot/shutdown, can simulate failure."""

    def __init__(self, *, fail_start: bool = False) -> None:
        self.starts = 0
        self.shutdowns = 0
        self._fail_start = fail_start

    def ensure_started(self, **_kwargs) -> None:
        if self._fail_start:
            raise RuntimeError("pylibobs is not installed")
        self.starts += 1

    def shutdown(self) -> None:
        self.shutdowns += 1


def test_hello_boots_the_runtime_and_shutdown_releases_it():
    runtime = _FakeRuntime()
    engine = LibobsSidecarEngine(runtime_factory=lambda: runtime)

    response = engine.handle(_request("hello", {"parent_process_id": 1}))

    assert response is not None and response.message_type == "hello_ack"
    assert runtime.starts == 1
    assert engine.runtime_started is True

    engine.shutdown()
    assert runtime.shutdowns == 1
    assert engine.runtime_started is False


def test_runtime_boots_only_once():
    runtime = _FakeRuntime()
    engine = LibobsSidecarEngine(runtime_factory=lambda: runtime)

    engine.handle(_request("hello"))
    engine.handle(_request("hello", request_id="req-2"))

    assert runtime.starts == 1


def test_boot_failure_is_non_fatal_to_the_handshake():
    runtime = _FakeRuntime(fail_start=True)
    engine = LibobsSidecarEngine(runtime_factory=lambda: runtime)

    response = engine.handle(_request("hello"))

    # The handshake still completes so the app degrades gracefully.
    assert response is not None and response.message_type == "hello_ack"
    assert engine.runtime_started is False
    # No runtime was retained, so shutdown is a no-op (never touches a dead ctx).
    engine.shutdown()
    assert runtime.shutdowns == 0


def test_engine_without_a_factory_never_touches_libobs():
    # Protocol-only mode (the serve() default) must not boot a runtime.
    engine = LibobsSidecarEngine()
    response = engine.handle(_request("hello"))
    assert response is not None and response.message_type == "hello_ack"
    assert engine.runtime_started is False


def test_serve_boots_then_shuts_down_the_injected_engine():
    runtime = _FakeRuntime()
    engine = LibobsSidecarEngine(runtime_factory=lambda: runtime)
    source = io.BytesIO(encode_envelope(_request("hello", {"parent_process_id": 1})))
    sink = io.BytesIO()

    serve(source, sink, engine=engine)

    assert runtime.starts == 1
    assert runtime.shutdowns == 1  # released on EOF via the finally block


# ── hydrate → obs_scene compositing (fake libobs runtime) ────────────────────


class _FakeItem:
    def __init__(self, source, scene=None) -> None:
        self.source = source
        self.scene = scene
        self.pos = (0.0, 0.0)
        self.bounds = (0.0, 0.0)
        self.bounds_type = 0
        self.bounds_alignment = 0
        self.order_position = 0
        self.removed = False

    def remove(self) -> None:
        self.removed = True
        if self.scene is not None and self in self.scene.items:
            self.scene.items.remove(self)


class _FakeScene:
    def __init__(self, name: str) -> None:
        self.name = name
        self.items: list[_FakeItem] = []
        self.released = 0

    def add(self, source) -> _FakeItem:
        item = _FakeItem(source, scene=self)
        item.order_position = len(self.items)  # appended on top, like obs_scene_add
        self.items.append(item)
        return item

    def as_source(self) -> str:
        return f"scene-source:{self.name}"

    def release(self) -> None:
        self.released += 1


class _FakeColorSource:
    def __init__(self, kind: str, name: str, settings: dict) -> None:
        self.kind = kind
        self.name = name
        self.settings = dict(settings)
        self.released = 0

    def update(self, settings: dict) -> None:
        self.settings = dict(settings)

    def release(self) -> None:
        self.released += 1


class _FakeMediaSource:
    """A fake ffmpeg_source exposing the obs media API LibobsMediaSource drives."""

    def __init__(self, kind: str, name: str, settings: dict) -> None:
        self.kind = kind
        self.name = name
        self.settings = dict(settings)
        self.released = 0
        self.volume = 1.0
        self.media_time = 0
        self._duration = 60_000
        self._state = 5  # STATE_STOPPED
        self.play_pause_calls: list[bool] = []
        self.stops = 0
        self.restarts = 0

    def media_play_pause(self, pause: bool) -> None:
        self.play_pause_calls.append(pause)
        self._state = 4 if pause else 1  # PAUSED / PLAYING

    def media_stop(self) -> None:
        self.stops += 1
        self._state = 5

    def media_restart(self) -> None:
        self.restarts += 1
        self.media_time = 0
        self._state = 1

    @property
    def media_duration(self) -> int:
        return self._duration

    @property
    def media_state(self) -> int:
        return self._state

    def update(self, settings: dict) -> None:
        self.settings.update(settings)

    def release(self) -> None:
        self.released += 1


class _FakeListItem:
    def __init__(self, name: str, value: str) -> None:
        self.name = name
        self.value = value


class _FakeProperty:
    def __init__(self, items: list) -> None:
        self.items = list(items)


class _FakeProperties:
    """Mimics pylibobs Properties: .get(name) -> Property(.items), .release()."""

    def __init__(self, items: list) -> None:
        self._items = list(items)
        self.released = 0

    def get(self, name: str):
        return _FakeProperty(self._items) if name == "device_id" else None

    def names(self) -> list[str]:
        return ["device_id"]

    def release(self) -> None:
        self.released += 1


class _FakeEncoder:
    def __init__(self, kind: str, name: str, settings: dict, attach_global: bool = True) -> None:
        self.kind = kind
        self.name = name
        self.settings = dict(settings)
        # True means the encoder binds libobs' global mix — the same composite the
        # virtual camera outputs.
        self.attach_global = attach_global
        self.released = 0

    def release(self) -> None:
        self.released += 1


class _FakeOutput:
    def __init__(self, kind: str, name: str, settings: dict) -> None:
        self.kind = kind
        self.name = name
        self.settings = dict(settings)
        self.media: tuple | None = None
        self.video_encoder = None
        self.audio_encoder = None
        self.started = 0
        self.stopped = 0
        self.released = 0
        self.start_ok = True
        self.failing_encoders: set[str] = set()

    def set_media(self, video, audio) -> None:
        self.media = (video, audio)

    def set_video_encoder(self, encoder) -> None:
        self.video_encoder = encoder

    def set_audio_encoder(self, encoder, idx: int = 0) -> None:
        self.audio_encoder = encoder

    def start(self) -> bool:
        self.started += 1
        if not self.start_ok:
            return False
        # A hardware encoder that creates fine but won't start (no GPU session).
        if getattr(self.video_encoder, "kind", None) in self.failing_encoders:
            return False
        return True

    def stop(self, *args, **kwargs) -> None:
        self.stopped += 1

    def release(self) -> None:
        self.released += 1


class _FakeTransition:
    def __init__(self, kind: str, name: str, settings: dict) -> None:
        self.kind = kind
        self.name = name
        self.settings = dict(settings)
        self.current_source = None
        self.starts: list[tuple] = []
        self.size = None
        self.released = 0

    def set_source(self, source) -> None:
        self.current_source = source

    def start(self, destination, duration_ms=500, mode=0) -> bool:
        self.starts.append((destination, duration_ms))
        self.current_source = destination
        return True

    def set_size(self, cx: int, cy: int) -> None:
        self.size = (cx, cy)

    def release(self) -> None:
        self.released += 1


class _FakeCanvas:
    width = 1920
    height = 1080
    fps = 30


class _CompositingRuntime:
    """Fake ObsRuntime with just enough libobs surface for the scene builder."""

    def __init__(self) -> None:
        self.video = _FakeCanvas()
        self.scenes: list[_FakeScene] = []
        self.sources: list[_FakeColorSource] = []
        self.transitions: list[_FakeTransition] = []
        self.outputs: list[_FakeOutput] = []
        self.camera_sources: list[tuple[str, str]] = []
        self.rtsp_sources: dict[str, object] = {}
        self.rtsp_requests: list[tuple[str, str]] = []
        self.channels: dict[int, object] = {}
        self.released_channels: list[int] = []
        self.output_types = ["virtualcam_output", "mp4_output", "ffmpeg_muxer"]
        self.output_start_ok = True
        # Encoders the runtime "loaded". Default is CPU-only so existing tests get
        # x264; HW-fallback tests extend this. ``fail_encoder_kinds`` makes
        # VideoEncoder.create raise for a kind; ``failing_video_encoders`` makes an
        # output bound to that encoder fail to start (create-time vs start-time HW
        # failure).
        self.encoder_types = ["obs_x264"]
        self.fail_encoder_kinds: set[str] = set()
        self.failing_video_encoders: set[str] = set()
        self.source_types = [
            "color_source_v3", "ffmpeg_source", "solin_frame_source",
            "pulse_input_capture", "pulse_output_capture",
        ]
        # Failure-injection hooks for the audio-mixer tests.
        self.fail_source_kinds: set[str] = set()  # Source.create -> None for these
        self.enum_source_types_raises = False     # enum_source_types() raises
        self.fail_set_channel_once = False         # next set_channel_source raises
        # device_id property items per capture kind: (display_name, device_id).
        # The synthetic "default" entry is present, as libobs really returns it.
        self.audio_devices_by_kind = {
            "pulse_input_capture": [
                ("Default", "default"),
                ("USB Microphone", "usb-mic-1"),
                ("Built-in Mic", "builtin-mic"),
            ],
            "pulse_output_capture": [
                ("Default", "default"),
                ("Speakers", "speakers-1"),
            ],
        }
        self.starts = 0
        self.shutdowns = 0
        self._next_channel = 0
        runtime = self

        class _SceneNS:
            @staticmethod
            def create(name: str) -> _FakeScene:
                scene = _FakeScene(name)
                runtime.scenes.append(scene)
                return scene

        class _SourceNS:
            @staticmethod
            def create(kind: str, name: str, settings: dict):
                if kind in runtime.fail_source_kinds:
                    return None  # simulate a plugin refusing the device
                if kind == "ffmpeg_source":
                    source = _FakeMediaSource(kind, name, settings)
                else:
                    source = _FakeColorSource(kind, name, settings)
                runtime.sources.append(source)
                return source

        class _TransitionNS:
            @staticmethod
            def create(kind: str, name: str, settings: dict | None = None) -> _FakeTransition:
                transition = _FakeTransition(kind, name, settings or {})
                runtime.transitions.append(transition)
                return transition

        class _OutputNS:
            @staticmethod
            def create(kind: str, name: str, settings: dict) -> _FakeOutput:
                output = _FakeOutput(kind, name, settings)
                output.start_ok = runtime.output_start_ok
                output.failing_encoders = runtime.failing_video_encoders
                runtime.outputs.append(output)
                return output

        class _VideoEncoderNS:
            @staticmethod
            def create(kind: str, name: str, settings: dict, *, attach_global: bool = True):
                if kind in runtime.fail_encoder_kinds:
                    raise RuntimeError(f"cannot create {kind}")  # HW encoder absent
                encoder = _FakeEncoder(kind, name, settings, attach_global)
                runtime.encoders.append(encoder)
                return encoder

        class _AudioEncoderNS:
            @staticmethod
            def create(kind: str, name: str, settings: dict, mixer_idx: int = 0,
                       *, attach_global: bool = True):
                encoder = _FakeEncoder(kind, name, settings, attach_global)
                runtime.encoders.append(encoder)
                return encoder

        def _enum_source_types():
            if runtime.enum_source_types_raises:
                raise RuntimeError("enum_source_types failed")
            return list(runtime.source_types)

        class _PropertiesNS:
            @staticmethod
            def from_source_id(kind: str):
                items = [
                    _FakeListItem(name, value)
                    for name, value in runtime.audio_devices_by_kind.get(kind, [])
                ]
                properties = _FakeProperties(items)
                runtime.properties.append(properties)
                return properties

        self.encoders: list[_FakeEncoder] = []
        self.properties: list[_FakeProperties] = []
        self.ob = types.SimpleNamespace(
            Scene=_SceneNS,
            Source=_SourceNS,
            Transition=_TransitionNS,
            Output=_OutputNS,
            VideoEncoder=_VideoEncoderNS,
            AudioEncoder=_AudioEncoderNS,
            Properties=_PropertiesNS,
            enum_output_types=lambda: list(runtime.output_types),
            enum_encoder_types=lambda: list(runtime.encoder_types),
            enum_source_types=_enum_source_types,
            BoundsType=types.SimpleNamespace(SCALE_INNER=2, SCALE_OUTER=3, STRETCH=1),
            Alignment=types.SimpleNamespace(LEFT=1, TOP=4),
        )

    @property
    def context(self):
        return types.SimpleNamespace(
            get_video=lambda: "main-video",
            get_audio=lambda: "main-audio",
        )

    # obs_runtime boot contract
    def ensure_started(self, **_kwargs) -> None:
        self.starts += 1

    def shutdown(self) -> None:
        self.shutdowns += 1

    # media source monitoring (best-effort; LibobsMediaSource.open calls this)
    def set_source_monitoring(self, source, monitoring_type: int) -> None:
        self.monitored = getattr(self, "monitored", [])
        self.monitored.append((source, monitoring_type))

    # shared camera source (runtime-owned)
    def camera_source(self, device_id: str, name: str = "", *,
                      pixel_format: str = "", width: int = 0, height: int = 0):
        self.camera_sources.append((device_id, name))
        return types.SimpleNamespace(kind="camera", device_id=device_id, name=name)

    # shared IP camera source (runtime-owned) — one per camera, not per layer
    def rtsp_source(self, camera_id: str, uri: str, settings: dict):
        self.rtsp_requests.append((camera_id, uri))
        existing = self.rtsp_sources.get(camera_id)
        if existing is not None:
            existing.settings.update(settings)
            return existing
        source = _FakeColorSource("ffmpeg_source", f"shared-rtsp-{camera_id}", settings)
        self.rtsp_sources[camera_id] = source
        self.sources.append(source)
        return source

    # channel routing
    def acquire_channel(self) -> int:
        channel = self._next_channel
        self._next_channel += 1
        return channel

    def set_channel_source(self, channel: int, source) -> None:
        if source is not None and self.fail_set_channel_once:
            self.fail_set_channel_once = False
            raise RuntimeError("set_channel_source failed")
        if source is None:
            self.channels.pop(channel, None)
        else:
            self.channels[channel] = source

    def release_channel(self, channel: int) -> None:
        self.released_channels.append(channel)
        self.channels.pop(channel, None)


_DOCUMENT = {
    "scenes": [
        {
            "id": "scene-a",
            "layers": [
                {"id": "L1", "source_id": "content", "visible": True,
                 "rect": {"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0}},
                {"id": "L2", "source_id": "cam", "visible": True,
                 "rect": {"x": 0.5, "y": 0.5, "width": 0.5, "height": 0.5}},
                {"id": "L3", "source_id": "hidden", "visible": False,
                 "rect": {"x": 0.0, "y": 0.0, "width": 0.2, "height": 0.2}},
            ],
        },
        {"id": "scene-b", "layers": []},
    ],
}


def test_scene_graph_builds_scenes_and_positions_visible_layers():
    from solin.core.scenes.libobs_scene_builder import LibobsSceneGraph

    runtime = _CompositingRuntime()
    graph = LibobsSceneGraph(runtime)

    graph.hydrate(_DOCUMENT, {"virtual_camera": "scene-a"})

    assert len(runtime.scenes) == 2
    scene_a = next(s for s in runtime.scenes if s.name == "solin-scene-scene-a")
    # The hidden layer (L3) is skipped; only the two visible layers are added.
    assert len(scene_a.items) == 2
    # Full-canvas layer: placed at the origin, scaled to fill the canvas.
    assert scene_a.items[0].pos == (0.0, 0.0)
    assert scene_a.items[0].bounds == (1920.0, 1080.0)
    assert scene_a.items[0].bounds_type == 2  # SCALE_INNER: the default fit
    assert scene_a.items[0].bounds_alignment == 0  # centred in its rect
    assert scene_a.items[0].source.settings["width"] == 1920
    # Bottom-right quadrant PiP: positioned + bounded to that quadrant.
    assert scene_a.items[1].pos == (960.0, 540.0)
    assert scene_a.items[1].bounds == (960.0, 540.0)
    # A transition sits on the acquired channel, holding the program scene.
    transition = runtime.channels[0]
    assert transition in runtime.transitions
    assert transition.current_source == "scene-source:solin-scene-scene-a"


def test_scene_graph_without_active_program_routes_nothing():
    from solin.core.scenes.libobs_scene_builder import LibobsSceneGraph

    runtime = _CompositingRuntime()
    LibobsSceneGraph(runtime).hydrate(_DOCUMENT, {})

    assert runtime.channels == {}


def test_scene_graph_rehydrate_releases_the_previous_graph():
    from solin.core.scenes.libobs_scene_builder import LibobsSceneGraph

    runtime = _CompositingRuntime()
    graph = LibobsSceneGraph(runtime)
    graph.hydrate(_DOCUMENT, {"virtual_camera": "scene-a"})
    first_scene = runtime.scenes[0]
    first_sources = list(runtime.sources)

    graph.hydrate(_DOCUMENT, {"virtual_camera": "scene-a"})

    assert first_scene.released == 1
    assert all(source.released == 1 for source in first_sources)

    graph.shutdown()
    assert runtime.released_channels == [0]


def test_engine_hydrate_builds_and_acks():
    runtime = _CompositingRuntime()
    engine = LibobsSidecarEngine(runtime_factory=lambda: runtime)
    engine.handle(_request("hello"))  # boots the runtime + creates the graph

    response = engine.handle(
        _request("hydrate", {"document": _DOCUMENT, "active_scenes": {"virtual_camera": "scene-a"}})
    )

    assert response is not None and response.message_type == "ack"
    assert response.payload["applied"] is True
    assert len(runtime.scenes) == 2
    assert runtime.channels[0].current_source == "scene-source:solin-scene-scene-a"


def test_engine_hydrate_without_runtime_acks_not_applied():
    engine = LibobsSidecarEngine()  # no runtime factory → no libobs
    response = engine.handle(
        _request("hydrate", {"document": _DOCUMENT, "active_scenes": {}})
    )
    assert response is not None and response.message_type == "ack"
    assert response.payload["applied"] is False
    assert response.payload["error_code"] == "runtime_unavailable"


def test_hello_advertises_hardware_compositing_only_when_booted():
    runtime = _CompositingRuntime()
    booted = LibobsSidecarEngine(runtime_factory=lambda: runtime)
    caps = _capabilities_from_envelope(booted.handle(_request("hello")))
    assert caps.hardware_compositing is True

    unbooted = LibobsSidecarEngine()  # no factory
    caps = _capabilities_from_envelope(unbooted.handle(_request("hello")))
    assert caps.hardware_compositing is False


# ── set_window_targets → obs_display binding (fake display factory) ───────────


class _FakeDisplay:
    def __init__(self, handle: int, width: int, height: int, background: int) -> None:
        self.handle = handle
        self.size = (width, height)
        self.background = background
        self.draw_callbacks: list = []
        self.resizes: list[tuple[int, int]] = []
        self.released = 0

    def add_draw_callback(self, fn) -> None:
        self.draw_callbacks.append(fn)

    def resize(self, width: int, height: int) -> None:
        self.resizes.append((width, height))
        self.size = (width, height)

    def release(self) -> None:
        self.released += 1


def _window_target(handle: int, *, width=1280, height=720, dpr=1.0, visible=True,
                   bus_id="virtual_camera") -> dict:
    return {
        "bus_id": bus_id,
        "target_id": f"t{handle}",
        "screen_id": f"s{handle}",
        "native_handle": handle,
        "x": 0,
        "y": 0,
        "width": width,
        "height": height,
        "device_pixel_ratio": dpr,
        "visible": visible,
    }


def _window_output_with_recorder():
    from solin.core.scenes.libobs_window_output import LibobsWindowOutput

    created: list[_FakeDisplay] = []

    def factory(handle, width, height, background):
        display = _FakeDisplay(handle, width, height, background)
        created.append(display)
        return display

    return LibobsWindowOutput(_CompositingRuntime(), display_factory=factory), created


def test_window_output_creates_a_display_per_visible_target_with_physical_size():
    output, created = _window_output_with_recorder()

    output.set_targets([_window_target(101, width=1280, height=720, dpr=1.5)])

    assert len(created) == 1
    display = created[0]
    assert display.handle == 101
    assert display.size == (1920, 1080)  # logical size * dpr
    assert len(display.draw_callbacks) == 1  # renders the main texture
    assert output.handles == (101,)


def test_window_output_skips_invisible_targets():
    output, created = _window_output_with_recorder()
    output.set_targets([_window_target(101, visible=False)])
    assert created == []
    assert output.handles == ()


def test_window_output_releases_targets_that_disappear():
    output, created = _window_output_with_recorder()
    output.set_targets([_window_target(101), _window_target(202)])
    output.set_targets([_window_target(202)])  # 101 gone

    display_101 = next(d for d in created if d.handle == 101)
    assert display_101.released == 1
    assert output.handles == (202,)


def test_window_output_resizes_an_existing_display():
    output, created = _window_output_with_recorder()
    output.set_targets([_window_target(101, width=1280, height=720)])
    output.set_targets([_window_target(101, width=1920, height=1080)])

    assert len(created) == 1  # reused, not recreated
    assert created[0].resizes == [(1920, 1080)]


def test_window_output_shutdown_releases_all():
    output, created = _window_output_with_recorder()
    output.set_targets([_window_target(101), _window_target(202)])
    output.shutdown()
    assert all(d.released == 1 for d in created)
    assert output.handles == ()


def test_window_output_draws_main_texture_for_a_program_window(monkeypatch):
    calls: list = []
    monkeypatch.setattr("pylibobs.display.render_main_texture_letterboxed",
                        lambda cw, ch, ww, wh: calls.append("main"), raising=False)
    output, created = _window_output_with_recorder()
    output.set_targets([_window_target(1)])  # program bus, no scene → main mix
    created[0].draw_callbacks[0](640, 360)
    assert calls == ["main"]


def test_window_output_draws_the_projection_route_not_the_program(monkeypatch):
    # A projection window shows the projection transition, which is independent of
    # the program that feeds the virtual camera and the recording.
    calls: list = []
    monkeypatch.setattr("pylibobs.display.render_source_letterboxed",
                        lambda ptr, cw, ch, ww, wh: calls.append(("source", ptr)), raising=False)
    monkeypatch.setattr("pylibobs.display.render_main_texture_letterboxed",
                        lambda cw, ch, ww, wh: calls.append("main"), raising=False)
    output, created = _window_output_with_recorder()
    output.set_projection_resolver(lambda: "PROJECTION-PTR")
    output.set_targets([_window_target(1, bus_id="media_windows")])
    created[0].draw_callbacks[0](640, 360)
    assert calls == [("source", "PROJECTION-PTR")]


def test_window_output_draws_a_specific_scene_when_scene_id_is_set(monkeypatch):
    calls: list = []
    monkeypatch.setattr("pylibobs.display.render_source_letterboxed",
                        lambda ptr, cw, ch, ww, wh: calls.append(("source", ptr)), raising=False)
    monkeypatch.setattr("pylibobs.display.render_main_texture_letterboxed",
                        lambda cw, ch, ww, wh: calls.append("main"), raising=False)
    output, created = _window_output_with_recorder()
    output.set_scene_resolver(
        lambda sid: types.SimpleNamespace(_ptr="PTR-%s" % sid) if sid == "edit" else None)
    target = _window_target(1)
    target["scene_id"] = "edit"
    output.set_targets([target])
    created[0].draw_callbacks[0](640, 360)
    assert calls == [("source", "PTR-edit")]  # the selected scene, not the main mix


def test_window_output_scene_change_needs_no_new_display(monkeypatch):
    resolved: list = []
    monkeypatch.setattr("pylibobs.display.render_source_letterboxed",
                        lambda ptr, cw, ch, ww, wh: resolved.append(ptr), raising=False)
    output, created = _window_output_with_recorder()
    output.set_scene_resolver(lambda sid: types.SimpleNamespace(_ptr=sid))
    first = _window_target(1); first["scene_id"] = "a"
    output.set_targets([first])
    second = _window_target(1); second["scene_id"] = "b"
    output.set_targets([second])  # same handle+size, only the scene changed
    assert len(created) == 1  # display reused, not recreated
    created[0].draw_callbacks[0](640, 360)
    assert resolved == ["b"]  # renders the newly-selected scene live


def test_window_output_draw_never_blocks_on_the_reconcile_lock(monkeypatch):
    """Regression for the editing-crash deadlock.

    The draw callback runs on libobs' graphics thread while it holds the graphics
    mutex; the reconcile/hydrate thread holds ``self._lock`` while creating or
    destroying a display (which needs that same graphics mutex). If the draw
    callback *blocked* on ``self._lock`` the two would deadlock (AB–BA) the moment
    an edit changed a window handle or re-hydrated — freezing the whole engine.
    So the callback must take the lock non-blockingly and, when it can't, fall
    back to the always-valid main texture instead of blocking or touching a
    borrowed scene source it isn't holding the lock for.
    """
    import threading

    calls: list = []
    monkeypatch.setattr("pylibobs.display.render_source_letterboxed",
                        lambda ptr, cw, ch, ww, wh: calls.append("source"), raising=False)
    monkeypatch.setattr("pylibobs.display.render_main_texture_letterboxed",
                        lambda cw, ch, ww, wh: calls.append("main"), raising=False)
    output, created = _window_output_with_recorder()
    output.set_scene_resolver(lambda sid: types.SimpleNamespace(_ptr="P"))
    target = _window_target(1); target["scene_id"] = "edit"
    output.set_targets([target])
    draw = created[0].draw_callbacks[0]

    held = threading.Event()
    release = threading.Event()

    def _hold_lock() -> None:
        with output.hydrate_lock:  # simulate a reconcile/hydrate in flight
            held.set()
            release.wait(2.0)

    holder = threading.Thread(target=_hold_lock)
    holder.start()
    assert held.wait(2.0)

    # Draw while another thread holds the lock: must return promptly (no deadlock)
    # and must render NOTHING. Falling back to the main texture here would flash
    # the program — camera and all — onto a screen showing something else.
    finished = threading.Event()
    threading.Thread(target=lambda: (draw(640, 360), finished.set())).start()
    assert finished.wait(2.0), "draw callback blocked on the held lock (deadlock)"
    assert calls == []

    release.set()
    holder.join(2.0)
    # Once the lock is free again the callback renders the selected scene as usual.
    calls.clear()
    draw(640, 360)
    assert calls == ["source"]


class _RecordingWindowOutput:
    def __init__(self) -> None:
        import threading

        self.calls: list[list] = []
        self.shutdowns = 0
        self.hydrate_lock = threading.RLock()  # the sidecar guards rebuilds with it

    def set_targets(self, targets) -> None:
        self.calls.append(list(targets))

    def shutdown(self) -> None:
        self.shutdowns += 1


def test_engine_set_window_targets_routes_to_the_window_output_and_acks():
    runtime = _CompositingRuntime()
    engine = LibobsSidecarEngine(runtime_factory=lambda: runtime)
    engine.handle(_request("hello"))  # boot → creates the runtime-backed output
    # Swap in a recorder so the routing is tested without touching real libobs
    # displays (LibobsWindowOutput itself is unit-tested above).
    recorder = _RecordingWindowOutput()
    engine._window_output = recorder

    response = engine.handle(
        _request("set_window_targets", {"window_targets": [_window_target(101)]})
    )

    assert response is not None and response.message_type == "ack"
    assert response.payload["applied"] is True
    assert recorder.calls == [[_window_target(101)]]


def test_engine_set_window_targets_without_runtime_acks_not_applied():
    engine = LibobsSidecarEngine()
    response = engine.handle(_request("set_window_targets", {"window_targets": []}))
    assert response is not None and response.message_type == "ack"
    assert response.payload["applied"] is False
    assert response.payload["error_code"] == "runtime_unavailable"


# ── camera / color / rtsp sources from the document ──────────────────────────


def test_camera_layer_uses_the_shared_runtime_source_and_is_not_released():
    from solin.core.scenes.libobs_scene_builder import LibobsSceneGraph

    runtime = _CompositingRuntime()
    graph = LibobsSceneGraph(runtime)
    document = {
        "sources": [
            {"id": "cam1", "type": "local_camera", "name": "Logitech",
             "configuration": {"device_id": "/dev/video0"}}
        ],
        "scenes": [
            {"id": "s", "layers": [
                {"id": "L", "source_id": "cam1", "visible": True,
                 "rect": {"x": 0.25, "y": 0.25, "width": 0.5, "height": 0.5}}]}
        ],
    }

    graph.hydrate(document, {"virtual_camera": "s"})

    # Resolved through the runtime's shared camera cache (not a placeholder).
    assert runtime.camera_sources == [("/dev/video0", "Logitech")]
    assert runtime.sources == []  # no color placeholder created for it

    graph.clear()  # must not try to release the runtime-owned camera source


def test_color_source_uses_the_configured_color():
    from solin.core.scenes.libobs_scene_builder import LibobsSceneGraph

    runtime = _CompositingRuntime()
    graph = LibobsSceneGraph(runtime)
    document = {
        "sources": [
            {"id": "c1", "type": "color", "name": "Red", "configuration": {"color": "#FF0000"}}
        ],
        "scenes": [
            {"id": "s", "layers": [
                {"id": "L", "source_id": "c1", "visible": True,
                 "rect": {"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0}}]}
        ],
    }

    graph.hydrate(document, {"virtual_camera": "s"})

    source = runtime.sources[-1]
    assert source.kind == "color_source_v3"
    assert source.settings["color"] == 0xFF0000FF  # 0xAABBGGRR: red in the low byte

    graph.clear()
    assert source.released == 1  # owned → released


def test_rtsp_layer_creates_an_ffmpeg_source():
    from solin.core.scenes.libobs_scene_builder import LibobsSceneGraph

    runtime = _CompositingRuntime()
    graph = LibobsSceneGraph(runtime)
    document = {
        "sources": [
            {"id": "r1", "type": "rtsp_camera", "name": "Cam",
             "configuration": {"uri": "rtsp://host/stream"}}
        ],
        "scenes": [
            {"id": "s", "layers": [
                {"id": "L", "source_id": "r1", "visible": True,
                 "rect": {"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0}}]}
        ],
    }

    graph.hydrate(document, {"virtual_camera": "s"})

    source = runtime.sources[-1]
    assert source.kind == "ffmpeg_source"
    assert source.settings["is_local_file"] is False
    assert source.settings["input"] == "rtsp://host/stream"


# ── year-text source (rendered PNG shown as an image source) ─────────────────


_YEARTEXT_DOC = {
    "sources": [
        {"id": "yt", "type": "yeartext", "name": "Year text", "configuration": {}},
    ],
    "scenes": [
        {"id": "default", "layers": [
            {"id": "l1", "source_id": "yt", "visible": True,
             "rect": {"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0}},
        ]},
    ],
}


def test_yeartext_layer_builds_an_image_source_from_the_rendered_png(tmp_path, monkeypatch):
    from solin.core.scenes.libobs_scene_builder import LibobsSceneGraph

    png = tmp_path / "yeartext.png"
    png.write_bytes(b"not-a-real-png")  # the fake never decodes it
    monkeypatch.setenv("SOLIN_YEARTEXT_IMAGE", str(png))
    runtime = _CompositingRuntime()
    graph = LibobsSceneGraph(runtime)
    graph.hydrate(_YEARTEXT_DOC, {"virtual_camera": "default"})

    image_sources = [s for s in runtime.sources if s.kind == "image_source"]
    assert len(image_sources) == 1
    assert image_sources[0].settings["file"] == str(png)


def test_yeartext_reload_rereads_the_png_in_place(tmp_path, monkeypatch):
    from solin.core.scenes.libobs_scene_builder import LibobsSceneGraph

    png = tmp_path / "yeartext.png"
    png.write_bytes(b"v1")
    monkeypatch.setenv("SOLIN_YEARTEXT_IMAGE", str(png))
    runtime = _CompositingRuntime()
    graph = LibobsSceneGraph(runtime)
    graph.hydrate(_YEARTEXT_DOC, {"virtual_camera": "default"})

    assert graph.reload_yeartext() is True
    image_source = next(s for s in runtime.sources if s.kind == "image_source")
    assert image_source.settings["file"] == str(png)  # re-applied the file


def test_yeartext_layer_without_a_rendered_png_falls_back_to_placeholder(monkeypatch):
    from solin.core.scenes.libobs_scene_builder import LibobsSceneGraph

    monkeypatch.delenv("SOLIN_YEARTEXT_IMAGE", raising=False)
    runtime = _CompositingRuntime()
    graph = LibobsSceneGraph(runtime)
    graph.hydrate(_YEARTEXT_DOC, {"virtual_camera": "default"})

    # No PNG yet → no image source; the layer takes a colour placeholder instead.
    assert not [s for s in runtime.sources if s.kind == "image_source"]
    assert graph.reload_yeartext() is False  # nothing to refresh


def test_engine_reload_yeartext_notification_rereads_the_source(tmp_path, monkeypatch):
    png = tmp_path / "yeartext.png"
    png.write_bytes(b"v1")
    monkeypatch.setenv("SOLIN_YEARTEXT_IMAGE", str(png))
    runtime = _CompositingRuntime()
    engine = LibobsSidecarEngine(runtime_factory=lambda: runtime)
    engine.handle(_request("hello"))
    engine.handle(_request("hydrate", {
        "document": _YEARTEXT_DOC,
        "active_scenes": {"virtual_camera": "default"},
    }))
    image_source = next(s for s in runtime.sources if s.kind == "image_source")
    image_source.settings["file"] = "STALE"  # simulate the PNG being re-rendered

    result = engine.handle(_request("reload_yeartext", {}))

    assert result is None  # a fire-and-forget notification — no response
    assert image_source.settings["file"] == str(png)  # re-read in place


# ── live layer geometry (preview_layer_geometry, no re-hydrate) ──────────────


_GEOMETRY_DOC = {
    "scenes": [
        {"id": "s1", "layers": [
            {"id": "L1", "source_id": "", "visible": True,
             "rect": {"x": 0.0, "y": 0.0, "width": 0.5, "height": 0.5}},
        ]},
    ],
}


def test_apply_layer_geometry_updates_the_live_item_without_rebuild():
    from solin.core.scenes.libobs_scene_builder import LibobsSceneGraph

    runtime = _CompositingRuntime()
    graph = LibobsSceneGraph(runtime)
    graph.hydrate(_GEOMETRY_DOC, {"virtual_camera": "s1"})
    scene = next(s for s in runtime.scenes if s.name == "solin-scene-s1")
    item = scene.items[0]
    before = (item.pos, item.bounds)

    assert graph.apply_layer_geometry(
        "s1", "L1", {"x": 0.25, "y": 0.25, "width": 1.0, "height": 1.0}
    ) is True
    assert (item.pos, item.bounds) != before  # transformed in place
    assert scene.items[0] is item  # same item — not rebuilt

    assert graph.apply_layer_geometry("s1", "missing", {}) is False


def test_engine_preview_layer_geometry_applies_live_and_acks():
    runtime = _CompositingRuntime()
    engine = LibobsSidecarEngine(runtime_factory=lambda: runtime)
    engine.handle(_request("hello"))
    engine.handle(_request("hydrate", {
        "document": _GEOMETRY_DOC, "active_scenes": {"virtual_camera": "s1"},
    }))
    scene = next(s for s in runtime.scenes if s.name == "solin-scene-s1")
    item = scene.items[0]
    before = item.pos

    ack = _ack_from_envelope(engine.handle(_request("preview_layer_geometry", {
        "scene_id": "s1",
        "layer": {"id": "L1", "source_id": "",
                  "rect": {"x": 0.5, "y": 0.5, "width": 0.4, "height": 0.4}},
    })))
    assert ack.applied is True
    assert item.pos != before  # applied live, no new item
    assert scene.items[0] is item


def test_engine_preview_layer_geometry_rejects_an_unbuilt_layer():
    runtime = _CompositingRuntime()
    engine = LibobsSidecarEngine(runtime_factory=lambda: runtime)
    engine.handle(_request("hello"))
    engine.handle(_request("hydrate", {
        "document": _GEOMETRY_DOC, "active_scenes": {"virtual_camera": "s1"},
    }))
    ack = _ack_from_envelope(engine.handle(_request("preview_layer_geometry", {
        "scene_id": "s1", "layer": {"id": "missing", "rect": {}},
    })))
    assert ack.applied is False and ack.error_code == "unknown_layer"


# ── transitions (prepare_scene / take_prepared) ──────────────────────────────


_TWO_SCENE_DOC = {"scenes": [{"id": "a", "layers": []}, {"id": "b", "layers": []}]}


def _graph_on_scene_a():
    from solin.core.scenes.libobs_scene_builder import LibobsSceneGraph

    runtime = _CompositingRuntime()
    graph = LibobsSceneGraph(runtime)
    graph.hydrate(_TWO_SCENE_DOC, {"virtual_camera": "a"})
    return runtime, graph


def test_prepare_and_take_dissolve_swaps_transition_and_animates():
    runtime, graph = _graph_on_scene_a()
    assert runtime.channels[0].current_source == "scene-source:solin-scene-a"

    result = graph.prepare("b", "dissolve", 500)
    assert result is not None and result["fallback_applied"] is False
    assert graph.take(result["token"]) is True

    transition = runtime.channels[0]  # dissolve != cut → swapped
    assert transition.kind == "fade_transition"
    assert transition.starts[-1] == ("scene-source:solin-scene-b", 500)


def test_cut_take_reuses_the_cut_transition():
    runtime, graph = _graph_on_scene_a()
    result = graph.prepare("b", "cut", 0)
    assert graph.take(result["token"]) is True
    transition = runtime.channels[0]
    assert transition.kind == "cut_transition"  # same kind → not swapped
    assert transition.starts[-1] == ("scene-source:solin-scene-b", 0)


def test_prepare_unknown_scene_returns_none():
    _runtime, graph = _graph_on_scene_a()
    assert graph.prepare("missing", "cut", 0) is None


def test_prepare_unknown_kind_falls_back_to_cut():
    _runtime, graph = _graph_on_scene_a()
    result = graph.prepare("b", "sparkle", 0)
    assert result is not None
    assert result["fallback_applied"] is True
    assert result["kind"] == "cut"


def test_take_unknown_token_returns_false():
    _runtime, graph = _graph_on_scene_a()
    assert graph.take("prep-999") is False


def test_discard_drops_pending_without_moving_the_program():
    runtime, graph = _graph_on_scene_a()
    assert graph._active_scene_id == "a"
    result = graph.prepare("b", "cut", 0)
    assert graph.discard(result["token"]) is True
    # the program channel is untouched — discard does not animate a take
    assert graph._active_scene_id == "a"
    assert runtime.channels[0].current_source == "scene-source:solin-scene-a"
    # the token is consumed, so a later take of it is a no-op
    assert graph.take(result["token"]) is False


def test_discard_unknown_token_returns_false():
    _runtime, graph = _graph_on_scene_a()
    assert graph.discard("prep-999") is False


def test_cancel_all_drops_pending_preparations():
    _runtime, graph = _graph_on_scene_a()
    result = graph.prepare("b", "cut", 0)
    graph.cancel_all()
    assert graph.take(result["token"]) is False


def test_engine_prepare_then_take_scene():
    runtime = _CompositingRuntime()
    engine = LibobsSidecarEngine(runtime_factory=lambda: runtime)
    engine.handle(_request("hello"))
    engine.handle(_request("hydrate", {"document": _TWO_SCENE_DOC,
                                       "active_scenes": {"virtual_camera": "a"}}))

    prepared = engine.handle(_request("prepare_scene", {
        "bus_id": "virtual_camera", "scene_id": "b",
        "transition": {"kind": "dissolve", "duration_ms": 300},
        "content_media_epoch": None,
    }))
    assert prepared is not None and prepared.message_type == "scene_prepared"
    assert prepared.payload["fallback_applied"] is False
    token = prepared.payload["preparation_token"]

    taken = engine.handle(_request("take_prepared", {
        "bus_id": "virtual_camera", "scene_id": "b", "preparation_token": token,
    }))
    assert taken is not None and taken.message_type == "ack"
    assert taken.payload["applied"] is True
    assert runtime.channels[0].starts[-1] == ("scene-source:solin-scene-b", 300)


def _engine_hydrated_on_a_with_fake_preview():
    """Engine hydrated with both buses on scene 'a' and a recording preview egress."""
    runtime = _CompositingRuntime()
    engine = LibobsSidecarEngine(runtime_factory=lambda: runtime)
    engine.handle(_request("hello"))
    fake = _FakePreviewEgress()
    engine._preview_egress = fake
    engine.handle(_request("hydrate", {
        "document": _TWO_SCENE_DOC,
        "active_scenes": {"media_windows": "a", "virtual_camera": "a"},
    }))
    return runtime, engine, fake


def test_engine_editor_take_repoints_preview_without_moving_program():
    # Selecting a scene in the editor takes the EDITOR channel. That must move the
    # editor preview to the new scene while leaving the program (and thus the
    # virtual camera) on its own scene. The editor has its own channel precisely so
    # it can never move a delivery output.
    runtime, engine, fake = _engine_hydrated_on_a_with_fake_preview()
    assert engine._scene_graph._active_scene_id == "a"

    prepared = engine.handle(_request("prepare_scene", {
        "bus_id": "editor", "scene_id": "b",
        "transition": {"kind": "cut", "duration_ms": 0},
    }))
    token = prepared.payload["preparation_token"]
    taken = engine.handle(_request("take_prepared", {
        "bus_id": "editor", "scene_id": "b", "preparation_token": token,
    }))
    assert _ack_from_envelope(taken).applied is True
    # the editor preview now renders scene b …
    assert fake.sources[-1] == "scene-source:solin-scene-b"
    # … but the program channel is untouched (still scene a) and no take animated
    assert engine._scene_graph._active_scene_id == "a"
    assert runtime.channels[0].current_source == "scene-source:solin-scene-a"
    assert token not in engine._scene_graph._pending


def test_engine_program_take_moves_program_and_leaves_preview_untouched():
    runtime, engine, fake = _engine_hydrated_on_a_with_fake_preview()
    fake.sources.clear()

    prepared = engine.handle(_request("prepare_scene", {
        "bus_id": "virtual_camera", "scene_id": "b",
        "transition": {"kind": "cut", "duration_ms": 0},
    }))
    token = prepared.payload["preparation_token"]
    taken = engine.handle(_request("take_prepared", {
        "bus_id": "virtual_camera", "scene_id": "b", "preparation_token": token,
    }))
    assert _ack_from_envelope(taken).applied is True
    # the program moved …
    assert engine._scene_graph._active_scene_id == "b"
    assert runtime.channels[0].starts[-1] == ("scene-source:solin-scene-b", 0)
    # … and a program take does not re-point the editor preview egress
    assert fake.sources == []


def test_engine_media_windows_take_unknown_token_errors_and_keeps_preview():
    _runtime, engine, fake = _engine_hydrated_on_a_with_fake_preview()
    fake.sources.clear()
    ack = _ack_from_envelope(engine.handle(_request("take_prepared", {
        "bus_id": "editor", "scene_id": "b", "preparation_token": "prep-999",
    })))
    assert ack.applied is False and ack.error_code == "unknown_preparation"
    assert fake.sources == []  # nothing re-pointed on an unknown token


def test_engine_prepare_unknown_scene_errors():
    runtime = _CompositingRuntime()
    engine = LibobsSidecarEngine(runtime_factory=lambda: runtime)
    engine.handle(_request("hello"))
    engine.handle(_request("hydrate", {"document": _TWO_SCENE_DOC,
                                       "active_scenes": {"virtual_camera": "a"}}))
    response = engine.handle(_request("prepare_scene", {
        "bus_id": "virtual_camera", "scene_id": "nope",
        "transition": {"kind": "cut", "duration_ms": 0},
    }))
    assert response is not None and response.message_type == "error"
    assert response.payload["error_code"] == "unknown_scene"


def test_engine_cancel_preparation_is_a_silent_notification():
    runtime = _CompositingRuntime()
    engine = LibobsSidecarEngine(runtime_factory=lambda: runtime)
    engine.handle(_request("hello"))
    engine.handle(_request("hydrate", {"document": _TWO_SCENE_DOC,
                                       "active_scenes": {"virtual_camera": "a"}}))
    assert engine.handle(_request("cancel_preparation", {"cancelled_request_id": "x"})) is None


# ── virtual camera (virtual_camera bus output) ───────────────────────────────


def test_virtual_camera_starts_and_stops_the_output(monkeypatch):
    # These assertions describe the v4l2 backend (an obs "virtualcam_output"). On
    # Windows start() dispatches to the DirectShow broker instead and creates no obs
    # output, so pin the platform to keep this covering one backend on every host.
    monkeypatch.setattr(sys, "platform", "linux")
    from solin.core.scenes.libobs_virtual_camera import LibobsVirtualCamera

    runtime = _CompositingRuntime()
    vcam = LibobsVirtualCamera(runtime)

    assert vcam.start() is True
    assert vcam.active is True
    output = runtime.outputs[-1]
    assert output.kind == "virtualcam_output"
    assert output.name == "Solin Virtual Camera"
    assert output.media == ("main-video", "main-audio")  # mirrors the main mix
    assert output.started == 1

    assert vcam.start() is True  # idempotent
    assert output.started == 1

    vcam.stop()
    assert output.stopped == 1 and output.released == 1
    assert vcam.active is False


def test_virtual_camera_unavailable_when_output_kind_missing():
    from solin.core.scenes.libobs_virtual_camera import LibobsVirtualCamera

    runtime = _CompositingRuntime()
    runtime.output_types = []  # e.g. Windows with the bundled plugins only

    vcam = LibobsVirtualCamera(runtime)
    assert vcam.start() is False
    assert vcam.active is False
    assert runtime.outputs == []


def test_engine_set_output_enabled_toggles_the_virtual_camera(monkeypatch):
    # These assertions describe the v4l2 backend (an obs "virtualcam_output"). On
    # Windows start() dispatches to the DirectShow broker instead and creates no obs
    # output, so pin the platform to keep this covering one backend on every host.
    monkeypatch.setattr(sys, "platform", "linux")
    runtime = _CompositingRuntime()
    engine = LibobsSidecarEngine(runtime_factory=lambda: runtime)
    engine.handle(_request("hello"))

    on = engine.handle(_request("set_output_enabled", {"bus_id": "virtual_camera", "enabled": True}))
    assert on is not None and on.message_type == "ack" and on.payload["applied"] is True
    assert runtime.outputs[-1].started == 1

    off = engine.handle(_request("set_output_enabled", {"bus_id": "virtual_camera", "enabled": False}))
    assert off is not None and off.payload["applied"] is True
    assert runtime.outputs[-1].stopped == 1


def test_engine_hydrate_restores_the_virtual_camera_output(monkeypatch):
    # These assertions describe the v4l2 backend (an obs "virtualcam_output"). On
    # Windows start() dispatches to the DirectShow broker instead and creates no obs
    # output, so pin the platform to keep this covering one backend on every host.
    monkeypatch.setattr(sys, "platform", "linux")
    # A restart brings up a fresh sidecar with the vcam stopped, while the app's
    # output cache still reads "enabled" so it never re-sends set_output_enabled.
    # Hydrate carries output_enabled, so it must restart the vcam itself — the
    # meeting's program output comes back on its own after a restart.
    runtime = _CompositingRuntime()
    engine = LibobsSidecarEngine(runtime_factory=lambda: runtime)
    engine.handle(_request("hello"))

    engine.handle(_request("hydrate", {
        "document": _MEDIA_DOC,
        "active_scenes": {"media_windows": "s1", "virtual_camera": "s1"},
        "output_enabled": {"virtual_camera": True},
    }))
    assert runtime.outputs[-1].started == 1

    # Idempotent: a structural-edit re-hydrate with the vcam already on must not
    # restart it (no flicker mid-meeting).
    before = len(runtime.outputs)
    engine.handle(_request("hydrate", {
        "document": _MEDIA_DOC,
        "active_scenes": {"media_windows": "s1", "virtual_camera": "s1"},
        "output_enabled": {"virtual_camera": True},
    }))
    assert len(runtime.outputs) == before  # no second output created
    assert runtime.outputs[-1].started == 1


def test_engine_set_output_enabled_other_bus_is_a_noop_ack():
    runtime = _CompositingRuntime()
    engine = LibobsSidecarEngine(runtime_factory=lambda: runtime)
    engine.handle(_request("hello"))

    response = engine.handle(_request("set_output_enabled", {"bus_id": "media_windows", "enabled": True}))
    assert response is not None and response.payload["applied"] is True
    assert runtime.outputs == []  # media_windows doesn't create a vcam output


def test_hello_advertises_virtual_camera_when_booted():
    runtime = _CompositingRuntime()
    booted = LibobsSidecarEngine(runtime_factory=lambda: runtime)
    caps = _capabilities_from_envelope(booted.handle(_request("hello")))
    assert caps.virtual_camera is True

    caps = _capabilities_from_envelope(LibobsSidecarEngine().handle(_request("hello")))
    assert caps.virtual_camera is False


# ── program recording: libobs ffmpeg_muxer + encoders ────────────────────────


def test_recorder_start_creates_muxer_and_encoders_then_stops():
    from solin.core.scenes.libobs_recorder import LibobsRecorder

    runtime = _CompositingRuntime()
    recorder = LibobsRecorder(runtime)

    assert recorder.start(_recording_path("solin-test.mp4")) is True
    assert recorder.active is True and recorder.path == _recording_path("solin-test.mp4")
    output = runtime.outputs[-1]
    assert output.kind == "mp4_output"  # the in-process muxer is preferred
    assert output.settings["path"] == _recording_path("solin-test.mp4")
    assert output.video_encoder is not None and output.audio_encoder is not None
    assert output.started == 1
    # a video + an audio encoder were attached to the global mix
    assert len(runtime.encoders) == 2

    recorder.stop()
    assert output.stopped == 1 and output.released == 1
    assert recorder.active is False and recorder.path == ""
    assert all(encoder.released == 1 for encoder in runtime.encoders)


def test_recorder_double_start_is_rejected():
    from solin.core.scenes.libobs_recorder import LibobsRecorder

    runtime = _CompositingRuntime()
    recorder = LibobsRecorder(runtime)
    assert recorder.start(_recording_path("a.mp4")) is True
    assert recorder.start(_recording_path("b.mp4")) is False  # already recording
    assert len(runtime.outputs) == 1  # no second output created


def test_recorder_start_failure_releases_everything():
    from solin.core.scenes.libobs_recorder import LibobsRecorder

    runtime = _CompositingRuntime()
    runtime.output_start_ok = False  # output.start() returns False
    recorder = LibobsRecorder(runtime)

    assert recorder.start(_recording_path("nope.mp4")) is False
    assert recorder.active is False
    # the output and both encoders were released on the failed path
    assert runtime.outputs[-1].released == 1
    assert all(encoder.released == 1 for encoder in runtime.encoders)


def test_recorder_unavailable_when_muxer_missing():
    from solin.core.scenes.libobs_recorder import LibobsRecorder

    runtime = _CompositingRuntime()
    runtime.output_types = ["virtualcam_output"]  # no ffmpeg_muxer
    recorder = LibobsRecorder(runtime)

    assert recorder.start(_recording_path("x.mp4")) is False
    assert runtime.outputs == [] and runtime.encoders == []


def test_recorder_prefers_a_hardware_encoder_when_available():
    from solin.core.scenes.libobs_recorder import LibobsRecorder

    runtime = _CompositingRuntime()
    runtime.encoder_types = ["obs_x264", "obs_qsv11", "obs_nvenc_h264_tex"]
    recorder = LibobsRecorder(runtime)

    assert recorder.start(_recording_path("hw.mp4")) is True
    # NVENC outranks QSV outranks x264, so the NVENC encoder is chosen.
    assert runtime.outputs[-1].video_encoder.kind == "obs_nvenc_h264_tex"


def test_recorder_falls_back_to_software_when_hardware_fails_to_start():
    from solin.core.scenes.libobs_recorder import LibobsRecorder

    runtime = _CompositingRuntime()
    runtime.encoder_types = ["obs_x264", "obs_nvenc_h264_tex"]
    runtime.failing_video_encoders = {"obs_nvenc_h264_tex"}  # creates, won't start
    recorder = LibobsRecorder(runtime)

    assert recorder.start(_recording_path("fallback.mp4")) is True
    assert recorder.active is True
    # Two outputs were attempted: the NVENC one failed+released, x264 records.
    assert len(runtime.outputs) == 2
    assert runtime.outputs[0].video_encoder.kind == "obs_nvenc_h264_tex"
    assert runtime.outputs[0].released == 1
    assert runtime.outputs[1].video_encoder.kind == "obs_x264"
    assert recorder._video_encoder.kind == "obs_x264"


def test_recorder_falls_back_when_hardware_encoder_cannot_be_created():
    from solin.core.scenes.libobs_recorder import LibobsRecorder

    runtime = _CompositingRuntime()
    runtime.encoder_types = ["obs_x264", "obs_nvenc_h264_tex"]
    runtime.fail_encoder_kinds = {"obs_nvenc_h264_tex"}  # create() raises
    recorder = LibobsRecorder(runtime)

    assert recorder.start(_recording_path("fallback2.mp4")) is True
    assert recorder._video_encoder.kind == "obs_x264"


def test_recorder_hardware_encode_can_be_disabled_by_env(monkeypatch):
    from solin.core.scenes.libobs_recorder import LibobsRecorder

    monkeypatch.setenv("SOLIN_RECORD_HW_ENCODE", "0")
    runtime = _CompositingRuntime()
    runtime.encoder_types = ["obs_x264", "obs_nvenc_h264_tex"]
    recorder = LibobsRecorder(runtime)

    assert recorder.start(_recording_path("sw.mp4")) is True
    assert recorder._video_encoder.kind == "obs_x264"
    # the hardware encoder was never even created
    assert all(enc.kind != "obs_nvenc_h264_tex" for enc in runtime.encoders)


def test_engine_start_and_stop_recording_acks_and_emits_state():
    runtime = _CompositingRuntime()
    engine = LibobsSidecarEngine(runtime_factory=lambda: runtime)
    events: list[SceneIpcEnvelope] = []
    engine.set_event_sink(events.append)
    engine.handle(_request("hello"))

    start = engine.handle(_request("start_program_recording", {
        "path": _recording_path("rec.mp4"), "width": 1920, "height": 1080,
        "fps_numerator": 30, "fps_denominator": 1,
        "microphone": {}, "system_audio": {},
    }))
    assert _ack_from_envelope(start).applied is True
    assert runtime.outputs[-1].kind == "mp4_output"
    assert runtime.outputs[-1].started == 1
    # the sidecar emitted a "recording" state the client can parse
    recording_event = _program_recording_event_from_envelope(events[-1])
    assert recording_event.state.status is ProgramRecordingStatus.RECORDING
    assert recording_event.state.path == _recording_path("rec.mp4")

    stop = engine.handle(_request("stop_program_recording", {}))
    assert _ack_from_envelope(stop).applied is True
    assert runtime.outputs[-1].stopped == 1
    idle_event = _program_recording_event_from_envelope(events[-1])
    assert idle_event.state.status is ProgramRecordingStatus.IDLE


def test_engine_start_recording_without_a_path_fails():
    runtime = _CompositingRuntime()
    engine = LibobsSidecarEngine(runtime_factory=lambda: runtime)
    engine.handle(_request("hello"))

    response = engine.handle(_request("start_program_recording", {"path": ""}))
    ack = _ack_from_envelope(response)
    assert ack.applied is False and ack.error_code == "invalid_path"
    assert runtime.outputs == []


def test_engine_start_recording_failure_emits_failed_state():
    runtime = _CompositingRuntime()
    runtime.output_start_ok = False
    engine = LibobsSidecarEngine(runtime_factory=lambda: runtime)
    events: list[SceneIpcEnvelope] = []
    engine.set_event_sink(events.append)
    engine.handle(_request("hello"))

    response = engine.handle(_request("start_program_recording", {"path": _recording_path("r.mp4")}))
    assert _ack_from_envelope(response).applied is False
    failed = _program_recording_event_from_envelope(events[-1])
    assert failed.state.status is ProgramRecordingStatus.FAILED
    assert failed.state.error_code == "recording_start_failed"


def test_engine_start_recording_without_runtime_fails():
    engine = LibobsSidecarEngine()  # protocol-only, no runtime
    engine.handle(_request("hello"))
    response = engine.handle(_request("start_program_recording", {"path": _recording_path("r.mp4")}))
    ack = _ack_from_envelope(response)
    assert ack.applied is False and ack.error_code == "runtime_unavailable"


def test_engine_set_program_recording_audio_acks():
    runtime = _CompositingRuntime()
    engine = LibobsSidecarEngine(runtime_factory=lambda: runtime)
    engine.handle(_request("hello"))
    response = engine.handle(_request("set_program_recording_audio", {
        "microphone": {}, "system_audio": {},
    }))
    assert _ack_from_envelope(response).applied is True


def test_engine_list_audio_devices_reports_real_devices():
    runtime = _CompositingRuntime()
    engine = LibobsSidecarEngine(runtime_factory=lambda: runtime)
    engine.handle(_request("hello"))
    discovery = _audio_device_discovery_from_envelope(
        engine.handle(_request("list_audio_devices"))
    )
    assert discovery.supported is True and discovery.ready is True
    ids = {(d.direction.value, d.device_id) for d in discovery.devices}
    # real devices are listed; the synthetic "default" pseudo-entry is dropped
    assert ("input", "usb-mic-1") in ids
    assert ("input", "builtin-mic") in ids
    assert ("output", "speakers-1") in ids
    assert all(d.device_id != "default" for d in discovery.devices)


def test_engine_list_local_cameras_returns_discovered_devices(monkeypatch):
    from solin.core.scenes.engine import (
        LocalCameraDevice,
        LocalCameraProbe,
        LocalCameraProbeStatus,
    )

    fake_devices = [
        LocalCameraDevice(
            device_id="/dev/video1", display_name="Brio 105", software_device=False,
            formats=(),
            probe=LocalCameraProbe(
                status=LocalCameraProbeStatus.UNVERIFIED, backend="v4l2",
                failure_stage="format_probe", error_code="no_formats")),
    ]
    # The handler discovers cameras out of band (real V4L2 scan); patch it so the
    # test is deterministic and does not depend on the host's cameras.
    monkeypatch.setattr(
        "solin.core.scenes.v4l2_camera_discovery.discover_local_cameras",
        lambda: fake_devices,
    )
    runtime = _CompositingRuntime()
    engine = LibobsSidecarEngine(runtime_factory=lambda: runtime)
    engine.handle(_request("hello"))
    discovery = _local_camera_discovery_from_envelope(
        engine.handle(_request("list_local_cameras"))
    )
    assert discovery.supported and discovery.ready and discovery.error_code == ""
    assert [device.device_id for device in discovery.devices] == ["/dev/video1"]


def test_hello_advertises_program_recording_when_booted():
    runtime = _CompositingRuntime()
    booted = LibobsSidecarEngine(runtime_factory=lambda: runtime)
    caps = _capabilities_from_envelope(booted.handle(_request("hello")))
    assert caps.program_recording is True

    caps = _capabilities_from_envelope(LibobsSidecarEngine().handle(_request("hello")))
    assert caps.program_recording is False


def test_shutdown_stops_an_active_recording():
    runtime = _CompositingRuntime()
    engine = LibobsSidecarEngine(runtime_factory=lambda: runtime)
    engine.handle(_request("hello"))
    engine.handle(_request("start_program_recording", {"path": _recording_path("r.mp4")}))
    output = runtime.outputs[-1]

    engine.shutdown()
    assert output.stopped == 1 and output.released == 1


# ── audio device selection: mic + system-audio capture into the main mix ─────


def _audio_sources(runtime, kind: str) -> list:
    return [s for s in runtime.sources if s.kind == kind]


def test_audio_mixer_lists_input_and_output_devices():
    from solin.core.scenes.libobs_audio_sources import LibobsAudioMixer

    runtime = _CompositingRuntime()
    payload = LibobsAudioMixer(runtime).list_devices()

    assert payload["supported"] is True and payload["ready"] is True
    assert payload["generation"] >= 1 and payload["error_code"] == ""
    by_id = {d["device_id"]: d for d in payload["devices"]}
    assert "default" not in by_id  # synthetic entry dropped
    assert by_id["usb-mic-1"]["direction"] == "input"
    assert by_id["usb-mic-1"]["display_name"] == "USB Microphone"
    assert by_id["speakers-1"]["direction"] == "output"
    assert all(d["is_default"] is False for d in payload["devices"])
    # the enumerated Properties objects are released
    assert all(p.released == 1 for p in runtime.properties)


def test_audio_mixer_generation_increments_per_enumeration():
    from solin.core.scenes.libobs_audio_sources import LibobsAudioMixer

    mixer = LibobsAudioMixer(_CompositingRuntime())
    first = mixer.list_devices()["generation"]
    second = mixer.list_devices()["generation"]
    assert second > first


def test_audio_mixer_apply_device_creates_capture_source_on_a_channel():
    from solin.core.scenes.libobs_audio_sources import LibobsAudioMixer

    runtime = _CompositingRuntime()
    mixer = LibobsAudioMixer(runtime)
    mixer.apply({"mode": "device", "device_id": "usb-mic-1"}, {"mode": "none"})

    mics = _audio_sources(runtime, "pulse_input_capture")
    assert len(mics) == 1
    assert mics[0].settings == {"device_id": "usb-mic-1"}
    assert mics[0] in runtime.channels.values()  # routed onto an output channel
    assert _audio_sources(runtime, "pulse_output_capture") == []  # system = none


def test_audio_mixer_apply_system_default_uses_default_device_id():
    from solin.core.scenes.libobs_audio_sources import LibobsAudioMixer

    runtime = _CompositingRuntime()
    mixer = LibobsAudioMixer(runtime)
    mixer.apply({"mode": "system_default", "device_id": ""},
                {"mode": "system_default", "device_id": ""})

    mic = _audio_sources(runtime, "pulse_input_capture")[0]
    system = _audio_sources(runtime, "pulse_output_capture")[0]
    assert mic.settings["device_id"] == "default"
    assert system.settings["device_id"] == "default"
    assert mic in runtime.channels.values() and system in runtime.channels.values()


def test_audio_mixer_apply_none_creates_no_source():
    from solin.core.scenes.libobs_audio_sources import LibobsAudioMixer

    runtime = _CompositingRuntime()
    LibobsAudioMixer(runtime).apply({"mode": "none"}, {"mode": "none"})
    assert _audio_sources(runtime, "pulse_input_capture") == []
    assert _audio_sources(runtime, "pulse_output_capture") == []


def test_audio_mixer_reapply_same_selection_is_stable():
    from solin.core.scenes.libobs_audio_sources import LibobsAudioMixer

    runtime = _CompositingRuntime()
    mixer = LibobsAudioMixer(runtime)
    selection = {"mode": "device", "device_id": "usb-mic-1"}
    mixer.apply(selection, {"mode": "none"})
    mixer.apply(dict(selection), {"mode": "none"})  # identical re-apply

    mics = _audio_sources(runtime, "pulse_input_capture")
    assert len(mics) == 1 and mics[0].released == 0  # no churn


def test_audio_mixer_switching_device_replaces_the_source():
    from solin.core.scenes.libobs_audio_sources import LibobsAudioMixer

    runtime = _CompositingRuntime()
    mixer = LibobsAudioMixer(runtime)
    mixer.apply({"mode": "device", "device_id": "usb-mic-1"}, {"mode": "none"})
    first = _audio_sources(runtime, "pulse_input_capture")[0]
    mixer.apply({"mode": "device", "device_id": "builtin-mic"}, {"mode": "none"})

    mics = _audio_sources(runtime, "pulse_input_capture")
    assert len(mics) == 2
    assert first.released == 1  # old source torn down
    assert mics[1].settings["device_id"] == "builtin-mic"
    assert first not in runtime.channels.values()
    assert mics[1] in runtime.channels.values()


def test_audio_mixer_clear_releases_sources_and_channels():
    from solin.core.scenes.libobs_audio_sources import LibobsAudioMixer

    runtime = _CompositingRuntime()
    mixer = LibobsAudioMixer(runtime)
    mixer.apply({"mode": "system_default"}, {"mode": "system_default"})
    channels_used = [c for c, s in runtime.channels.items()
                     if s in {src for src in runtime.sources if src.kind.startswith("pulse")}]
    mixer.clear()

    assert all(s.released == 1 for s in runtime.sources if s.kind.startswith("pulse"))
    assert all(c in runtime.released_channels for c in channels_used)
    assert not any(s.kind.startswith("pulse") for s in runtime.channels.values())


def test_audio_mixer_unavailable_kind_creates_nothing():
    from solin.core.scenes.libobs_audio_sources import LibobsAudioMixer

    runtime = _CompositingRuntime()
    runtime.source_types = ["color_source_v3", "ffmpeg_source"]  # no capture kinds
    mixer = LibobsAudioMixer(runtime)
    mixer.apply({"mode": "system_default"}, {"mode": "system_default"})

    assert runtime.sources == []
    assert mixer.available() == (False, False)


def test_engine_set_program_recording_audio_applies_selection():
    runtime = _CompositingRuntime()
    engine = LibobsSidecarEngine(runtime_factory=lambda: runtime)
    engine.handle(_request("hello"))

    response = engine.handle(_request("set_program_recording_audio", {
        "microphone": AudioDeviceSelection(
            AudioSelectionMode.DEVICE, "usb-mic-1", "USB Microphone"
        ).to_engine_record(),
        "system_audio": AudioDeviceSelection().to_engine_record(),  # system default
    }))
    assert _ack_from_envelope(response).applied is True
    mic = _audio_sources(runtime, "pulse_input_capture")[0]
    assert mic.settings["device_id"] == "usb-mic-1"
    system = _audio_sources(runtime, "pulse_output_capture")[0]
    assert system.settings["device_id"] == "default"


def test_engine_set_program_recording_audio_without_runtime_fails():
    engine = LibobsSidecarEngine()  # protocol-only, no runtime
    engine.handle(_request("hello"))
    response = engine.handle(_request("set_program_recording_audio", {
        "microphone": {"mode": "system_default", "device_id": ""},
        "system_audio": {"mode": "system_default", "device_id": ""},
    }))
    ack = _ack_from_envelope(response)
    assert ack.applied is False and ack.error_code == "runtime_unavailable"


def test_engine_start_recording_applies_audio_before_start():
    runtime = _CompositingRuntime()
    engine = LibobsSidecarEngine(runtime_factory=lambda: runtime)
    engine.handle(_request("hello"))

    start = engine.handle(_request("start_program_recording", {
        "path": _recording_path("rec.mp4"),
        "microphone": {"mode": "device", "device_id": "usb-mic-1"},
        "system_audio": {"mode": "none"},
    }))
    assert _ack_from_envelope(start).applied is True
    mic = _audio_sources(runtime, "pulse_input_capture")[0]
    assert mic.settings["device_id"] == "usb-mic-1"
    assert mic in runtime.channels.values()
    assert runtime.outputs[-1].started == 1  # recording actually started


def test_engine_stop_recording_clears_audio_sources():
    runtime = _CompositingRuntime()
    engine = LibobsSidecarEngine(runtime_factory=lambda: runtime)
    engine.handle(_request("hello"))
    engine.handle(_request("start_program_recording", {
        "path": _recording_path("rec.mp4"),
        "microphone": {"mode": "device", "device_id": "usb-mic-1"},
        "system_audio": {"mode": "system_default"},
    }))
    engine.handle(_request("stop_program_recording", {}))

    # every capture source is released and off its channel once recording stops
    assert all(s.released == 1 for s in runtime.sources if s.kind.startswith("pulse"))
    assert not any(s.kind.startswith("pulse") for s in runtime.channels.values())


def test_engine_failed_recording_start_clears_audio_sources():
    runtime = _CompositingRuntime()
    runtime.output_start_ok = False  # recorder.start() will fail
    engine = LibobsSidecarEngine(runtime_factory=lambda: runtime)
    engine.handle(_request("hello"))

    response = engine.handle(_request("start_program_recording", {
        "path": _recording_path("rec.mp4"),
        "microphone": {"mode": "device", "device_id": "usb-mic-1"},
        "system_audio": {"mode": "none"},
    }))
    assert _ack_from_envelope(response).applied is False
    # a failed start must not leave the microphone live
    assert all(s.released == 1 for s in runtime.sources if s.kind.startswith("pulse"))
    assert not any(s.kind.startswith("pulse") for s in runtime.channels.values())


def test_hello_advertises_audio_capture_when_kinds_available():
    runtime = _CompositingRuntime()
    caps = _capabilities_from_envelope(
        LibobsSidecarEngine(runtime_factory=lambda: runtime).handle(_request("hello"))
    )
    assert caps.audio_input_capture is True
    assert caps.system_audio_capture is True

    bare = _CompositingRuntime()
    bare.source_types = ["color_source_v3"]  # no capture kinds registered
    caps = _capabilities_from_envelope(
        LibobsSidecarEngine(runtime_factory=lambda: bare).handle(_request("hello"))
    )
    assert caps.audio_input_capture is False
    assert caps.system_audio_capture is False

    caps = _capabilities_from_envelope(LibobsSidecarEngine().handle(_request("hello")))
    assert caps.audio_input_capture is False  # no runtime at all


def test_engine_shutdown_releases_audio_sources():
    runtime = _CompositingRuntime()
    engine = LibobsSidecarEngine(runtime_factory=lambda: runtime)
    engine.handle(_request("hello"))
    engine.handle(_request("set_program_recording_audio", {
        "microphone": {"mode": "system_default"},
        "system_audio": {"mode": "system_default"},
    }))
    engine.shutdown()
    assert all(s.released == 1 for s in runtime.sources if s.kind.startswith("pulse"))


def test_audio_mixer_missing_device_warns_but_still_binds():
    from solin.core.scenes.libobs_audio_sources import LibobsAudioMixer

    runtime = _CompositingRuntime()
    mixer = LibobsAudioMixer(runtime)
    warnings = mixer.apply({"mode": "device", "device_id": "ghost-mic"}, {"mode": "none"})

    assert warnings["microphone"] == "device_unavailable"  # id not in the enumerated set
    assert warnings["system_audio"] == ""
    # it still binds a source (the device may return; recording is not blocked)
    mics = _audio_sources(runtime, "pulse_input_capture")
    assert len(mics) == 1 and mics[0].settings["device_id"] == "ghost-mic"


def test_audio_mixer_source_create_failure_warns_and_leaks_nothing():
    from solin.core.scenes.libobs_audio_sources import LibobsAudioMixer

    runtime = _CompositingRuntime()
    runtime.fail_source_kinds = {"pulse_input_capture"}  # plugin refuses the device
    mixer = LibobsAudioMixer(runtime)
    warnings = mixer.apply({"mode": "device", "device_id": "usb-mic-1"},
                           {"mode": "system_default"})

    assert warnings["microphone"] == "device_unavailable"
    assert _audio_sources(runtime, "pulse_input_capture") == []  # nothing created
    # the system-audio slot is unaffected and bound
    assert warnings["system_audio"] == ""
    assert len(_audio_sources(runtime, "pulse_output_capture")) == 1


def test_audio_mixer_releases_channel_when_routing_fails():
    from solin.core.scenes.libobs_audio_sources import LibobsAudioMixer

    runtime = _CompositingRuntime()
    runtime.fail_set_channel_once = True  # obs_set_output_source raises once
    mixer = LibobsAudioMixer(runtime)
    warnings = mixer.apply({"mode": "device", "device_id": "usb-mic-1"}, {"mode": "none"})

    assert warnings["microphone"] == "device_unavailable"
    # the just-created source is released and the reserved channel is freed (no leak)
    mic = _audio_sources(runtime, "pulse_input_capture")[0]
    assert mic.released == 1
    assert mic not in runtime.channels.values()
    assert runtime.released_channels  # the acquired channel was handed back


def test_audio_mixer_keeps_live_source_when_enumeration_transiently_fails():
    from solin.core.scenes.libobs_audio_sources import LibobsAudioMixer

    runtime = _CompositingRuntime()
    mixer = LibobsAudioMixer(runtime)
    mixer.apply({"mode": "device", "device_id": "usb-mic-1"}, {"mode": "none"})
    live = _audio_sources(runtime, "pulse_input_capture")[0]

    runtime.enum_source_types_raises = True  # a momentary libobs hiccup
    warnings = mixer.apply({"mode": "device", "device_id": "usb-mic-1"}, {"mode": "none"})

    assert warnings["microphone"] == ""  # not reported as unavailable
    assert live.released == 0  # the live capture is NOT torn down over a transient failure
    assert live in runtime.channels.values()


def test_engine_start_recording_with_missing_device_warns_but_records():
    runtime = _CompositingRuntime()
    engine = LibobsSidecarEngine(runtime_factory=lambda: runtime)
    events: list[SceneIpcEnvelope] = []
    engine.set_event_sink(events.append)
    engine.handle(_request("hello"))

    start = engine.handle(_request("start_program_recording", {
        "path": _recording_path("rec.mp4"),
        "microphone": {"mode": "device", "device_id": "ghost-mic"},
        "system_audio": {"mode": "none"},
    }))
    assert _ack_from_envelope(start).applied is True  # recording still starts
    event = _program_recording_event_from_envelope(events[-1])
    assert event.state.status is ProgramRecordingStatus.RECORDING
    assert event.state.microphone_warning == "device_unavailable"
    assert runtime.outputs[-1].started == 1


def test_engine_live_device_switch_replaces_source_and_emits_state():
    runtime = _CompositingRuntime()
    engine = LibobsSidecarEngine(runtime_factory=lambda: runtime)
    events: list[SceneIpcEnvelope] = []
    engine.set_event_sink(events.append)
    engine.handle(_request("hello"))
    engine.handle(_request("start_program_recording", {
        "path": _recording_path("rec.mp4"),
        "microphone": {"mode": "device", "device_id": "usb-mic-1"},
        "system_audio": {"mode": "none"},
    }))
    first = _audio_sources(runtime, "pulse_input_capture")[0]

    # The app changes the mic mid-recording via a second message on the same engine.
    switch = engine.handle(_request("set_program_recording_audio", {
        "microphone": {"mode": "device", "device_id": "builtin-mic"},
        "system_audio": {"mode": "none"},
    }))
    assert _ack_from_envelope(switch).applied is True
    mics = _audio_sources(runtime, "pulse_input_capture")
    assert len(mics) == 2 and first.released == 1  # old torn down, new bound
    assert mics[1].settings["device_id"] == "builtin-mic"
    # a fresh recording-state event is published so the UI reflects the change
    latest = _program_recording_event_from_envelope(events[-1])
    assert latest.state.status is ProgramRecordingStatus.RECORDING


def test_engine_set_recording_audio_missing_device_emits_warning():
    runtime = _CompositingRuntime()
    engine = LibobsSidecarEngine(runtime_factory=lambda: runtime)
    events: list[SceneIpcEnvelope] = []
    engine.set_event_sink(events.append)
    engine.handle(_request("hello"))
    engine.handle(_request("start_program_recording", {
        "path": _recording_path("rec.mp4"),
        "microphone": {"mode": "system_default"},
        "system_audio": {"mode": "none"},
    }))

    engine.handle(_request("set_program_recording_audio", {
        "microphone": {"mode": "device", "device_id": "ghost-mic"},
        "system_audio": {"mode": "none"},
    }))
    event = _program_recording_event_from_envelope(events[-1])
    assert event.state.status is ProgramRecordingStatus.RECORDING
    assert event.state.microphone_warning == "device_unavailable"


def test_engine_list_audio_devices_without_runtime_reports_unavailable():
    engine = LibobsSidecarEngine()  # protocol-only: runtime never booted
    engine.handle(_request("hello"))
    # a valid "unavailable" discovery (supported False + error_code), not a crash
    discovery = _audio_device_discovery_from_envelope(
        engine.handle(_request("list_audio_devices"))
    )
    assert discovery.supported is False and discovery.error_code != ""
    assert discovery.devices == ()


def test_engine_list_local_cameras_without_runtime_reports_unavailable():
    engine = LibobsSidecarEngine()  # protocol-only: runtime never booted
    engine.handle(_request("hello"))
    discovery = _local_camera_discovery_from_envelope(
        engine.handle(_request("list_local_cameras"))
    )
    assert discovery.supported is False and discovery.error_code != ""


# ── media control: sidecar libobs decode driven over IPC (Fork A) ────────────

_MEDIA_DOC = {
    "sources": [
        {"id": "solin.content.current", "type": "solin_content", "name": "Content"},
        {"id": "ov", "type": "color", "name": "Overlay", "configuration": {"color": "#0000FF"}},
    ],
    "scenes": [
        {"id": "s1", "layers": [
            {"id": "content", "source_id": "solin.content.current", "visible": True,
             "rect": {"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0}},
            {"id": "overlay", "source_id": "ov", "visible": True,
             "rect": {"x": 0.5, "y": 0.5, "width": 0.5, "height": 0.5}},
        ]},
    ],
}


def _media_sources(runtime):
    return [s for s in runtime.sources if s.kind == "ffmpeg_source"]


def test_scene_graph_set_content_source_retargets_and_preserves_z_order():
    from solin.core.scenes.libobs_scene_builder import LibobsSceneGraph

    runtime = _CompositingRuntime()
    graph = LibobsSceneGraph(runtime)
    frame = object()
    graph.hydrate(_MEDIA_DOC, {"virtual_camera": "s1"}, frame)
    scene = runtime.scenes[0]
    content_item = next(i for i in scene.items if i.source is frame)
    assert content_item.order_position == 0  # content at the bottom, overlay on top

    media = object()
    graph.set_content_source(media)

    new_content = next(i for i in scene.items if i.source is media)
    assert new_content.order_position == 0  # z-order preserved
    assert all(i.source is not frame for i in scene.items)  # old content item gone
    overlay = [i for i in scene.items if getattr(i.source, "kind", None) == "color_source_v3"]
    assert overlay and overlay[0].order_position == 1  # overlay still on top


def test_scene_graph_set_content_source_none_uses_placeholder():
    from solin.core.scenes.libobs_scene_builder import LibobsSceneGraph

    runtime = _CompositingRuntime()
    graph = LibobsSceneGraph(runtime)
    frame = object()
    graph.hydrate(_MEDIA_DOC, {"virtual_camera": "s1"}, frame)
    graph.set_content_source(None)

    scene = runtime.scenes[0]
    content_item = min(scene.items, key=lambda i: i.order_position)
    assert getattr(content_item.source, "kind", None) == "color_source_v3"  # placeholder


def test_scene_graph_set_content_source_releases_replaced_placeholder():
    from solin.core.scenes.libobs_scene_builder import LibobsSceneGraph

    runtime = _CompositingRuntime()
    graph = LibobsSceneGraph(runtime)
    graph.hydrate(_MEDIA_DOC, {"virtual_camera": "s1"}, None)  # no content → placeholder
    scene = runtime.scenes[0]
    placeholder = min(scene.items, key=lambda i: i.order_position).source
    assert placeholder.kind == "color_source_v3"

    graph.set_content_source(object())
    assert placeholder.released == 1  # the stand-in placeholder is freed


def test_libobs_media_source_open_applies_volume_speed_and_local_flag():
    from solin.core.scenes.libobs_media_source import LibobsMediaSource

    runtime = _CompositingRuntime()
    media = LibobsMediaSource(runtime)
    assert media.open("/v.mp4", is_local_file=True, volume_percent=50, speed_percent=200) is True
    src = _media_sources(runtime)[-1]
    assert src.settings["is_local_file"] is True and src.settings["local_file"] == "/v.mp4"
    assert src.settings.get("speed_percent") == 200
    assert src.volume == 0.5
    assert media.state == MediaPlaybackState.PLAYING  # autoplay


def test_engine_open_media_puts_ffmpeg_source_in_content_slot_and_autoplays():
    runtime = _CompositingRuntime()
    engine = LibobsSidecarEngine(runtime_factory=lambda: runtime)
    events: list[SceneIpcEnvelope] = []
    engine.set_event_sink(events.append)
    engine.handle(_request("hello"))
    engine.handle(_request("hydrate", {"document": _MEDIA_DOC,
                                        "active_scenes": {"virtual_camera": "s1"}}))

    ack = _ack_from_envelope(engine.handle(_request("open_media", {
        "path": "/clip.mp4", "is_local_file": True, "autoplay": True,
        "volume_percent": 80, "speed_percent": 100, "trim_start_ms": 0, "trim_end_ms": 0,
    })))
    assert ack.applied is True
    media = _media_sources(runtime)[-1]
    assert media.settings["local_file"] == "/clip.mp4"
    assert media.play_pause_calls == [False]  # autoplay → play
    assert media.volume == 0.8
    assert any(i.source is media for i in runtime.scenes[0].items)  # in the content slot
    event = _media_playback_event_from_envelope(events[-1])
    assert event.state.path == "/clip.mp4"
    assert event.state.state is MediaPlaybackState.PLAYING
    engine.shutdown()


def test_engine_control_media_drives_transport_with_trim_offset():
    runtime = _CompositingRuntime()
    engine = LibobsSidecarEngine(runtime_factory=lambda: runtime)
    engine.set_event_sink(lambda _e: None)
    engine.handle(_request("hello"))
    engine.handle(_request("open_media", {"path": "/c.mp4", "is_local_file": True,
                                          "trim_start_ms": 2000}))
    media = _media_sources(runtime)[-1]
    assert media.media_time == 2000  # opened → seeked to trim start

    engine.handle(_request("control_media", {"action": "pause"}))
    assert media.play_pause_calls[-1] is True
    engine.handle(_request("control_media", {"action": "play"}))
    assert media.play_pause_calls[-1] is False
    engine.handle(_request("control_media", {"action": "seek", "position_ms": 5000}))
    assert media.media_time == 7000  # trim-relative 5000 → absolute 5000+2000
    engine.handle(_request("control_media", {"action": "restart"}))
    assert media.restarts == 1 and media.media_time == 2000  # restart re-seeks trim start
    engine.shutdown()


def test_engine_control_media_close_reverts_content_slot_and_stops_poller():
    runtime = _CompositingRuntime()
    engine = LibobsSidecarEngine(runtime_factory=lambda: runtime)
    engine.set_event_sink(lambda _e: None)
    engine.handle(_request("hello"))
    engine.handle(_request("hydrate", {"document": _MEDIA_DOC,
                                        "active_scenes": {"virtual_camera": "s1"}}))
    engine.handle(_request("open_media", {"path": "/c.mp4", "is_local_file": True}))
    media = _media_sources(runtime)[-1]
    assert engine._media_poller is not None  # poller running while media is open

    ack = _ack_from_envelope(engine.handle(_request("control_media", {"action": "close"})))
    assert ack.applied is True
    assert media.stops >= 1 and media.released == 1  # stopped + released
    assert engine._media_poller is None  # poller stopped
    assert all(i.source is not media for i in runtime.scenes[0].items)  # slot reverted


def test_engine_set_media_properties_updates_volume_and_speed():
    runtime = _CompositingRuntime()
    engine = LibobsSidecarEngine(runtime_factory=lambda: runtime)
    engine.set_event_sink(lambda _e: None)
    engine.handle(_request("hello"))
    engine.handle(_request("open_media", {"path": "/c.mp4", "is_local_file": True}))
    media = _media_sources(runtime)[-1]

    ack = _ack_from_envelope(engine.handle(_request("set_media_properties", {
        "volume_percent": 50, "speed_percent": 100,
    })))
    assert ack.applied is True
    assert media.volume == 0.5 and media.settings.get("speed_percent", 100) == 100
    engine.shutdown()


def test_engine_off_normal_speed_moves_the_audio_off_the_media_source():
    """The rate change is carried by a pitch-preserving stretch playing alongside."""
    runtime = _CompositingRuntime()
    engine = LibobsSidecarEngine(runtime_factory=lambda: runtime)
    engine.set_event_sink(lambda _e: None)
    engine.handle(_request("hello"))
    engine.handle(_request("open_media", {"path": "/c.mp4", "is_local_file": True}))
    media = _media_sources(runtime)[-1]

    ack = _ack_from_envelope(engine.handle(_request("set_media_properties", {
        "volume_percent": 50, "speed_percent": 150,
    })))

    assert ack.applied is True
    assert media.settings["speed_percent"] == 150
    assert media.volume == 0.0  # silenced; the stretch is heard instead
    engine.shutdown()


def test_engine_open_media_reports_trim_relative_position_and_duration():
    runtime = _CompositingRuntime()
    engine = LibobsSidecarEngine(runtime_factory=lambda: runtime)
    events: list[SceneIpcEnvelope] = []
    engine.set_event_sink(events.append)
    engine.handle(_request("hello"))
    # trim_end_ms is "ms removed from the end"; fake duration 60s, start 10s,
    # 20s off the end → window 10s..40s → 30s duration, position 0 at start
    engine.handle(_request("open_media", {"path": "/c.mp4", "is_local_file": True,
                                          "trim_start_ms": 10000, "trim_end_ms": 20000}))
    event = _media_playback_event_from_envelope(events[-1])
    assert event.state.duration_ms == 30000
    assert event.state.position_ms == 0
    engine.shutdown()


def test_engine_open_media_without_runtime_fails():
    engine = LibobsSidecarEngine()  # protocol-only, no runtime
    engine.handle(_request("hello"))
    ack = _ack_from_envelope(engine.handle(_request("open_media", {
        "path": "/c.mp4", "is_local_file": True,
    })))
    assert ack.applied is False and ack.error_code == "runtime_unavailable"


def test_engine_media_trim_end_latches_ended_state():
    runtime = _CompositingRuntime()
    engine = LibobsSidecarEngine(runtime_factory=lambda: runtime)
    engine.set_event_sink(lambda _e: None)
    engine.handle(_request("hello"))
    engine.handle(_request("open_media", {"path": "/c.mp4", "is_local_file": True,
                                          "trim_end_ms": 20000}))
    media = _media_sources(runtime)[-1]
    media.media_time = 40000  # at the trim end (duration 60000 - 20000)
    media._state = 1  # PLAYING

    state, _pos, _dur, _path = engine._sample_media()
    assert state == int(MediaPlaybackState.ENDED)
    assert media.play_pause_calls[-1] is True  # paused at the trim end
    # the source now reads PAUSED, but ENDED stays latched across ticks
    assert engine._sample_media()[0] == int(MediaPlaybackState.ENDED)

    engine.handle(_request("control_media", {"action": "restart"}))
    media._state = 1
    media.media_time = 0
    assert engine._sample_media()[0] != int(MediaPlaybackState.ENDED)  # latch cleared
    engine.shutdown()


def test_engine_media_oversized_trim_end_does_not_end_early():
    runtime = _CompositingRuntime()
    engine = LibobsSidecarEngine(runtime_factory=lambda: runtime)
    engine.set_event_sink(lambda _e: None)
    engine.handle(_request("hello"))
    # trim_end larger than the (unknown-to-the-app) real duration → ignore it
    engine.handle(_request("open_media", {"path": "/c.mp4", "is_local_file": True,
                                          "trim_end_ms": 70000}))
    media = _media_sources(runtime)[-1]
    media.media_time = 1000
    media._state = 1  # PLAYING

    state, _pos, duration, _path = engine._sample_media()
    assert state == int(MediaPlaybackState.PLAYING)  # no spurious instant end
    assert duration == 60000  # natural duration, not a collapsed 0
    engine.shutdown()


def test_engine_open_media_missing_path_fails():
    runtime = _CompositingRuntime()
    engine = LibobsSidecarEngine(runtime_factory=lambda: runtime)
    engine.handle(_request("hello"))
    ack = _ack_from_envelope(engine.handle(_request("open_media", {
        "path": "", "is_local_file": True,
    })))
    assert ack.applied is False and ack.error_code == "invalid_path"


def test_engine_control_media_unknown_action_is_rejected():
    runtime = _CompositingRuntime()
    engine = LibobsSidecarEngine(runtime_factory=lambda: runtime)
    engine.set_event_sink(lambda _e: None)
    engine.handle(_request("hello"))
    engine.handle(_request("open_media", {"path": "/c.mp4", "is_local_file": True}))
    ack = _ack_from_envelope(engine.handle(_request("control_media", {"action": "frobnicate"})))
    assert ack.applied is False and ack.error_code == "invalid_media_action"
    engine.shutdown()


class _FakePreviewEgress:
    def __init__(self) -> None:
        self.configured: list = []
        self.sources: list = []
        self.enabled: list = []
        self.shutdowns = 0

    def configure(self, descriptor) -> None:
        self.configured.append(descriptor)

    def set_scene_source(self, source) -> None:
        self.sources.append(source)

    def set_enabled(self, enabled) -> None:
        self.enabled.append(enabled)

    def shutdown(self) -> None:
        self.shutdowns += 1


def test_engine_hydrate_configures_preview_egress_and_sets_scene():
    runtime = _CompositingRuntime()
    engine = LibobsSidecarEngine(runtime_factory=lambda: runtime)
    engine.handle(_request("hello"))
    fake = _FakePreviewEgress()
    engine._preview_egress = fake  # replace the real egress with a recorder

    descriptor = {"transport": "shared_memory_bgra", "handle_token": "tok",
                  "width": 1920, "height": 1080}
    engine.handle(_request("hydrate", {
        "document": _MEDIA_DOC,
        "active_scenes": {"media_windows": "s1", "virtual_camera": "s1"},
        "preview_egress": descriptor,
    }))
    assert fake.configured[-1] == descriptor
    # the render is paused (None) before rebuild, then pointed at the edit scene
    assert fake.sources[0] is None
    assert fake.sources[-1] == "scene-source:solin-scene-s1"


def test_engine_hydrate_restores_preview_enable_from_render_enabled():
    # Regression: after a sidecar restart the fresh preview egress defaults to
    # disabled, and the app's edge-triggered render cache still reads "enabled" so
    # it never re-sends set_render_enabled. Hydrate carries render_enabled, so it
    # must re-apply the preview enable itself — otherwise the editor canvas stays
    # blank after a restart.
    runtime = _CompositingRuntime()
    engine = LibobsSidecarEngine(runtime_factory=lambda: runtime)
    engine.handle(_request("hello"))
    fake = _FakePreviewEgress()
    engine._preview_egress = fake

    engine.handle(_request("hydrate", {
        "document": _MEDIA_DOC,
        "active_scenes": {"media_windows": "s1", "virtual_camera": "s1", "editor": "s1"},
        "preview_egress": {"transport": "shared_memory_bgra", "handle_token": "tok",
                           "width": 1920, "height": 1080},
        "render_enabled": {"media_windows": True, "virtual_camera": True, "editor": True},
    }))
    assert fake.enabled[-1] is True  # preview re-enabled by the hydrate alone

    engine.handle(_request("hydrate", {
        "document": _MEDIA_DOC,
        "active_scenes": {"media_windows": "s1", "virtual_camera": "s1", "editor": "s1"},
        "render_enabled": {"media_windows": True, "virtual_camera": True, "editor": False},
    }))
    assert fake.enabled[-1] is False


def test_engine_hydrate_rebinds_window_targets():
    # Regression: after a restart the sidecar holds no displays while the app's
    # window-target cache still equals the recomputed targets (so it never
    # re-dispatches). Hydrate carries window_targets, so it must rebind the
    # projection displays itself — otherwise the projection stays black after a
    # restart.
    runtime = _CompositingRuntime()
    engine = LibobsSidecarEngine(runtime_factory=lambda: runtime)
    engine.handle(_request("hello"))
    recorder = _RecordingWindowOutput()
    engine._window_output = recorder

    engine.handle(_request("hydrate", {
        "document": _MEDIA_DOC,
        "active_scenes": {"media_windows": "s1", "virtual_camera": "s1"},
        "window_targets": [_window_target(101)],
    }))
    assert recorder.calls[-1] == [_window_target(101)]


def test_engine_set_render_enabled_toggles_preview_and_acks():
    runtime = _CompositingRuntime()
    engine = LibobsSidecarEngine(runtime_factory=lambda: runtime)
    engine.handle(_request("hello"))
    fake = _FakePreviewEgress()
    engine._preview_egress = fake

    on = engine.handle(_request("set_render_enabled", {"bus_id": "editor", "enabled": True}))
    assert _ack_from_envelope(on).applied is True
    assert fake.enabled[-1] is True

    off = engine.handle(_request("set_render_enabled", {"bus_id": "editor", "enabled": False}))
    assert _ack_from_envelope(off).applied is True
    assert fake.enabled[-1] is False

    # the program bus render is a no-op ack (it always composites)
    prog = engine.handle(_request("set_render_enabled", {"bus_id": "virtual_camera", "enabled": True}))
    assert _ack_from_envelope(prog).applied is True
    assert fake.enabled[-1] is False  # unchanged by the program-bus toggle


def test_engine_shutdown_stops_preview_egress_before_scene_graph():
    runtime = _CompositingRuntime()
    engine = LibobsSidecarEngine(runtime_factory=lambda: runtime)
    engine.handle(_request("hello"))
    fake = _FakePreviewEgress()
    engine._preview_egress = fake
    engine.shutdown()
    assert fake.shutdowns == 1


def test_engine_hydrate_configures_program_egress():
    runtime = _CompositingRuntime()
    engine = LibobsSidecarEngine(runtime_factory=lambda: runtime)
    engine.handle(_request("hello"))
    fake = _FakePreviewEgress()  # configure/shutdown surface suffices for the program egress
    engine._program_egress = fake

    descriptor = {"transport": "shared_memory_bgra", "handle_token": "ptok",
                  "width": 1280, "height": 720}
    engine.handle(_request("hydrate", {
        "document": _MEDIA_DOC,
        "active_scenes": {"virtual_camera": "s1"},
        "program_egress": descriptor,
    }))
    assert fake.configured[-1] == descriptor


def test_engine_shutdown_stops_program_egress():
    runtime = _CompositingRuntime()
    engine = LibobsSidecarEngine(runtime_factory=lambda: runtime)
    engine.handle(_request("hello"))
    fake = _FakePreviewEgress()
    engine._program_egress = fake
    engine.shutdown()
    assert fake.shutdowns == 1


def test_program_egress_tight_repacks_padded_stride():
    from solin.core.scenes.libobs_program_egress import LibobsProgramEgress

    # a 2x2 BGRA frame delivered with a padded stride (12 > 2*4) → tight 8-byte rows
    padded = bytes(range(24))  # 12 bytes/row * 2 rows
    tight = LibobsProgramEgress._tight(padded, 12, 2, 2)
    assert len(tight) == 2 * 2 * 4
    assert tight[0:8] == padded[0:8] and tight[8:16] == padded[12:20]


def test_scene_graph_scene_source_returns_built_scene():
    from solin.core.scenes.libobs_scene_builder import LibobsSceneGraph

    runtime = _CompositingRuntime()
    graph = LibobsSceneGraph(runtime)
    graph.hydrate(_MEDIA_DOC, {"virtual_camera": "s1"}, object())
    assert graph.scene_source("s1") == "scene-source:solin-scene-s1"
    assert graph.scene_source("missing") is None


# ── image + scene_reference sources ──────────────────────────────────────────

_SCENE_REF_DOC = {
    "sources": [
        # scene "a" references scene "b", which is defined AFTER it (forward ref)
        {"id": "ref", "type": "scene_reference", "name": "Ref",
         "configuration": {"target_scene_id": "b"}},
        {"id": "green", "type": "color", "name": "G", "configuration": {"color": "#00FF00"}},
    ],
    "scenes": [
        {"id": "a", "layers": [{"id": "la", "source_id": "ref", "visible": True,
                                "rect": {"x": 0, "y": 0, "width": 1.0, "height": 1.0}}]},
        {"id": "b", "layers": [{"id": "lb", "source_id": "green", "visible": True,
                                "rect": {"x": 0, "y": 0, "width": 1.0, "height": 1.0}}]},
    ],
}


def _scene_by_name(runtime, name):
    return next(s for s in runtime.scenes if s.name == name)


def test_scene_reference_resolves_to_the_target_scene_forward_ref():
    from solin.core.scenes.libobs_scene_builder import LibobsSceneGraph

    runtime = _CompositingRuntime()
    LibobsSceneGraph(runtime).hydrate(_SCENE_REF_DOC, {"virtual_camera": "a"})
    scene_a = _scene_by_name(runtime, "solin-scene-a")
    # the reference layer nests scene b's source (resolved despite b coming later)
    assert scene_a.items[0].source == "scene-source:solin-scene-b"


def test_scene_reference_unknown_target_uses_placeholder():
    from solin.core.scenes.libobs_scene_builder import LibobsSceneGraph

    doc = {
        "sources": [{"id": "ref", "type": "scene_reference", "name": "R",
                     "configuration": {"target_scene_id": "nope"}}],
        "scenes": [{"id": "a", "layers": [{"id": "la", "source_id": "ref", "visible": True,
                                           "rect": {"x": 0, "y": 0, "width": 1.0, "height": 1.0}}]}],
    }
    runtime = _CompositingRuntime()
    LibobsSceneGraph(runtime).hydrate(doc, {"virtual_camera": "a"})
    scene_a = _scene_by_name(runtime, "solin-scene-a")
    assert getattr(scene_a.items[0].source, "kind", None) == "color_source_v3"  # placeholder


def _image_doc(asset_id):
    return {
        "sources": [{"id": "im", "type": "image", "name": "I",
                     "configuration": {"asset_id": asset_id}}],
        "scenes": [{"id": "s", "layers": [{"id": "l", "source_id": "im", "visible": True,
                                           "rect": {"x": 0, "y": 0, "width": 1.0, "height": 1.0}}]}],
    }


def test_image_source_resolves_asset_to_image_source(tmp_path, monkeypatch):
    from solin.core.scenes.libobs_scene_builder import LibobsSceneGraph

    image = tmp_path / "logo.png"
    image.write_bytes(b"\x89PNG")
    monkeypatch.setenv("SOLIN_SCENE_IMAGES_DIR", str(tmp_path))

    runtime = _CompositingRuntime()
    LibobsSceneGraph(runtime).hydrate(_image_doc("logo"), {"virtual_camera": "s"})
    images = [s for s in runtime.sources if s.kind == "image_source"]
    assert len(images) == 1 and images[0].settings["file"] == str(image)


def test_image_source_unresolved_uses_placeholder(monkeypatch):
    from solin.core.scenes.libobs_scene_builder import LibobsSceneGraph

    monkeypatch.delenv("SOLIN_SCENE_IMAGES_DIR", raising=False)
    runtime = _CompositingRuntime()
    LibobsSceneGraph(runtime).hydrate(_image_doc("ghost"), {"virtual_camera": "s"})
    assert not any(s.kind == "image_source" for s in runtime.sources)
    assert any(s.kind == "color_source_v3" for s in runtime.sources)  # placeholder


def test_create_libobs_engine_exports_images_dir(tmp_path, monkeypatch):
    from solin.core.scenes.libobs_engine import create_libobs_scene_engine

    monkeypatch.delenv("SOLIN_SCENE_IMAGES_DIR", raising=False)
    engine = create_libobs_scene_engine(tmp_path)
    try:
        assert os.environ["SOLIN_SCENE_IMAGES_DIR"] == str(tmp_path)
    finally:
        engine.stop()


def test_engine_shutdown_closes_media_source():
    runtime = _CompositingRuntime()
    engine = LibobsSidecarEngine(runtime_factory=lambda: runtime)
    engine.set_event_sink(lambda _e: None)
    engine.handle(_request("hello"))
    engine.handle(_request("open_media", {"path": "/c.mp4", "is_local_file": True}))
    media = _media_sources(runtime)[-1]
    engine.shutdown()
    assert media.released == 1 and engine._media_poller is None


# ── content ingress: shared-memory frames → a libobs content source ──────────


def test_content_layer_uses_the_content_source_and_is_not_released():
    from solin.core.scenes.libobs_scene_builder import LibobsSceneGraph

    runtime = _CompositingRuntime()
    graph = LibobsSceneGraph(runtime)
    document = {
        "scenes": [
            {
                "id": "s",
                "layers": [
                    {"id": "c", "source_id": "solin.content.current", "visible": True,
                     "rect": {"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0}},
                    {"id": "p", "source_id": "other", "visible": True,
                     "rect": {"x": 0.0, "y": 0.0, "width": 0.5, "height": 0.5}},
                ],
            }
        ]
    }
    content_source = object()  # referenced, never created by the builder

    graph.hydrate(document, {"virtual_camera": "s"}, content_source)

    scene = runtime.scenes[0]
    assert scene.items[0].source is content_source  # content layer wired to it
    assert len(runtime.sources) == 1  # only the non-content layer got a placeholder

    graph.clear()
    # The placeholder is released; the injected content source is NOT (not owned).
    assert runtime.sources[0].released == 1


def test_content_consumer_delivers_frames_to_the_frame_source():
    from solin.core.scenes.content_frame_channel import SharedFrameChannelWriter
    from solin.core.scenes.content_frame_consumer import ContentFrameConsumer

    pushed: list[tuple] = []

    class _FakeFrameSource:
        source = "content-source"

        def push_bgra(self, data, width, height, stride, *, reset=False) -> bool:
            pushed.append((bytes(data), width, height, stride))
            return True

        def release(self) -> None:
            pass

    writer = SharedFrameChannelWriter(2, 2)
    consumer = ContentFrameConsumer(
        _CompositingRuntime(),
        {"handle_token": writer.name, "width": 2, "height": 2, "transport": "shared_memory_bgra"},
        frame_source_factory=lambda runtime, name: _FakeFrameSource(),
    )
    try:
        assert consumer.start() is True
        assert consumer.source == "content-source"
        writer.write(bytes(range(16)))
        deadline = time.monotonic() + 2.0
        while not pushed and time.monotonic() < deadline:
            time.sleep(0.02)
        assert pushed, "the pump thread never delivered a frame"
        assert pushed[0] == (bytes(range(16)), 2, 2, 8)
    finally:
        consumer.stop()
        writer.close()
        writer.unlink()


def test_content_consumer_start_fails_without_a_channel():
    from solin.core.scenes.content_frame_consumer import ContentFrameConsumer

    consumer = ContentFrameConsumer(
        _CompositingRuntime(),
        {"handle_token": "solin-no-such-channel", "width": 2, "height": 2,
         "transport": "shared_memory_bgra"},
        frame_source_factory=lambda runtime, name: None,
    )
    assert consumer.start() is False


def test_content_consumer_prepares_only_uploaded_current_presentations(monkeypatch):
    import threading
    import solin.core.scenes.content_frame_consumer as consumer_module
    from solin.core.scenes.content_frame_channel import SharedFrameChannelWriter

    # Exercise pumping deterministically, with the worker parked until shutdown.
    monkeypatch.setattr(consumer_module, "_POLL_SECONDS", 60)
    uploads = []

    class FrameSource:
        source = "content-source"

        def push_bgra(self, data, width, height, stride, *, reset=False):
            uploads.append((data[0], reset))
            return data[0] != 99

        def release(self):
            pass

    writer = SharedFrameChannelWriter(2, 2)
    consumer = consumer_module.ContentFrameConsumer(
        _CompositingRuntime(), {"handle_token": writer.name, "width": 2, "height": 2},
        frame_source_factory=lambda runtime, name: FrameSource(),
    )
    try:
        assert consumer.start()
        assert not consumer.wait_for_epoch(1, deadline=time.monotonic())
        for value, epoch, delivered in ((1, 1, True), (2, 1, True), (3, 0, False), (99, 2, False)):
            writer.write(bytes([value]) * 16, media_epoch=epoch)
            assert consumer.pump_once() is delivered
        assert consumer.wait_for_epoch(1, deadline=time.monotonic())
        assert not consumer.wait_for_epoch(2, deadline=time.monotonic())
        writer.write(bytes([4]) * 16, media_epoch=3)
        assert consumer.pump_once()
        assert not consumer.wait_for_epoch(2, deadline=time.monotonic() + 1)
        assert consumer.wait_for_epoch(3, deadline=time.monotonic())
        assert uploads == [(1, True), (2, False), (99, True), (4, True)]

        result = []
        waiter = threading.Thread(
            target=lambda: result.append(consumer.wait_for_epoch(4, deadline=time.monotonic() + 10))
        )
        waiter.start()
        consumer.stop()
        waiter.join(1)
        assert not waiter.is_alive()
        assert result == [False]
    finally:
        consumer.stop()
        writer.close()
        writer.unlink()


def test_prepare_requires_ready_content_but_not_ingress_for_decoded_media():
    from types import SimpleNamespace

    _runtime, engine, _preview = _engine_hydrated_on_a_with_fake_preview()
    waited = []

    def wait_for_epoch(epoch, *, deadline):
        waited.append((epoch, deadline))
        return epoch == 2

    engine._content_consumer = SimpleNamespace(wait_for_epoch=wait_for_epoch)
    payload = {
        "bus_id": "virtual_camera", "scene_id": "b",
        "transition": {"kind": "cut", "duration_ms": 0}, "content_media_epoch": 1,
    }
    response = engine.handle(_request("prepare_scene", payload))
    assert response.message_type == "error"
    assert response.payload["error_code"] == "source_unavailable"
    assert engine._scene_graph._pending == {}
    payload["content_media_epoch"] = 2
    assert engine.handle(_request("prepare_scene", payload)).message_type == "scene_prepared"
    assert waited == [(1, 123.456), (2, 123.456)]

    engine._media_source = SimpleNamespace(source="decoded-media")
    payload["content_media_epoch"] = 3
    assert engine.handle(_request("prepare_scene", payload)).message_type == "scene_prepared"
    assert len(waited) == 2
    engine._media_source = None
    payload["content_media_epoch"] = None
    assert engine.handle(_request("prepare_scene", payload)).message_type == "scene_prepared"
    assert len(waited) == 2


# ── factory + selection ──────────────────────────────────────────────────────


def test_factory_targets_the_sidecar_module():
    engine = create_libobs_scene_engine()
    config = engine._config
    assert config.executable == Path(sys.executable)
    assert config.arguments == ("-m", _SIDECAR_MODULE)
    # No GStreamer runtime — libobs is self-contained via pylibobs.
    assert config.gstreamer_runtime_root is None
    assert config.gstreamer_registry_path is None


def test_factory_is_resilient_to_transient_engine_stalls():
    # The sidecar handles IPC (incl. heartbeats) on one thread, so a heavy hydrate
    # under load can briefly delay a heartbeat. The engine must tolerate that
    # instead of killing a working engine and giving up mid-meeting.
    config = create_libobs_scene_engine()._config
    assert config.heartbeat_timeout_ms >= 8000
    assert config.heartbeat_timeout_ms > config.heartbeat_interval_ms
    assert config.maximum_restarts >= 20
    assert config.restart_window_seconds >= 120


def test_selection_reads_the_environment(monkeypatch):
    monkeypatch.setenv(ENGINE_SELECTION_ENV, "libobs")
    assert libobs_scene_engine_selected() is True
    monkeypatch.setenv(ENGINE_SELECTION_ENV, "  LibOBS ")
    assert libobs_scene_engine_selected() is True
    monkeypatch.setenv(ENGINE_SELECTION_ENV, "native")
    assert libobs_scene_engine_selected() is False
    # libobs is the default engine on this branch: no env var needed.
    monkeypatch.delenv(ENGINE_SELECTION_ENV, raising=False)
    assert libobs_scene_engine_selected() is True
    monkeypatch.setenv(ENGINE_SELECTION_ENV, "   ")
    assert libobs_scene_engine_selected() is True


# ── end-to-end: real subprocess under the real supervisor ────────────────────


def test_handshake_end_to_end_over_subprocess(monkeypatch):
    # Exercise the real subprocess + supervisor + IPC, but without booting libobs
    # (GPU/display dependent) — that keeps the wire/supervision contract
    # deterministic on any machine. The boot lifecycle is covered above.
    monkeypatch.setenv("SOLIN_LIBOBS_SIDECAR_NO_RUNTIME", "1")
    engine = create_libobs_scene_engine()
    try:
        future = engine.start(session_id="stage1ahandshake", deadline_ms=15_000)
        capabilities = future.result(timeout=20.0)
        assert capabilities.protocol_version == PROTOCOL_VERSION
        assert capabilities.virtual_camera is False

        deadline = time.monotonic() + 5.0
        while engine.health.status != SceneEngineStatus.READY and time.monotonic() < deadline:
            time.sleep(0.02)
        assert engine.health.status == SceneEngineStatus.READY
    finally:
        engine.stop()


def test_recording_captures_the_same_main_mix_the_virtual_camera_outputs():
    # The recording must always be the virtual-camera output, not some other view.
    # Both ride libobs' global mix: the vcam output binds context.get_video()/
    # get_audio(), and the recorder's encoders attach to that same global mix
    # (pylibobs' attach_global). Nothing here may bind a separate view.
    from solin.core.scenes.libobs_recorder import LibobsRecorder

    runtime = _CompositingRuntime()
    recorder = LibobsRecorder(runtime)
    assert recorder.start(_recording_path("solin-mix.mp4")) is True

    video, audio = runtime.encoders[-2], runtime.encoders[-1]
    assert video.attach_global is True  # the program composite, not a private view
    assert audio.attach_global is True
    output = runtime.outputs[-1]
    assert output.video_encoder is video and output.audio_encoder is audio
    # the recording output is fed only through those encoders — no separate media
    assert output.media is None
    recorder.stop()


class _FakeProjectionRoute:
    def __init__(self) -> None:
        self.scenes: list[tuple[str, object]] = []
        self.shutdowns = 0
        self.source_ptr = "PROJ"

    def set_scene(self, scene_id: str, source: object) -> bool:
        self.scenes.append((scene_id, source))
        return True

    def shutdown(self) -> None:
        self.shutdowns += 1


def test_engine_hydrate_points_the_projection_at_its_own_bus_scene():
    # Projection is its own output: hydrate must aim it at the projection bus's
    # scene rather than leaving it on whatever the program shows.
    runtime = _CompositingRuntime()
    engine = LibobsSidecarEngine(runtime_factory=lambda: runtime)
    engine.handle(_request("hello"))
    route = _FakeProjectionRoute()
    engine._projection_route = route

    engine.handle(_request("hydrate", {
        "document": _TWO_SCENE_DOC,
        "active_scenes": {"media_windows": "b", "virtual_camera": "a", "editor": "a"},
    }))

    assert route.scenes[-1][0] == "b"
    assert route.scenes[-1][1] == "scene-source:solin-scene-b"
    # the program is untouched by the projection routing
    assert engine._scene_graph._active_scene_id == "a"


def test_engine_shutdown_releases_the_projection_route():
    runtime = _CompositingRuntime()
    engine = LibobsSidecarEngine(runtime_factory=lambda: runtime)
    engine.handle(_request("hello"))
    route = _FakeProjectionRoute()
    engine._projection_route = route
    engine.shutdown()
    assert route.shutdowns == 1


def _engine_with_projection_route():
    runtime = _CompositingRuntime()
    engine = LibobsSidecarEngine(runtime_factory=lambda: runtime)
    engine.handle(_request("hello"))
    route = _FakeProjectionRoute()
    route.starts: list = []  # type: ignore[attr-defined]

    def _start(scene_id, source, kind, duration_ms):
        route.starts.append((scene_id, source, kind, duration_ms))
        return True

    route.start = _start  # type: ignore[assignment]
    engine._projection_route = route
    engine.handle(_request("hydrate", {
        "document": _TWO_SCENE_DOC,
        "active_scenes": {"media_windows": "a", "virtual_camera": "a", "editor": "a"},
    }))
    return runtime, engine, route


def test_projection_take_animates_its_own_transition_not_the_program():
    runtime, engine, route = _engine_with_projection_route()
    assert engine._scene_graph._active_scene_id == "a"

    prepared = engine.handle(_request("prepare_scene", {
        "bus_id": "media_windows", "scene_id": "b",
        "transition": {"kind": "dissolve", "duration_ms": 300},
    }))
    token = prepared.payload["preparation_token"]
    taken = engine.handle(_request("take_prepared", {
        "bus_id": "media_windows", "scene_id": "b", "preparation_token": token,
    }))

    assert _ack_from_envelope(taken).applied is True
    # the projection animated on its own transition …
    assert route.starts[-1][0] == "b"
    assert route.starts[-1][2] == "dissolve" and route.starts[-1][3] == 300
    # … and the program channel never moved
    assert engine._scene_graph._active_scene_id == "a"
    assert runtime.channels[0].current_source == "scene-source:solin-scene-a"


def test_a_take_cannot_move_an_output_its_token_was_not_prepared_for():
    # The token remembers its output, so a reordered or duplicated take cannot
    # swap the program using a token staged for the projection.
    _runtime, engine, route = _engine_with_projection_route()
    prepared = engine.handle(_request("prepare_scene", {
        "bus_id": "media_windows", "scene_id": "b",
        "transition": {"kind": "cut", "duration_ms": 0},
    }))
    token = prepared.payload["preparation_token"]

    ack = _ack_from_envelope(engine.handle(_request("take_prepared", {
        "bus_id": "virtual_camera", "scene_id": "b", "preparation_token": token,
    })))

    assert ack.applied is False and ack.error_code == "bus_mismatch"
    assert engine._scene_graph._active_scene_id == "a"  # program untouched
    assert route.starts == []  # and the projection did not move either


class _ShowRefLib:
    """Stands in for the unwrapped obs_source_inc/dec_showing symbols."""

    def __init__(self) -> None:
        self.counts: dict[object, int] = {}

    def obs_source_inc_showing(self, pointer) -> None:
        self.counts[pointer] = self.counts.get(pointer, 0) + 1

    def obs_source_dec_showing(self, pointer) -> None:
        self.counts[pointer] = self.counts.get(pointer, 0) - 1


def _thumbnail_egress_with_show_refs(monkeypatch):
    from solin.core.scenes import libobs_thumbnail_egress as module
    from solin.core.scenes.content_frame_channel import SharedFrameChannelReader

    lib = _ShowRefLib()
    monkeypatch.setattr("pylibobs._ffi.get_lib", lambda: lib, raising=False)
    sources = {sid: types.SimpleNamespace(_ptr=f"ptr-{sid}") for sid in ("a", "b")}
    egress = module.LibobsThumbnailEgress(
        _CompositingRuntime(), lambda sid: sources.get(sid)
    )
    # The app owns the block; the egress only attaches to it.
    block = SharedFrameChannelReader(None, 64, 72, create=True)
    egress.configure(
        {"transport": "shared_memory_bgra", "handle_token": block.name,
         "width": 64, "height": 72},
        ("a", "b"), 64, 36,
    )
    return egress, lib, block


def test_thumbnailed_scenes_are_show_reffed_so_every_card_is_live(monkeypatch):
    # Same mechanism OBS studio mode uses: show refs start the scene's sources
    # without making them active, so no projection audio reaches the program mix.
    egress, lib, block = _thumbnail_egress_with_show_refs(monkeypatch)
    assert lib.counts == {"ptr-a": 1, "ptr-b": 1}

    egress.shutdown()
    assert all(count == 0 for count in lib.counts.values())  # balanced
    block.close()
    block.unlink()


def test_thumbnail_show_refs_are_dropped_across_a_graph_rebuild(monkeypatch):
    # Scene sources are borrowed, so a rebuild destroys them: decrementing a show
    # ref afterwards would hit freed memory.
    egress, lib, block = _thumbnail_egress_with_show_refs(monkeypatch)

    egress.suspend()
    assert all(count == 0 for count in lib.counts.values())

    egress.resume()
    assert lib.counts == {"ptr-a": 1, "ptr-b": 1}

    egress.shutdown()
    assert all(count == 0 for count in lib.counts.values())
    block.close()
    block.unlink()


_FIT_DOCUMENT = {
    "scenes": [
        {
            "id": "scene-fit",
            "layers": [
                {"id": "contain", "source_id": "content", "visible": True,
                 "fit_mode": "contain",
                 "rect": {"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0}},
                {"id": "cover", "source_id": "cam", "visible": True,
                 "fit_mode": "cover",
                 "rect": {"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0}},
                {"id": "stretch", "source_id": "hidden", "visible": True,
                 "fit_mode": "stretch",
                 "rect": {"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0}},
            ],
        },
    ],
}


def test_each_layer_is_bounded_by_the_fit_it_asked_for():
    """contain letterboxes, cover fills and crops, stretch ignores the aspect.

    Hardcoding SCALE_INNER letterboxed everything, so a camera set to cover its
    rect rendered inside a box instead of filling it.
    """
    from solin.core.scenes.libobs_scene_builder import LibobsSceneGraph

    runtime = _CompositingRuntime()
    LibobsSceneGraph(runtime).hydrate(_FIT_DOCUMENT, {"virtual_camera": "scene-fit"})

    items = next(s for s in runtime.scenes if s.name == "solin-scene-scene-fit").items
    assert [item.bounds_type for item in items] == [2, 3, 1]  # INNER, OUTER, STRETCH
    assert all(item.bounds_alignment == 0 for item in items)  # all centred


def test_an_unknown_fit_mode_falls_back_to_letterboxing() -> None:
    from solin.core.scenes.libobs_scene_builder import LibobsSceneGraph

    runtime = _CompositingRuntime()
    LibobsSceneGraph(runtime).hydrate(
        {"scenes": [{"id": "s", "layers": [
            {"id": "L", "source_id": "content", "visible": True, "fit_mode": "wat",
             "rect": {"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0}}]}]},
        {"virtual_camera": "s"},
    )

    items = next(s for s in runtime.scenes if s.name == "solin-scene-s").items
    assert items[0].bounds_type == 2  # contain: never crop on a bad value


def test_one_ip_camera_in_many_scenes_opens_a_single_connection():
    """An IP camera usually serves only one or two concurrent streams.

    Creating a source per layer meant the first scene connected and every other
    scene was refused by the camera and rendered black — the camera appeared to
    work in exactly one scene however many it had been added to.
    """
    from solin.core.scenes.libobs_scene_builder import LibobsSceneGraph

    runtime = _CompositingRuntime()
    document = {
        "sources": [
            {"id": "cam", "type": "rtsp_camera", "name": "fachada",
             "configuration": {"uri": "rtsp://host/stream"}}
        ],
        "scenes": [
            {"id": "a", "layers": [
                {"id": "L1", "source_id": "cam", "visible": True,
                 "rect": {"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0}}]},
            {"id": "b", "layers": [
                {"id": "L2", "source_id": "cam", "visible": True,
                 "rect": {"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0}}]},
            {"id": "c", "layers": [
                {"id": "L3", "source_id": "cam", "visible": True,
                 "rect": {"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0}}]},
        ],
    }

    LibobsSceneGraph(runtime).hydrate(document, {"virtual_camera": "a"})

    assert len(runtime.rtsp_sources) == 1, "one camera must mean one connection"
    assert [request[0] for request in runtime.rtsp_requests] == ["cam"] * 3
    scenes = {scene.name: scene for scene in runtime.scenes}
    shown = {
        scenes[f"solin-scene-{scene_id}"].items[0].source
        for scene_id in ("a", "b", "c")
    }
    assert len(shown) == 1, "every scene must show the same source object"


def test_two_different_ip_cameras_get_their_own_connections():
    from solin.core.scenes.libobs_scene_builder import LibobsSceneGraph

    runtime = _CompositingRuntime()
    document = {
        "sources": [
            {"id": "cam1", "type": "rtsp_camera", "name": "a",
             "configuration": {"uri": "rtsp://host/one"}},
            {"id": "cam2", "type": "rtsp_camera", "name": "b",
             "configuration": {"uri": "rtsp://host/two"}},
        ],
        "scenes": [
            {"id": "s", "layers": [
                {"id": "L1", "source_id": "cam1", "visible": True,
                 "rect": {"x": 0.0, "y": 0.0, "width": 0.5, "height": 1.0}},
                {"id": "L2", "source_id": "cam2", "visible": True,
                 "rect": {"x": 0.5, "y": 0.0, "width": 0.5, "height": 1.0}}]},
        ],
    }

    LibobsSceneGraph(runtime).hydrate(document, {"virtual_camera": "s"})

    assert len(runtime.rtsp_sources) == 2
