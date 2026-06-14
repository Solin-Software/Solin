"""Pure remote update version and eligibility rules."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlsplit

_VERSION_PATTERN = re.compile(r"^\d+(?:\.\d+){1,3}$")
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


@dataclass(frozen=True, slots=True, order=True)
class ReleaseVersion:
    components: tuple[int, int, int, int]

    def __post_init__(self) -> None:
        if len(self.components) != 4 or any(part < 0 for part in self.components):
            raise ValueError("release versions require four non-negative components")

    @classmethod
    def parse(cls, raw: object) -> ReleaseVersion | None:
        text = str(raw or "").strip()
        if not _VERSION_PATTERN.fullmatch(text):
            return None
        parts = [int(part) for part in text.split(".")]
        parts.extend([0] * (4 - len(parts)))
        return cls((parts[0], parts[1], parts[2], parts[3]))

    def __str__(self) -> str:
        return ".".join(str(part) for part in self.components)


class UpdateKind(StrEnum):
    SETUP = "setup"
    PATCH = "patch"


@dataclass(frozen=True, slots=True)
class UpdateInfo:
    kind: UpdateKind
    version: ReleaseVersion
    url: str

    def __post_init__(self) -> None:
        if not is_safe_update_url(self.url):
            raise ValueError("update URL must use HTTPS or local development HTTP")


def is_safe_update_url(url: str) -> bool:
    parsed = urlsplit(url)
    hostname = (parsed.hostname or "").lower()
    if not hostname:
        return False
    return parsed.scheme == "https" or (parsed.scheme == "http" and hostname in _LOCAL_HOSTS)


def evaluate_update(
    payload: object,
    *,
    current_version: str | ReleaseVersion,
) -> UpdateInfo | None:
    """Select the eligible patch first, then fall back to a full setup."""
    if not isinstance(payload, dict):
        return None
    current = (
        current_version
        if isinstance(current_version, ReleaseVersion)
        else ReleaseVersion.parse(current_version)
    )
    if current is None:
        return None

    patch = payload.get("patch")
    if isinstance(patch, dict):
        candidate = ReleaseVersion.parse(patch.get("version"))
        minimum = ReleaseVersion.parse(patch.get("min_version", "0.0"))
        url = str(patch.get("url") or "").strip()
        if (
            candidate is not None
            and minimum is not None
            and candidate > current
            and current >= minimum
            and is_safe_update_url(url)
        ):
            return UpdateInfo(UpdateKind.PATCH, candidate, url)

    setup = payload.get("setup")
    if isinstance(setup, dict):
        candidate = ReleaseVersion.parse(setup.get("version"))
        url = str(setup.get("url") or "").strip()
        if candidate is not None and candidate > current and is_safe_update_url(url):
            return UpdateInfo(UpdateKind.SETUP, candidate, url)

    return None
