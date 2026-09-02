"""libobs scene-engine sidecar — Stage 1a: the lifecycle handshake.

A separate, supervised process that speaks the scene-engine control protocol
(:mod:`solin.core.scenes.ipc_protocol`, PROTOCOL_VERSION 7) over stdin/stdout, so
it can be driven by the existing :class:`~solin.core.scenes.process_engine.SubprocessSceneEngine`
client — the *same* supervision / heartbeat / restart / IPC machinery ``main``
uses for its native GStreamer engine, pointed at libobs instead.

Stage 1a implements only the lifecycle contract:

* ``hello`` → ``hello_ack`` carrying the engine capability flags, and
* ``ping`` → ``heartbeat`` for liveness.

Every other control message is answered with a ``not_implemented`` error until
later stages wire compositing, cameras, the virtual camera and recording. libobs
itself is deliberately **not** initialised here yet (that arrives with the first
compositing stage), which keeps the lifecycle contract testable in a headless
environment with no GPU or native runtime.
"""

from __future__ import annotations

import sys
from typing import BinaryIO

from solin.core.scenes.ipc_protocol import (
    SceneIpcEnvelope,
    SceneIpcError,
    read_envelope,
    write_envelope,
)

# Capability flags advertised in the hello handshake. All False in Stage 1a: the
# engine completes the lifecycle handshake but composites nothing yet. Each flag
# flips to True as its stage lands:
#   hardware_compositing                         -> Stage 2 (scene graph + transitions)
#   local_cameras / rtsp_cameras                 -> Stage 3 (camera sources)
#   virtual_camera                               -> Stage 4 (obs_view -> DShow sink)
#   program_recording / audio_*_capture          -> Stage 5 (recording + audio)
#   d3d11_shared_textures                        -> not planned (libobs sources are fed directly)
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

    Pure and side-effect free, so the protocol contract is unit-testable without
    spawning a process.
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


def serve(source: BinaryIO, sink: BinaryIO) -> None:
    """Run the control loop until the input stream closes or is corrupted.

    Exiting the loop lets the supervising client observe the process end and
    apply its restart policy. A malformed frame is treated the same way — the
    channel is no longer trustworthy, so the generation is abandoned.
    """
    while True:
        try:
            request = read_envelope(source)
        except SceneIpcError:
            return
        if request is None:
            return  # clean EOF: the parent closed the pipe or exited
        response = build_response(request)
        if response is not None:
            write_envelope(sink, response)


def main() -> int:
    # The control protocol owns stdout; only raw frames go there. Anything the
    # engine wants to log must go to stderr (the client drains it as warnings).
    serve(sys.stdin.buffer, sys.stdout.buffer)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
