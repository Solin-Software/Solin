"""Pure remote update version and eligibility rules."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum

from .urls import is_safe_remote_url

_VERSION_PATTERN = re.compile(r"^\d+(?:\.\d+){1,3}$")


@dataclass(frozen=True, slots=True, order=True)
class ReleaseVersion:
    components: tuple[int, int, int, int]
    source_component_count: int = field(default=4, compare=False, repr=False)

    def __post_init__(self) -> None:
        if len(self.components) != 4 or any(part < 0 for part in self.components):
            raise ValueError("release versions require four non-negative components")
        if not 2 <= self.source_component_count <= 4:
            raise ValueError("release versions require two to four source components")

    @classmethod
    def parse(cls, raw: object) -> ReleaseVersion | None:
        text = str(raw or "").strip()
        if not _VERSION_PATTERN.fullmatch(text):
            return None
        try:
            parts = [int(part) for part in text.split(".")]
        except ValueError:
            return None
        parts.extend([0] * (4 - len(parts)))
        return cls(
            (parts[0], parts[1], parts[2], parts[3]),
            source_component_count=len(text.split(".")),
        )

    def __str__(self) -> str:
        return ".".join(str(part) for part in self.components)

    @property
    def display_version(self) -> str:
        """Match the website's public version formatting without losing precision."""
        count = self.source_component_count
        if self.components[0] != 1 and count >= 4:
            count = 3
        return ".".join(str(part) for part in self.components[:count])


class UpdateKind(StrEnum):
    SETUP = "setup"
    PATCH = "patch"


@dataclass(frozen=True, slots=True)
class ReleaseNote:
    version: ReleaseVersion
    markdown: str
    language: str

    def __post_init__(self) -> None:
        if not self.markdown.strip():
            raise ValueError("release-note markdown cannot be empty")
        if (
            not self.language
            or len(self.language) > 8
            or not self.language.isascii()
            or not self.language.isalnum()
        ):
            raise ValueError("release-note language must be an API code")


@dataclass(frozen=True, slots=True)
class UpdateInfo:
    kind: UpdateKind
    version: ReleaseVersion
    url: str
    changelog: tuple[ReleaseNote, ...] = ()

    def __post_init__(self) -> None:
        if not is_safe_update_url(self.url):
            raise ValueError("update URL must use HTTPS or local development HTTP")


def is_safe_update_url(url: str) -> bool:
    return is_safe_remote_url(url)


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

    selected: tuple[UpdateKind, ReleaseVersion, str] | None = None

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
            selected = (UpdateKind.PATCH, candidate, url)

    setup = payload.get("setup")
    if selected is None and isinstance(setup, dict):
        candidate = ReleaseVersion.parse(setup.get("version"))
        url = str(setup.get("url") or "").strip()
        if candidate is not None and candidate > current and is_safe_update_url(url):
            selected = (UpdateKind.SETUP, candidate, url)

    if selected is None:
        return None

    kind, version, url = selected
    return UpdateInfo(
        kind,
        version,
        url,
        _parse_release_notes(
            payload.get("changelog"),
            current=current,
            target=version,
        ),
    )


def _parse_release_notes(
    value: object,
    *,
    current: ReleaseVersion,
    target: ReleaseVersion,
) -> tuple[ReleaseNote, ...]:
    if not isinstance(value, dict):
        return ()
    entries = value.get("entries")
    if not isinstance(entries, list):
        return ()

    by_version: dict[ReleaseVersion, ReleaseNote] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        version = ReleaseVersion.parse(entry.get("version"))
        markdown = entry.get("markdown")
        language = str(entry.get("language") or "").strip().upper()
        if (
            version is None
            or not current < version <= target
            or not isinstance(markdown, str)
            or not markdown.strip()
            or not language
            or len(language) > 8
            or not language.isascii()
            or not language.isalnum()
        ):
            continue
        by_version.setdefault(
            version,
            ReleaseNote(version, markdown.strip(), language),
        )

    return tuple(by_version[version] for version in sorted(by_version, reverse=True))
