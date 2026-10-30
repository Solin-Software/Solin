"""Portable, visible folder components and platform-independent identity."""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum
import unicodedata


class FolderNameIssue(StrEnum):
    EMPTY = "empty"
    WHITESPACE = "whitespace"
    CHARACTERS = "characters"
    RESERVED = "reserved"
    HIDDEN = "hidden"
    TRAILING_DOT = "trailing_dot"
    TOO_LONG = "too_long"
    CONFLICT = "conflict"


class FolderNameError(ValueError):
    def __init__(self, name: object, issue: FolderNameIssue) -> None:
        self.issue = issue
        super().__init__(f"Invalid folder name: {name!r} ({issue.value})")


def folder_name_key(name: str) -> str:
    """Compare canonically equivalent names without filesystem case rules."""
    return unicodedata.normalize("NFC", unicodedata.normalize("NFC", name).casefold())


def normalize_portable_folder_name(value: object) -> str:
    """Normalize NFC without trimming, replacing, or truncating user input.

    Use the common Windows/macOS/Linux component constraints, with dot-prefixed
    names excluded because linked-folder scanners treat them as internal data.
    """
    if not isinstance(value, str) or not value:
        raise FolderNameError(value, FolderNameIssue.EMPTY)
    name = unicodedata.normalize("NFC", value)
    if name != name.strip():
        raise FolderNameError(value, FolderNameIssue.WHITESPACE)
    if any(char in '<>:"/\\|?*' or ord(char) < 32 or ord(char) == 127 for char in name):
        raise FolderNameError(value, FolderNameIssue.CHARACTERS)
    if name in {".", ".."}:
        raise FolderNameError(value, FolderNameIssue.RESERVED)
    if name.startswith("."):
        raise FolderNameError(value, FolderNameIssue.HIDDEN)
    if name.endswith("."):
        raise FolderNameError(value, FolderNameIssue.TRAILING_DOT)
    basename = name.partition(".")[0].rstrip(" ").upper()
    if basename in {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$", "CLOCK$"} or (
        len(basename) == 4 and basename[:3] in {"COM", "LPT"} and basename[3] in "123456789¹²³"
    ):
        raise FolderNameError(value, FolderNameIssue.RESERVED)
    try:
        utf8_size = len(name.encode("utf-8"))
        utf16_size = len(name.encode("utf-16-le")) // 2
    except UnicodeEncodeError as exc:
        raise FolderNameError(value, FolderNameIssue.CHARACTERS) from exc
    if utf8_size > 255 or utf16_size > 255:
        raise FolderNameError(value, FolderNameIssue.TOO_LONG)
    return name


def ensure_available_folder_name(
    value: object,
    existing_names: Iterable[str],
    *,
    excluding_name: str | None = None,
) -> str:
    """Validate one component against a snapshot of all sibling entries."""
    name = normalize_portable_folder_name(value)
    key = folder_name_key(name)
    if any(
        sibling != excluding_name and folder_name_key(sibling) == key for sibling in existing_names
    ):
        raise FolderNameError(value, FolderNameIssue.CONFLICT)
    return name
