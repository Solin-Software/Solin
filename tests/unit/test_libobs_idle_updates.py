"""Idle command admission, cancellation and control-loop responsiveness."""

from __future__ import annotations

import io
import threading
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from solin.core.scenes.idle import IdleScreenState
from solin.core.scenes.ipc_protocol import SceneIpcEnvelope, encode_envelope, read_envelope
from solin.core.scenes.libobs_idle_updates import IdleScreenUpdates
from solin.core.scenes.libobs_sidecar import LibobsSidecarEngine, serve


def _request(revision: int, *, payload=None, message_type="set_idle_screen"):
    return SceneIpcEnvelope(
        message_type=message_type,
        request_id=f"request-{revision}",
        session_id="idle-test",
        process_generation="generation-1",
        sequence=revision,
        document_revision=4,
        deadline_monotonic_ms=int(time.monotonic() * 1000) + 10_000,
        payload=payload
        if payload is not None
        else {
            "idle_screen": IdleScreenState(
                revision=revision, media_path=f"{revision}.mp4"
            ).to_record(),
        },
    )


class _Replies:
    def __init__(self):
        self.items = []
        self.condition = threading.Condition()

    def emit(self, envelope):
        with self.condition:
            self.items.append(envelope)
            self.condition.notify_all()

    def response(self, request):
        with self.condition:
            assert self.condition.wait_for(
                lambda: any(
                    item.request_id == request.request_id and item.message_type in ("ack", "error")
                    for item in self.items
                ),
                timeout=3,
            ), "idle response did not arrive"
            return next(
                item
                for item in self.items
                if item.request_id == request.request_id and item.message_type in ("ack", "error")
            )


class _Engine(LibobsSidecarEngine):
    def __init__(self, *, release_on_cancel=False):
        super().__init__(runtime_factory=None)
        self.started = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()
        self.calls = []
        self.events = []
        self.cancel_count = 0
        self.admissions = []
        self.release_on_cancel = release_on_cancel
        self.failure = None

    def handle(self, request):
        if request.message_type != "set_idle_screen":
            self.events.append(request.message_type)
            return replace(request, message_type="heartbeat", payload={})
        self.calls.append(request.request_id)
        self.started.set()
        if len(self.calls) == 1:
            assert self.release.wait(3), "test preparation was not released"
        try:
            if self.failure is not None:
                if isinstance(self.failure, Exception):
                    raise self.failure
                return replace(request, message_type="ack", payload=self.failure)
            if self._event_sink is not None:
                self._event_sink(replace(request, message_type="source_health", payload={}))
            return replace(
                request,
                message_type="ack",
                payload={
                    "applied": True,
                    "error_code": "",
                    "error_message": "",
                },
            )
        finally:
            self.finished.set()

    def cancel_idle_preparation(self):
        self.cancel_count += 1
        self.events.append("cancel")
        if self.release_on_cancel:
            self.release.set()

    def admit_idle_screen(self, request, state):
        self.admissions.append((request, state))

    def shutdown(self):
        assert not self.calls or self.finished.is_set()
        self.events.append("shutdown")


def test_slow_idle_preparation_does_not_block_serve_ping_or_foreground():
    engine = _Engine(release_on_cancel=True)
    idle, ping, foreground = (
        _request(1),
        _request(2, message_type="ping"),
        _request(
            3,
            message_type="control_media",
        ),
    )

    class Source(io.BytesIO):
        def read(self, size=-1):
            if self.tell() == len(encode_envelope(idle)):
                assert engine.started.wait(3)
            return super().read(size)

    class Sink(io.BytesIO):
        def write(self, data):
            response = read_envelope(io.BytesIO(data))
            if response.request_id in (ping.request_id, foreground.request_id):
                assert engine.started.is_set() and not engine.release.is_set()
            return super().write(data)

    source = Source(b"".join(map(encode_envelope, (idle, ping, foreground))))
    sink = Sink()
    serve(source, sink, engine=engine)
    responses = io.BytesIO(sink.getvalue())
    received = []
    while (response := read_envelope(responses)) is not None:
        received.append(response)
    assert [item.request_id for item in received[:2]] == [ping.request_id, foreground.request_id]
    assert engine.events[-2:] == ["cancel", "shutdown"]


def test_latest_pending_only_and_obsolete_handlers_cannot_publish():
    engine, replies = _Engine(), _Replies()
    updates = IdleScreenUpdates(
        engine.handle, replies.emit, engine.cancel_idle_preparation, admit=engine.admit_idle_screen
    )
    engine.set_event_sink(updates.emit_event)
    a, b, c = map(_request, (1, 2, 3))
    try:
        updates.submit(a)
        assert engine.started.wait(3)
        updates.submit(b)
        updates.submit(c)
        assert replies.response(b).payload["error_code"] == "stale_revision"
        assert engine.calls == [a.request_id]
        assert [state.revision for _, state in engine.admissions] == [1, 2, 3]
        assert engine.cancel_count == 0  # admission supersedes; cancellation is for shutdown
        engine.release.set()
        assert replies.response(a).payload["error_code"] == "stale_revision"
        assert replies.response(c).payload["applied"] is True
        assert engine.calls == [a.request_id, c.request_id]
        assert [
            item.request_id for item in replies.items if item.message_type == "source_health"
        ] == [
            c.request_id,
        ]
        for request in (a, b, c):
            response = replies.response(request)
            assert response.session_id == request.session_id
            assert response.sequence == request.sequence
            assert response.document_revision == request.document_revision
            assert response.deadline_monotonic_ms == request.deadline_monotonic_ms
    finally:
        engine.release.set()
        updates.close()


@pytest.mark.parametrize(
    "bad_state",
    [
        None,
        {},
        {
            "revision": True,
            "media_path": "",
            "yeartext_image_path": "",
            "yeartext_revision": 0,
        },
        IdleScreenState(revision=2).to_record() | {"unexpected": 1},
    ],
)
def test_invalid_state_never_cancels_or_opens_decoder(bad_state):
    engine, replies = _Engine(), _Replies()
    updates = IdleScreenUpdates(
        engine.handle, replies.emit, engine.cancel_idle_preparation, admit=engine.admit_idle_screen
    )
    a, invalid = _request(1), _request(2, payload={"idle_screen": bad_state})
    try:
        updates.submit(a)
        assert engine.started.wait(3)
        updates.submit(invalid)
        assert replies.response(invalid).payload["error_code"] == "invalid_idle_screen"
        assert engine.cancel_count == 0
        assert [state.revision for _, state in engine.admissions] == [1]
        engine.release.set()
        assert replies.response(a).payload["applied"] is True
        assert engine.calls == [a.request_id]
    finally:
        engine.release.set()
        updates.close()


@pytest.mark.parametrize("kind", ["expired", "stale", "conflict"])
def test_unusable_requests_leave_running_choice_intact(kind):
    engine, replies = _Engine(), _Replies()
    updates = IdleScreenUpdates(
        engine.handle, replies.emit, engine.cancel_idle_preparation, admit=engine.admit_idle_screen
    )
    a = _request(2)
    rejected = _request(1 if kind == "stale" else 3)
    if kind == "expired":
        rejected = replace(rejected, deadline_monotonic_ms=0)
    elif kind == "conflict":
        rejected = replace(
            rejected,
            payload={
                "idle_screen": IdleScreenState(
                    revision=2,
                    media_path="other.mp4",
                ).to_record()
            },
        )
    try:
        updates.submit(a)
        assert engine.started.wait(3)
        updates.submit(rejected)
        assert (
            replies.response(rejected).payload["error_code"]
            == {
                "expired": "deadline_exceeded",
                "stale": "stale_revision",
                "conflict": "revision_conflict",
            }[kind]
        )
        assert engine.cancel_count == 0
        assert [state.revision for _, state in engine.admissions] == [2]
        engine.release.set()
        assert replies.response(a).payload["applied"] is True
    finally:
        engine.release.set()
        updates.close()


@pytest.mark.parametrize(
    "failure",
    [
        {"applied": False, "error_code": "idle_media_invalid", "error_message": "decode failed"},
        RuntimeError("handler failed"),
    ],
)
def test_errors_are_preserved_and_worker_remains_available(failure):
    engine, replies = _Engine(), _Replies()
    engine.failure = failure
    engine.release.set()
    updates = IdleScreenUpdates(
        engine.handle, replies.emit, engine.cancel_idle_preparation, admit=engine.admit_idle_screen
    )
    a, b = _request(1), _request(2)
    try:
        updates.submit(a)
        response = replies.response(a)
        if isinstance(failure, Exception):
            assert response.message_type == "error"
            assert response.payload["error_code"] == "handler_error"
        else:
            assert response.payload == failure
        engine.failure = None
        updates.submit(b)
        assert replies.response(b).payload["applied"] is True
    finally:
        updates.close()


def test_close_cancels_and_joins_before_owner_shutdown():
    engine, replies = _Engine(release_on_cancel=True), _Replies()
    updates = IdleScreenUpdates(
        engine.handle, replies.emit, engine.cancel_idle_preparation, admit=engine.admit_idle_screen
    )
    a, b = _request(1), _request(2)
    updates.submit(a)
    assert engine.started.wait(3)
    # Hold A until close, so B is provably pending rather than another decoder.
    engine.release_on_cancel = False
    updates.submit(b)
    engine.release_on_cancel = True
    updates.close()
    engine.shutdown()
    assert engine.calls == [a.request_id]
    assert replies.response(a).payload["error_code"] == "closed"
    assert replies.response(b).payload["error_code"] == "closed"
    assert engine.events[-2:] == ["cancel", "shutdown"]
    assert not updates._worker.is_alive()
    count = engine.cancel_count
    updates.close()
    assert engine.cancel_count == count
    late = _request(3)
    updates.submit(late)
    assert replies.response(late).payload["error_code"] == "closed"


def test_close_still_joins_when_cancellation_fails():
    engine, replies = _Engine(), _Replies()

    def cancel():
        engine.release.set()
        raise RuntimeError("native cancellation failed")

    updates = IdleScreenUpdates(engine.handle, replies.emit, cancel, admit=engine.admit_idle_screen)
    updates.submit(_request(1))
    assert engine.started.wait(3)
    with pytest.raises(RuntimeError, match="native cancellation failed"):
        updates.close()
    assert engine.finished.is_set()
    assert not updates._worker.is_alive()
    engine.shutdown()


@pytest.mark.parametrize("failure", ["session", "generation", "runtime", "owner"])
def test_sidecar_admission_rejects_foreign_or_unavailable_context_before_superseding(failure):
    engine, replies = _Engine(), _Replies()
    authority = LibobsSidecarEngine(runtime_factory=None)
    authority._session_id = "idle-test"
    authority._process_generation = "generation-1"
    authority._runtime_started = True
    admitted = []
    owner = SimpleNamespace(admit=admitted.append)
    authority._idle_source = owner
    updates = IdleScreenUpdates(
        engine.handle, replies.emit, engine.cancel_idle_preparation,
        admit=authority.admit_idle_screen,
    )
    a, invalid = _request(1), _request(100)
    try:
        updates.submit(a)
        assert engine.started.wait(3)
        if failure == "session":
            invalid = replace(invalid, session_id="previous-session")
        elif failure == "generation":
            invalid = replace(invalid, process_generation="previous-process")
        elif failure == "runtime":
            authority._runtime_started = False
        else:
            authority._idle_source = None
        updates.submit(invalid)
        expected = "session_mismatch" if failure in ("session", "generation") else "runtime_unavailable"
        assert replies.response(invalid).payload["error_code"] == expected
        assert [state.revision for state in admitted] == [1]
        assert engine.cancel_count == 0
        assert not engine.release.is_set()
        engine.release.set()
        assert replies.response(a).payload["applied"] is True
        assert engine.calls == [a.request_id]
        # Rejection must not poison the worker's revision watermark either.
        authority._runtime_started = True
        authority._idle_source = owner
        valid = _request(2)
        updates.submit(valid)
        assert replies.response(valid).payload["applied"] is True
        assert [state.revision for state in admitted] == [1, 2]
    finally:
        engine.release.set()
        updates.close()


def test_identical_revision_requests_are_admitted_without_cancelling_running_handler():
    engine, replies = _Engine(), _Replies()
    updates = IdleScreenUpdates(
        engine.handle, replies.emit, engine.cancel_idle_preparation, admit=engine.admit_idle_screen
    )
    first = _request(1)
    repeated = replace(first, request_id="retry-same-choice")
    try:
        updates.submit(first)
        assert engine.started.wait(3)
        updates.submit(repeated)
        assert engine.cancel_count == 0
        assert [state for _, state in engine.admissions] == [
            IdleScreenState.from_record(first.payload["idle_screen"])
        ] * 2
        engine.release.set()
        assert replies.response(first).payload["error_code"] == "stale_revision"
        assert replies.response(repeated).payload["applied"] is True
        assert engine.calls == [first.request_id, repeated.request_id]
    finally:
        engine.release.set()
        updates.close()
