"""Independent policies and commands for prepared meeting content."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


@dataclass(frozen=True, slots=True)
class MeetingContentPolicy:
    keep_images_for_manual_cleanup: bool = False
    auto_recover_images: bool = True
    auto_update: bool = True

    @classmethod
    def from_defaults(cls) -> MeetingContentPolicy:
        from solin.core.foundation import constants

        return cls(
            keep_images_for_manual_cleanup=constants.KEEP_MEETING_IMAGES_FOR_MANUAL_CLEANUP,
            auto_recover_images=constants.AUTO_RECOVER_PREPARED_MEETING_IMAGES,
            auto_update=constants.AUTO_UPDATE_PREPARED_MEETINGS,
        )


class MeetingContentOperation(StrEnum):
    PREPARE = "prepare"
    CHECK = "check"
    UPDATE = "update"
    REPAIR = "repair"


class MeetingRequestOrigin(StrEnum):
    MANUAL = "manual"
    AUTOMATIC = "automatic"
    MAINTENANCE = "maintenance"


class MeetingContentError(StrEnum):
    IMAGE_REVISION_UNAVAILABLE = "image_revision_unavailable"
    LEGACY_REVISION_UNVERIFIABLE = "legacy_revision_unverifiable"


@dataclass(frozen=True, slots=True)
class PublicationContentRequest:
    """Worker command. A check never authorizes materialization or download."""

    publications: frozenset[str] = frozenset({"mwb", "wt"})
    operation: MeetingContentOperation = MeetingContentOperation.PREPARE
    dependencies: dict[str, list[dict[str, str]]] = field(default_factory=dict)
    repair_refs: tuple[dict[str, Any], ...] = ()


def publication_dependency(pub: str, language: str, issue: str, checksum: str) -> dict[str, str]:
    return {"pub": pub, "language": language, "issue": issue, "checksum": checksum}
