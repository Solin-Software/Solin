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
