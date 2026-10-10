"""Session-owned idle presentation shared with the supervised scene engine."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class IdleScreenState:
    """One immutable revision, independent of foreground presentation epochs."""

    revision: int = 0
    media_path: str = ""
    yeartext_image_path: str = ""
    yeartext_revision: int = 0

    def __post_init__(self) -> None:
        for value in (self.revision, self.yeartext_revision):
            if type(value) is not int or not 0 <= value < 2**64:
                raise ValueError("Invalid idle screen revision")
        for path in (self.media_path, self.yeartext_image_path):
            if (
                not isinstance(path, str)
                or len(path) > 4096
                or any(ord(character) < 32 or ord(character) == 127 for character in path)
                or "://" in path
            ):
                raise ValueError("Invalid idle screen local path")

    def to_record(self) -> dict[str, object]:
        return {
            "revision": self.revision,
            "media_path": self.media_path,
            "yeartext_image_path": self.yeartext_image_path,
            "yeartext_revision": self.yeartext_revision,
        }

    @classmethod
    def from_record(cls, raw: object) -> IdleScreenState:
        if not isinstance(raw, dict) or set(raw) != {
            "revision", "media_path", "yeartext_image_path", "yeartext_revision",
        }:
            raise ValueError("Invalid idle screen state fields")
        return cls(**raw)
