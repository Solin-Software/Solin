from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ProjectionResultStatus(StrEnum):
    ACCEPTED = "accepted"
    BLOCKED = "blocked"
    INVALID = "invalid"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ProjectionResult:
    status: ProjectionResultStatus
    message: str = ""

    @property
    def accepted(self) -> bool:
        return self.status is ProjectionResultStatus.ACCEPTED


__all__ = ["ProjectionResult", "ProjectionResultStatus"]
