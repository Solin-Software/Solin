"""Pure automatic shortcut action model and serialization."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass

EVENT_MEDIA_STARTED = "media_started"
EVENT_MEDIA_ENDED = "media_ended"
EVENT_MEDIA_PAUSED = "media_paused"
EVENT_MEDIA_RESUMED = "media_resumed"

AUTO_KEY_EVENTS = (
    EVENT_MEDIA_STARTED,
    EVENT_MEDIA_ENDED,
    EVENT_MEDIA_PAUSED,
    EVENT_MEDIA_RESUMED,
)


@dataclass(frozen=True)
class AutoKeyAction:
    id: str
    event: str
    sequence: str
    enabled: bool = True

    @classmethod
    def from_dict(cls, data: dict) -> AutoKeyAction | None:
        if not isinstance(data, dict):
            return None
        event = str(data.get("event") or "")
        sequence = str(data.get("sequence") or "").split(",", 1)[0].strip()
        if event not in AUTO_KEY_EVENTS or not sequence:
            return None
        return cls(
            id=str(data.get("id") or uuid.uuid4().hex),
            event=event,
            sequence=sequence,
            enabled=bool(data.get("enabled", True)),
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "event": self.event,
            "sequence": self.sequence,
            "enabled": self.enabled,
        }


def parse_actions(raw: object) -> list[AutoKeyAction]:
    try:
        parsed = json.loads(str(raw or "[]"))
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(parsed, list):
        return []
    actions: list[AutoKeyAction] = []
    for item in parsed:
        action = AutoKeyAction.from_dict(item)
        if action is not None:
            actions.append(action)
    return actions


def serialize_actions(actions: list[AutoKeyAction]) -> str:
    return json.dumps([action.to_dict() for action in actions])


__all__ = [
    "AUTO_KEY_EVENTS",
    "EVENT_MEDIA_ENDED",
    "EVENT_MEDIA_PAUSED",
    "EVENT_MEDIA_RESUMED",
    "EVENT_MEDIA_STARTED",
    "AutoKeyAction",
    "parse_actions",
    "serialize_actions",
]
