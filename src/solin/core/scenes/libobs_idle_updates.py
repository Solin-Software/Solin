"""Bounded idle preparation off the sidecar's foreground control loop."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import replace

from solin.core.scenes.idle import IdleScreenState
from solin.core.scenes.ipc_protocol import SceneIpcEnvelope
from solin.core.scenes.libobs_idle_source import IdleScreenError

log = logging.getLogger(__name__)


def _reject(request: SceneIpcEnvelope, code: str) -> SceneIpcEnvelope:
    return replace(
        request,
        message_type="ack",
        payload={
            "applied": False,
            "error_code": code,
            "error_message": "The idle screen update could not be applied",
        },
    )


class IdleScreenUpdates:
    """One worker and one latest pending request; never queue decoders or futures.

    Admission validates the envelope's session/runtime and reserves the provider
    revision before dispatch, without changing committed content. Both event
    delivery and response delivery serialize with admission so
    an obsolete request cannot publish after a newer request has been admitted.
    Close cancels preparation and joins before the caller releases runtime owners.
    """

    def __init__(
        self,
        handle: Callable[[SceneIpcEnvelope], SceneIpcEnvelope | None],
        emit: Callable[[SceneIpcEnvelope], None],
        cancel: Callable[[], None],
        *,
        admit: Callable[[SceneIpcEnvelope, IdleScreenState], None],
    ) -> None:
        self._handle = handle
        self._emit = emit
        self._cancel = cancel
        self._admit = admit
        self._condition = threading.Condition(threading.RLock())
        self._pending: SceneIpcEnvelope | None = None
        self._running: SceneIpcEnvelope | None = None
        self._latest: SceneIpcEnvelope | None = None
        self._state: IdleScreenState | None = None
        self._closed = False
        self._worker = threading.Thread(target=self._run, name="solin-idle-updates")
        self._worker.start()

    def submit(self, request: SceneIpcEnvelope) -> None:
        """Reject malformed or expired requests before cancelling useful work."""
        try:
            if request.message_type != "set_idle_screen" or set(request.payload) != {"idle_screen"}:
                raise ValueError("Invalid idle screen command")
            state = IdleScreenState.from_record(request.payload["idle_screen"])
        except ValueError:
            self._emit(_reject(request, "invalid_idle_screen"))
            return
        with self._condition:
            if self._closed:
                self._emit(_reject(request, "closed"))
                return
            if time.monotonic() * 1000 >= request.deadline_monotonic_ms:
                self._emit(_reject(request, "deadline_exceeded"))
                return
            previous = self._state
            if previous is not None:
                if state.revision < previous.revision:
                    self._emit(_reject(request, "stale_revision"))
                    return
                if state.revision == previous.revision and state != previous:
                    self._emit(_reject(request, "revision_conflict"))
                    return
            try:
                self._admit(request, state)
            except IdleScreenError as exc:
                self._emit(_reject(request, exc.error_code))
                return
            displaced = self._pending
            self._state = state
            self._latest = request
            self._pending = request
            if displaced is not None:
                self._emit(_reject(displaced, "stale_revision"))
            self._condition.notify()

    def emit_event(self, envelope: SceneIpcEnvelope) -> None:
        """Keep unrelated engine events, suppress obsolete idle handler events."""
        with self._condition:
            running = self._running
            if running is not None and envelope.request_id == running.request_id:
                if self._closed or running is not self._latest:
                    return
            self._emit(envelope)

    def close(self) -> None:
        try:
            with self._condition:
                if not self._closed:
                    self._closed = True
                    pending, self._pending = self._pending, None
                    try:
                        self._cancel()
                        if pending is not None:
                            self._emit(_reject(pending, "closed"))
                    finally:
                        self._condition.notify_all()
        finally:
            # Even a cancellation or sink failure cannot release the native
            # owner while its preparation handler is still using it.
            self._worker.join()

    def _run(self) -> None:
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._closed or self._pending is not None)
                if self._closed:
                    return
                request, self._pending = self._pending, None
                self._running = request
            assert request is not None
            try:
                response = self._handle(request)
            except Exception:  # noqa: BLE001 - preserve the sidecar handler error contract
                log.exception("Idle screen handler crashed")
                response = replace(
                    request,
                    message_type="error",
                    payload={
                        "error_code": "handler_error",
                        "error_message": "the engine failed to handle 'set_idle_screen'",
                    },
                )
            with self._condition:
                try:
                    if self._closed:
                        self._emit(_reject(request, "closed"))
                    elif request is not self._latest:
                        self._emit(_reject(request, "stale_revision"))
                    elif response is not None:
                        self._emit(response)
                except Exception:  # noqa: BLE001 - a broken IPC sink must not strand the worker
                    log.exception("Could not emit idle screen response")
                finally:
                    self._running = None
