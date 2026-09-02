"""Stage 1a — libobs scene-engine sidecar handshake + factory.

The sidecar's replies are validated with the *real* client-side parsers from
``process_engine`` so the two sides are proven wire-compatible, and one test
spawns the sidecar under the actual ``SubprocessSceneEngine`` supervisor to prove
the full handshake end-to-end.
"""

from __future__ import annotations

import io
import sys
import time
import types
from pathlib import Path

from solin.core.scenes.engine import SceneEngineStatus
from solin.core.scenes.recording import ProgramRecordingStatus
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
from solin.core.scenes.process_engine import (
    SceneEngineCommandRejectedError,
    _ack_from_envelope,
    _audio_device_discovery_from_envelope,
    _capabilities_from_envelope,
    _command_error_from_envelope,
    _local_camera_discovery_from_envelope,
    _program_recording_event_from_envelope,
)

_SIDECAR_MODULE = "solin.core.scenes.libobs_sidecar"


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
    assert response.payload == {}
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
    def __init__(self, source) -> None:
        self.source = source
        self.pos = (0.0, 0.0)
        self.bounds = (0.0, 0.0)
        self.bounds_type = 0
        self.bounds_alignment = 0


class _FakeScene:
    def __init__(self, name: str) -> None:
        self.name = name
        self.items: list[_FakeItem] = []
        self.released = 0

    def add(self, source) -> _FakeItem:
        item = _FakeItem(source)
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

    def release(self) -> None:
        self.released += 1


class _FakeEncoder:
    def __init__(self, kind: str, name: str, settings: dict) -> None:
        self.kind = kind
        self.name = name
        self.settings = dict(settings)
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

    def set_media(self, video, audio) -> None:
        self.media = (video, audio)

    def set_video_encoder(self, encoder) -> None:
        self.video_encoder = encoder

    def set_audio_encoder(self, encoder, idx: int = 0) -> None:
        self.audio_encoder = encoder

    def start(self) -> bool:
        self.started += 1
        return self.start_ok

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
        self.channels: dict[int, object] = {}
        self.released_channels: list[int] = []
        self.output_types = ["virtualcam_output", "mp4_output", "ffmpeg_muxer"]
        self.output_start_ok = True
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
            def create(kind: str, name: str, settings: dict) -> _FakeColorSource:
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
                runtime.outputs.append(output)
                return output

        class _VideoEncoderNS:
            @staticmethod
            def create(kind: str, name: str, settings: dict, *, attach_global: bool = True):
                encoder = _FakeEncoder(kind, name, settings)
                runtime.encoders.append(encoder)
                return encoder

        class _AudioEncoderNS:
            @staticmethod
            def create(kind: str, name: str, settings: dict, mixer_idx: int = 0,
                       *, attach_global: bool = True):
                encoder = _FakeEncoder(kind, name, settings)
                runtime.encoders.append(encoder)
                return encoder

        self.encoders: list[_FakeEncoder] = []
        self.ob = types.SimpleNamespace(
            Scene=_SceneNS,
            Source=_SourceNS,
            Transition=_TransitionNS,
            Output=_OutputNS,
            VideoEncoder=_VideoEncoderNS,
            AudioEncoder=_AudioEncoderNS,
            enum_output_types=lambda: list(runtime.output_types),
            BoundsType=types.SimpleNamespace(SCALE_INNER=2),
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

    # shared camera source (runtime-owned)
    def camera_source(self, device_id: str, name: str = ""):
        self.camera_sources.append((device_id, name))
        return types.SimpleNamespace(kind="camera", device_id=device_id, name=name)

    # channel routing
    def acquire_channel(self) -> int:
        channel = self._next_channel
        self._next_channel += 1
        return channel

    def set_channel_source(self, channel: int, source) -> None:
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
    assert scene_a.items[0].bounds_type == 2  # SCALE_INNER
    assert scene_a.items[0].bounds_alignment == 5  # LEFT | TOP
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


def _window_target(handle: int, *, width=1280, height=720, dpr=1.0, visible=True) -> dict:
    return {
        "bus_id": "media_windows",
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


class _RecordingWindowOutput:
    def __init__(self) -> None:
        self.calls: list[list] = []
        self.shutdowns = 0

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


def test_virtual_camera_starts_and_stops_the_output():
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


def test_engine_set_output_enabled_toggles_the_virtual_camera():
    runtime = _CompositingRuntime()
    engine = LibobsSidecarEngine(runtime_factory=lambda: runtime)
    engine.handle(_request("hello"))

    on = engine.handle(_request("set_output_enabled", {"bus_id": "virtual_camera", "enabled": True}))
    assert on is not None and on.message_type == "ack" and on.payload["applied"] is True
    assert runtime.outputs[-1].started == 1

    off = engine.handle(_request("set_output_enabled", {"bus_id": "virtual_camera", "enabled": False}))
    assert off is not None and off.payload["applied"] is True
    assert runtime.outputs[-1].stopped == 1


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

    assert recorder.start("/tmp/solin-test.mp4") is True
    assert recorder.active is True and recorder.path == "/tmp/solin-test.mp4"
    output = runtime.outputs[-1]
    assert output.kind == "mp4_output"  # the in-process muxer is preferred
    assert output.settings["path"] == "/tmp/solin-test.mp4"
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
    assert recorder.start("/tmp/a.mp4") is True
    assert recorder.start("/tmp/b.mp4") is False  # already recording
    assert len(runtime.outputs) == 1  # no second output created


def test_recorder_start_failure_releases_everything():
    from solin.core.scenes.libobs_recorder import LibobsRecorder

    runtime = _CompositingRuntime()
    runtime.output_start_ok = False  # output.start() returns False
    recorder = LibobsRecorder(runtime)

    assert recorder.start("/tmp/nope.mp4") is False
    assert recorder.active is False
    # the output and both encoders were released on the failed path
    assert runtime.outputs[-1].released == 1
    assert all(encoder.released == 1 for encoder in runtime.encoders)


def test_recorder_unavailable_when_muxer_missing():
    from solin.core.scenes.libobs_recorder import LibobsRecorder

    runtime = _CompositingRuntime()
    runtime.output_types = ["virtualcam_output"]  # no ffmpeg_muxer
    recorder = LibobsRecorder(runtime)

    assert recorder.start("/tmp/x.mp4") is False
    assert runtime.outputs == [] and runtime.encoders == []


def test_engine_start_and_stop_recording_acks_and_emits_state():
    runtime = _CompositingRuntime()
    engine = LibobsSidecarEngine(runtime_factory=lambda: runtime)
    events: list[SceneIpcEnvelope] = []
    engine.set_event_sink(events.append)
    engine.handle(_request("hello"))

    start = engine.handle(_request("start_program_recording", {
        "path": "/tmp/rec.mp4", "width": 1920, "height": 1080,
        "fps_numerator": 30, "fps_denominator": 1,
        "microphone": {}, "system_audio": {},
    }))
    assert _ack_from_envelope(start).applied is True
    assert runtime.outputs[-1].kind == "mp4_output"
    assert runtime.outputs[-1].started == 1
    # the sidecar emitted a "recording" state the client can parse
    recording_event = _program_recording_event_from_envelope(events[-1])
    assert recording_event.state.status is ProgramRecordingStatus.RECORDING
    assert recording_event.state.path == "/tmp/rec.mp4"

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

    response = engine.handle(_request("start_program_recording", {"path": "/tmp/r.mp4"}))
    assert _ack_from_envelope(response).applied is False
    failed = _program_recording_event_from_envelope(events[-1])
    assert failed.state.status is ProgramRecordingStatus.FAILED
    assert failed.state.error_code == "recording_start_failed"


def test_engine_start_recording_without_runtime_fails():
    engine = LibobsSidecarEngine()  # protocol-only, no runtime
    engine.handle(_request("hello"))
    response = engine.handle(_request("start_program_recording", {"path": "/tmp/r.mp4"}))
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


def test_engine_list_audio_devices_returns_an_empty_discovery():
    runtime = _CompositingRuntime()
    engine = LibobsSidecarEngine(runtime_factory=lambda: runtime)
    engine.handle(_request("hello"))
    discovery = _audio_device_discovery_from_envelope(
        engine.handle(_request("list_audio_devices"))
    )
    assert discovery.supported is True and discovery.devices == ()


def test_engine_list_local_cameras_returns_an_empty_discovery():
    runtime = _CompositingRuntime()
    engine = LibobsSidecarEngine(runtime_factory=lambda: runtime)
    engine.handle(_request("hello"))
    discovery = _local_camera_discovery_from_envelope(
        engine.handle(_request("list_local_cameras"))
    )
    assert discovery.devices == ()


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
    engine.handle(_request("start_program_recording", {"path": "/tmp/r.mp4"}))
    output = runtime.outputs[-1]

    engine.shutdown()
    assert output.stopped == 1 and output.released == 1


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

        def push_bgra(self, data, width, height, stride) -> None:
            pushed.append((bytes(data), width, height, stride))

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


# ── factory + selection ──────────────────────────────────────────────────────


def test_factory_targets_the_sidecar_module():
    engine = create_libobs_scene_engine()
    config = engine._config
    assert config.executable == Path(sys.executable)
    assert config.arguments == ("-m", _SIDECAR_MODULE)
    # No GStreamer runtime — libobs is self-contained via pylibobs.
    assert config.gstreamer_runtime_root is None
    assert config.gstreamer_registry_path is None


def test_selection_reads_the_environment(monkeypatch):
    monkeypatch.setenv(ENGINE_SELECTION_ENV, "libobs")
    assert libobs_scene_engine_selected() is True
    monkeypatch.setenv(ENGINE_SELECTION_ENV, "  LibOBS ")
    assert libobs_scene_engine_selected() is True
    monkeypatch.setenv(ENGINE_SELECTION_ENV, "native")
    assert libobs_scene_engine_selected() is False
    monkeypatch.delenv(ENGINE_SELECTION_ENV, raising=False)
    assert libobs_scene_engine_selected() is False


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
