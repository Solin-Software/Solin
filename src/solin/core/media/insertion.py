"""Shared result contract for media insertions."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class MediaInsertResult:
    added_items: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)
    duplicate_items: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)
    target_valid: bool = True

    @property
    def added_count(self) -> int:
        return len(self.added_items)

    @property
    def duplicate_count(self) -> int:
        return len(self.duplicate_items)


__all__ = ["MediaInsertResult"]
