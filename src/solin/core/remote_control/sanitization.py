"""Sanitize desktop-only text before it crosses the remote-control boundary."""

from __future__ import annotations

import re
from typing import Final


MAX_PUBLIC_TEXT_LENGTH: Final = 4_096

_WINDOWS_ABSOLUTE_PATH: Final = re.compile(r"^[A-Za-z]:[\\/]")
_WINDOWS_PATH_IN_TEXT: Final = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/]")
_POSIX_PATH_IN_TEXT: Final = re.compile(r"(?<![A-Za-z0-9])/(?:[^\s/]+/)+[^\s]*")


def looks_private_location(value: str) -> bool:
    """Return whether text appears to contain a private path or URL."""

    normalized = value.strip()
    lowered = normalized.casefold()
    return bool(
        _WINDOWS_ABSOLUTE_PATH.match(normalized)
        or _WINDOWS_PATH_IN_TEXT.search(normalized)
        or _POSIX_PATH_IN_TEXT.search(normalized)
        or normalized.startswith(("/", "\\\\"))
        or "\\\\" in normalized
        or "../" in normalized
        or "..\\" in normalized
        or "://" in normalized
        or lowered.startswith(("file:", "smb:", "ftp:"))
        or any(ord(character) < 32 for character in normalized)
    )


def public_title(value: object, fallback: str) -> str:
    """Return display text that cannot disclose a private media location."""

    title = str(value or "").strip()
    if not title or looks_private_location(title):
        title = fallback
    return title[:MAX_PUBLIC_TEXT_LENGTH]


def public_subtitle(value: object) -> str:
    subtitle = str(value or "").strip()
    return (
        ""
        if not subtitle or looks_private_location(subtitle)
        else subtitle[:MAX_PUBLIC_TEXT_LENGTH]
    )


def public_reason(value: object, *, fallback: str = "Unavailable") -> str | None:
    if value is None:
        return None
    reason = str(value).strip()
    if not reason:
        return None
    if looks_private_location(reason):
        return fallback
    return reason[:MAX_PUBLIC_TEXT_LENGTH]
