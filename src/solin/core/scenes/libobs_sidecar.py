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
from typing import BinaryIO, Callable

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
        return _reply(request, "heartbeat", {})
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

    @property
    def runtime_started(self) -> bool:
        return self._runtime_started

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

    def handle(self, request: SceneIpcEnvelope) -> SceneIpcEnvelope | None:
        if request.message_type == "hello":
            self._boot_runtime()
        return build_response(request)

    def shutdown(self) -> None:
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
    try:
        while True:
            try:
                request = read_envelope(source)
            except SceneIpcError:
                return
            if request is None:
                return  # clean EOF: the parent closed the pipe or exited
            response = active.handle(request)
            if response is not None:
                write_envelope(sink, response)
    finally:
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
