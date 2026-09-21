"""Immutable presence markers for explicitly activated linked documents."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any

from solin.core.storage.binary_files import publish_bytes_immutable


VERSION = 1
_NAMESPACE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,99}$")
_DOCUMENT_ID = re.compile(r"^[a-f0-9]{64}$")


class ActivationError(ValueError):
    """An activation marker exists but cannot safely identify a document."""


class ActivationConflict(ActivationError):
    """Another immutable activation won the same folder."""


@dataclass(frozen=True, slots=True)
class DocumentActivation:
    namespace: str
    document_id: str
    subject: dict[str, str]


class ActivationStore:
    """Use one immutable file as the authority for an active document epoch."""

    def __init__(self, folder: Path, namespace: str) -> None:
        if not _NAMESPACE.fullmatch(namespace):
            raise ValueError("Invalid activation namespace")
        self.folder = Path(folder)
        self.namespace = namespace
        self.path = self.folder / ".solin_sync" / namespace / "active.json"

    def read(self) -> DocumentActivation | None:
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return None
        try:
            value = json.loads(raw)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ActivationError("Incomplete activation marker") from exc
        if not isinstance(value, dict):
            raise ActivationError("Activation marker must be an object")
        if value.get("version") != VERSION or value.get("namespace") != self.namespace:
            raise ActivationError("Unsupported activation marker")
        document_id = value.get("document_id")
        subject = value.get("subject")
        if not isinstance(document_id, str) or not _DOCUMENT_ID.fullmatch(document_id):
            raise ActivationError("Invalid activation document identity")
        if (
            not isinstance(subject, dict)
            or any(not isinstance(key, str) or not isinstance(item, str)
                   for key, item in subject.items())
        ):
            raise ActivationError("Invalid activation subject")
        return DocumentActivation(self.namespace, document_id, dict(subject))

    def publish(self, document_id: str, subject: dict[str, str]) -> DocumentActivation:
        if not _DOCUMENT_ID.fullmatch(document_id):
            raise ValueError("Invalid activation document identity")
        if any(not isinstance(key, str) or not isinstance(value, str)
               for key, value in subject.items()):
            raise TypeError("Activation subject values must be strings")
        value: dict[str, Any] = {
            "version": VERSION,
            "namespace": self.namespace,
            "document_id": document_id,
            "subject": dict(sorted(subject.items())),
        }
        payload = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        try:
            publish_bytes_immutable(self.path, payload)
        except FileExistsError as exc:
            raise ActivationConflict("Another document is already active") from exc
        return DocumentActivation(self.namespace, document_id, dict(subject))

    def remove(self, expected_document_id: str) -> bool:
        current = self.read()
        if current is None:
            return False
        if current.document_id != expected_document_id:
            raise ActivationConflict("The active document changed before deactivation")
        self.path.unlink()
        return True
