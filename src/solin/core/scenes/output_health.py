"""Output delivery health, independent of requested activation and scene routing."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


OUTPUT_FRAME_TIMEOUT_S = 2.0


class OutputHealthStatus(StrEnum):
    STOPPED = "stopped"
    STARTING = "starting"
    READY = "ready"
    RECOVERING = "recovering"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class OutputHealth:
    status: OutputHealthStatus = OutputHealthStatus.STOPPED
    error_code: str = ""
    generation: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.status, OutputHealthStatus):
            raise ValueError("Invalid output health status")
        if (
            not isinstance(self.error_code, str)
            or len(self.error_code) > 128
            or any(ord(character) < 32 or ord(character) == 127 for character in self.error_code)
            or self.error_code != self.error_code.strip()
        ):
            raise ValueError("Invalid output health error code")
        if type(self.generation) is not int or not 0 <= self.generation <= 2**63 - 1:
            raise ValueError("Invalid output health generation")
        if self.status is OutputHealthStatus.FAILED and not self.error_code:
            raise ValueError("Failed output health requires an error code")
        if self.error_code and self.status not in {
            OutputHealthStatus.RECOVERING,
            OutputHealthStatus.FAILED,
        }:
            raise ValueError("Only recovering or failed output health can contain an error code")

    def to_record(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "error_code": self.error_code,
            "generation": self.generation,
        }

    @classmethod
    def from_record(cls, raw: object) -> OutputHealth:
        if not isinstance(raw, dict) or set(raw) != {"status", "error_code", "generation"}:
            raise ValueError("Invalid output health fields")
        if not isinstance(raw["status"], str):
            raise ValueError("Invalid output health status")
        return cls(
            status=OutputHealthStatus(raw["status"]),
            error_code=raw["error_code"],
            generation=raw["generation"],
        )
