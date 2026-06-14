"""Framework-independent profile entities and validation."""

from __future__ import annotations

import math
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass, field

_PROFILE_ID_PATTERN = re.compile(r"^[\w-]+$", re.UNICODE)
_WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{number}" for number in range(1, 10)}
    | {f"LPT{number}" for number in range(1, 10)}
)


def normalize_profile_name(name: str) -> str:
    normalized = name.strip()
    if not normalized:
        raise ValueError("Profile name cannot be empty.")
    if any(ord(character) < 32 for character in normalized):
        raise ValueError("Profile name cannot contain control characters.")
    return normalized


def validate_profile_id(profile_id: str) -> str:
    normalized = profile_id.strip()
    if not normalized:
        raise ValueError("Profile id cannot be empty.")
    if not _PROFILE_ID_PATTERN.fullmatch(normalized):
        raise ValueError("Profile id contains unsafe characters.")
    if normalized.upper() in _WINDOWS_RESERVED_NAMES:
        raise ValueError("Profile id is reserved by the operating system.")
    return normalized


def profile_slug(name: str) -> str:
    """Build a path-safe, human-readable id from a display name."""
    slug = normalize_profile_name(name).lower()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[\s_-]+", "_", slug).strip("_")
    slug = slug or "profile"
    if slug.upper() in _WINDOWS_RESERVED_NAMES:
        slug = f"profile_{slug}"
    return validate_profile_id(slug)


def unique_profile_slug(slug: str, existing: set[str]) -> str:
    candidate = validate_profile_id(slug)
    if candidate not in existing:
        return candidate
    suffix = 2
    while f"{candidate}_{suffix}" in existing:
        suffix += 1
    return f"{candidate}_{suffix}"


@dataclass(slots=True)
class ProfileInfo:
    id: str
    name: str
    created_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        self.id = validate_profile_id(self.id)
        self.name = normalize_profile_name(self.name)
        if not math.isfinite(self.created_at):
            raise ValueError("Profile creation time must be finite.")
        if self.created_at <= 0:
            self.created_at = time.time()

    def rename(self, name: str) -> None:
        self.name = normalize_profile_name(name)

    def to_dict(self) -> dict[str, str | float]:
        return {
            "id": self.id,
            "name": self.name,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "ProfileInfo":
        if not isinstance(data, Mapping):
            raise ValueError("Profile registry entry must be an object.")
        profile_id = data.get("id")
        name = data.get("name")
        if not isinstance(profile_id, str) or not isinstance(name, str):
            raise ValueError("Profile registry entry requires string id and name.")
        created_at = data.get("created_at", time.time())
        try:
            created = float(created_at)
        except (TypeError, ValueError) as exc:
            raise ValueError("Profile registry entry has an invalid creation time.") from exc
        return cls(profile_id, name, created)
