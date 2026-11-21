"""Validation and bounds for the private JSON-lines automation transport."""

from __future__ import annotations

import re

from .sequences import SEQUENCES
from .state import (
    AudioState,
    ParticipantAction,
    ParticipantActionResult,
    ParticipantStepResult,
    ParticipantStepStatus,
    RaisedHand,
    RaisedHandsSnapshot,
    RaisedHandsStatus,
    StepFailure,
)

RAISED_HANDS = "raised_hands"
PARTICIPANT_ACTIONS = frozenset(action.value for action in ParticipantAction)
_COMMANDS = SEQUENCES | PARTICIPANT_ACTIONS | {"meeting", "inspect", RAISED_HANDS}
MAX_MESSAGE_BYTES = 64 * 1024
PROTOCOL_VERSION = 2


def valid_reference(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{32}", value) is not None


def valid_request(request: object) -> bool:
    if not isinstance(request, dict):
        return False
    request_id, command = request.get("id"), request.get("command")
    if type(request_id) is not int or request_id <= 0:
        return False
    if not isinstance(command, str) or command not in _COMMANDS:
        return False
    fields = {"id", "command"}
    if command == RAISED_HANDS:
        return set(request) == fields | {"open_panel"} and type(request["open_panel"]) is bool
    if command in PARTICIPANT_ACTIONS:
        return set(request) == fields | {"reference"} and valid_reference(request["reference"])
    return set(request) == fields


def parse_snapshot(value: dict) -> RaisedHandsSnapshot:
    if not isinstance(value, dict) or set(value) != {"status", "hands", "chronological", "error"}:
        raise ValueError("Invalid raised hands snapshot")
    status = RaisedHandsStatus(value["status"])
    chronological = value["chronological"]
    error = value["error"]
    hands = value["hands"]
    if (
        type(chronological) is not bool
        or (error is not None and not isinstance(error, str))
        or not isinstance(hands, list)
        or (hands and status not in (RaisedHandsStatus.READY, RaisedHandsStatus.PARTIAL))
        or (chronological and status is not RaisedHandsStatus.READY)
    ):
        raise ValueError("Invalid raised hands snapshot state")
    parsed = []
    references = set()
    for hand in hands:
        if not isinstance(hand, dict) or set(hand) != {"name", "audio", "reference"}:
            raise ValueError("Invalid raised hand")
        name, reference = hand["name"], hand["reference"]
        if not isinstance(name, str) or not name:
            raise ValueError("Invalid participant name")
        if reference is not None:
            if (
                status is not RaisedHandsStatus.READY
                or not valid_reference(reference)
                or reference in references
            ):
                raise ValueError("Invalid participant reference")
            references.add(reference)
        parsed.append(RaisedHand(name, AudioState(hand["audio"]), reference))
    return RaisedHandsSnapshot(status, tuple(parsed), chronological, error)


def parse_participant_step(
    value: dict, action: ParticipantAction, index: int
) -> ParticipantStepResult:
    if (
        not isinstance(value, dict)
        or set(value) != {"step", "status"}
        or index >= len(action.steps)
        or value["step"] != action.steps[index]
    ):
        raise ValueError("Invalid participant step")
    step = action.steps[index]
    status = ParticipantStepStatus(value["status"])
    if (status is ParticipantStepStatus.REQUESTED and step != "request_unmute") or (
        status is ParticipantStepStatus.CHANGED and step == "request_unmute"
    ):
        raise ValueError("Invalid participant step status")
    return ParticipantStepResult(step, status)


def parse_participant_result(
    value: dict, action: ParticipantAction, reference: str
) -> ParticipantActionResult:
    if (
        not isinstance(value, dict)
        or set(value) != {"action", "reference", "completed_steps", "failure", "cancelled"}
        or value["action"] != action
        or value["reference"] != reference
        or value["cancelled"] is not False
        or not isinstance(value["completed_steps"], list)
    ):
        raise ValueError("Invalid participant action result")
    steps = tuple(
        parse_participant_step(step, action, index)
        for index, step in enumerate(value["completed_steps"])
    )
    failed = value["failure"]
    if failed is None:
        if len(steps) != len(action.steps):
            raise ValueError("Incomplete successful participant action")
        return ParticipantActionResult(action, reference, steps)
    if (
        not isinstance(failed, dict)
        or len(steps) >= len(action.steps)
        or failed.get("step") != action.steps[len(steps)]
        or not isinstance(failed.get("code"), str)
    ):
        raise ValueError("Invalid participant action failure")
    return ParticipantActionResult(
        action, reference, steps, StepFailure(failed["step"], failed["code"])
    )
