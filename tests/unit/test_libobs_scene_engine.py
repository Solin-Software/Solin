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
    _capabilities_from_envelope,
    _command_error_from_envelope,
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
        self.channels: dict[int, object] = {}
        self.released_channels: list[int] = []
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

        self.ob = types.SimpleNamespace(Scene=_SceneNS, Source=_SourceNS)

    # obs_runtime boot contract
    def ensure_started(self, **_kwargs) -> None:
        self.starts += 1

    def shutdown(self) -> None:
        self.shutdowns += 1

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
    # Full-canvas layer sized to the canvas at the origin.
    assert scene_a.items[0].source.settings["width"] == 1920
    assert scene_a.items[0].source.settings["height"] == 1080
    assert scene_a.items[0].pos == (0.0, 0.0)
    # Bottom-right quadrant PiP.
    assert scene_a.items[1].source.settings["width"] == 960
    assert scene_a.items[1].source.settings["height"] == 540
    assert scene_a.items[1].pos == (960.0, 540.0)
    # The program scene is routed onto an acquired channel.
    assert runtime.channels[0] == "scene-source:solin-scene-scene-a"


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
    assert runtime.channels[0] == "scene-source:solin-scene-scene-a"


def test_engine_hydrate_without_runtime_acks_not_applied():
    engine = LibobsSidecarEngine()  # no runtime factory → no libobs
    response = engine.handle(
        _request("hydrate", {"document": _DOCUMENT, "active_scenes": {}})
    )
    assert response is not None and response.message_type == "ack"
    assert response.payload["applied"] is False
    assert response.payload["error_code"] == "runtime_unavailable"


def test_engine_set_window_targets_acks():
    engine = LibobsSidecarEngine()
    response = engine.handle(_request("set_window_targets", {"window_targets": []}))
    assert response is not None and response.message_type == "ack"
    assert response.payload["applied"] is True


def test_hello_advertises_hardware_compositing_only_when_booted():
    runtime = _CompositingRuntime()
    booted = LibobsSidecarEngine(runtime_factory=lambda: runtime)
    caps = _capabilities_from_envelope(booted.handle(_request("hello")))
    assert caps.hardware_compositing is True

    unbooted = LibobsSidecarEngine()  # no factory
    caps = _capabilities_from_envelope(unbooted.handle(_request("hello")))
    assert caps.hardware_compositing is False


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
