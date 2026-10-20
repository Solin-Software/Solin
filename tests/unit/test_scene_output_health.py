from __future__ import annotations

from concurrent.futures import Future
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
import time

import pytest

from solin.core.scenes.engine import OutputHealthEvent
from solin.core.scenes.ipc_protocol import SceneIpcEnvelope, SceneIpcMessageError
from solin.core.scenes.model import BusId
from solin.core.scenes.output_health import OutputHealth, OutputHealthStatus
from solin.core.scenes.process_engine import (
    SceneEngineProcessConfig,
    SubprocessSceneEngine,
    _PendingRequest,
    _ack_from_envelope,
    _output_health_from_envelope,
)


@pytest.mark.parametrize("status", list(OutputHealthStatus))
def test_output_health_round_trips_immutable_typed_state(status):
    state = OutputHealth(
        status,
        "virtual_camera_unavailable"
        if status
        in {
            OutputHealthStatus.RECOVERING,
            OutputHealthStatus.FAILED,
        }
        else "",
        2**63 - 1,
    )
    assert OutputHealth.from_record(state.to_record()) == state
    assert OutputHealth().status is OutputHealthStatus.STOPPED
    with pytest.raises(FrozenInstanceError):
        state.generation = 7


@pytest.mark.parametrize(
    "values",
    [
        {"status": "ready"},
        {"status": None},
        {"generation": True},
        {"generation": -1},
        {"generation": 2**63},
        {"generation": 1.0},
        {"error_code": None},
        {"status": OutputHealthStatus.RECOVERING, "error_code": "x" * 129},
        {"status": OutputHealthStatus.FAILED},
        {"status": OutputHealthStatus.READY, "error_code": "unavailable"},
        {"status": OutputHealthStatus.STARTING, "error_code": "unavailable"},
        {"status": OutputHealthStatus.STOPPED, "error_code": "unavailable"},
        {"status": OutputHealthStatus.RECOVERING, "error_code": "bad\ncode"},
        {"status": OutputHealthStatus.RECOVERING, "error_code": "bad\x7fcode"},
        {"status": OutputHealthStatus.RECOVERING, "error_code": " "},
    ],
)
def test_output_health_rejects_invalid_values(values):
    with pytest.raises(ValueError):
        OutputHealth(**values)


@pytest.mark.parametrize(
    "raw",
    [
        None,
        [],
        {},
        {"status": "ready", "generation": 1},
        {"status": "ready", "generation": 1, "error_code": "", "message": "debug"},
        {"status": "unknown", "generation": 1, "error_code": ""},
        {"status": [], "generation": 1, "error_code": ""},
    ],
)
def test_output_health_record_requires_exact_fields_and_valid_status(raw):
    with pytest.raises(ValueError):
        OutputHealth.from_record(raw)


def _envelope(**changes):
    return replace(
        SceneIpcEnvelope(
            message_type="output_health",
            request_id="output-request",
            session_id="output-session",
            process_generation="process-3",
            sequence=42,
            document_revision=7,
            deadline_monotonic_ms=3000,
            payload={
                "bus_id": "virtual_camera",
                **OutputHealth(
                    OutputHealthStatus.RECOVERING,
                    "virtual_camera_unavailable",
                    3,
                ).to_record(),
            },
        ),
        **changes,
    )


def test_output_health_parser_preserves_envelope_scope_and_request_sequence():
    envelope = _envelope()
    event = _output_health_from_envelope(envelope)
    assert event == OutputHealthEvent(
        BusId.VIRTUAL_CAMERA,
        OutputHealth(OutputHealthStatus.RECOVERING, "virtual_camera_unavailable", 3),
        envelope.session_id,
        envelope.process_generation,
        envelope.sequence,
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("bus_id", "unknown"),
        ("bus_id", []),
        ("status", "unknown"),
        ("status", None),
        ("generation", True),
        ("generation", -1),
        ("error_code", None),
        ("extra", "debug"),
    ],
)
def test_output_health_parser_rejects_malformed_payload(field, value):
    envelope = _envelope()
    with pytest.raises(SceneIpcMessageError):
        _output_health_from_envelope(replace(envelope, payload={**envelope.payload, field: value}))


@pytest.mark.parametrize("field", ["bus_id", "status", "error_code", "generation"])
def test_output_health_parser_rejects_missing_fields(field):
    envelope = _envelope()
    payload = dict(envelope.payload)
    del payload[field]
    with pytest.raises(SceneIpcMessageError):
        _output_health_from_envelope(replace(envelope, payload=payload))


@pytest.mark.parametrize(
    "changes",
    [
        {"bus_id": "virtual_camera"},
        {"state": {}},
        {"session_id": ""},
        {"process_generation": ""},
        {"sequence": True},
        {"sequence": -1},
    ],
)
def test_output_health_event_rejects_invalid_scope_or_state(changes):
    with pytest.raises(ValueError):
        replace(_output_health_from_envelope(_envelope()), **changes)


@pytest.mark.parametrize("health_before_ack", [True, False])
def test_output_health_delivery_does_not_consume_or_require_pending_ack(health_before_ack):
    envelope = _envelope()
    engine = SubprocessSceneEngine(SceneEngineProcessConfig(executable=Path("unused")))
    engine._session_id = envelope.session_id
    engine._process_generation = envelope.process_generation
    events = []
    engine.subscribe(events.append)
    future = Future()
    engine._pending[envelope.request_id] = _PendingRequest(
        expected_message_type="ack",
        session_id=envelope.session_id,
        process_generation=envelope.process_generation,
        sequence=envelope.sequence,
        document_revision=envelope.document_revision,
        deadline_monotonic_ms=envelope.deadline_monotonic_ms,
        started_monotonic=time.monotonic(),
        is_liveness_probe=False,
        converter=_ack_from_envelope,
        future=future,
    )
    ack = replace(
        envelope,
        message_type="ack",
        payload={
            "applied": True,
            "error_code": "",
            "error_message": "",
        },
    )
    if health_before_ack:
        engine._receive(envelope, envelope.process_generation)
        assert not future.done()
    engine._receive(ack, ack.process_generation)
    if not health_before_ack:
        engine._receive(envelope, envelope.process_generation)

    assert future.result().applied
    assert events == [_output_health_from_envelope(envelope)]
    assert not engine._generation_failed.is_set()


@pytest.mark.parametrize(
    "changes,reader",
    [
        ({"session_id": "old-session"}, "process-3"),
        ({"process_generation": "old-process"}, "process-3"),
        ({}, "old-reader"),
    ],
)
def test_output_health_receive_ignores_retired_processes(changes, reader):
    envelope = _envelope()
    engine = SubprocessSceneEngine(SceneEngineProcessConfig(executable=Path("unused")))
    engine._session_id = envelope.session_id
    engine._process_generation = envelope.process_generation
    events = []
    engine.subscribe(events.append)
    engine._receive(replace(envelope, **changes), reader)
    assert events == []
