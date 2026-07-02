"""Canonical media identity and duplicate-partitioning policy."""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from solin.core.jw.identifiers import is_jw_url
from solin.core.media.jw_reference import parse_jw_media_reference


_JW_LANGUAGE_RE = re.compile(
    r"(?:pub-)?[A-Za-z0-9]+_([A-Z]{1,4})_",
    re.IGNORECASE,
)


def _integer(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _text(value: Any) -> str:
    return str(value or "").strip()


def _first_text(record: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = _text(record.get(key))
        if value:
            return value
    return ""


def _first_integer(record: Mapping[str, Any], *keys: str) -> int:
    for key in keys:
        value = _integer(record.get(key))
        if value:
            return value
    return 0


def _normalized_location(value: str) -> str:
    location = value.strip()
    if not location:
        return ""
    if location.startswith(("http://", "https://")):
        parsed = urlsplit(location)
        query = "" if is_jw_url(location) else parsed.query
        return urlunsplit(
            (
                parsed.scheme.lower(),
                parsed.netloc.lower().rstrip("."),
                parsed.path,
                query,
                "",
            )
        )
    return os.path.normcase(os.path.abspath(os.path.expanduser(location)))


@dataclass(frozen=True, slots=True)
class MediaIdentity:
    """Comparable identity extracted from playlist, meeting, or JW picker data."""

    source_id: str = ""
    key_symbol: str = ""
    doc_id: int = 0
    issue_tag: int = 0
    track: int = 0
    meps_language: int = 0
    language_code: str = ""
    location: str = ""

    def _jw_key(self) -> tuple[str, int, int, int] | None:
        if self.doc_id:
            return ("doc", self.doc_id, self.issue_tag, self.track)
        if self.key_symbol and (self.issue_tag or self.track):
            return (self.key_symbol, 0, self.issue_tag, self.track)
        return None

    def matches(self, other: MediaIdentity) -> bool:
        if self.source_id and other.source_id:
            if self.source_id != other.source_id:
                return False
            if (
                self.language_code
                and other.language_code
                and self.language_code != other.language_code
            ):
                return False
            if (
                self.meps_language
                and other.meps_language
                and self.meps_language != other.meps_language
            ):
                return False
            return True
        own_jw = self._jw_key()
        other_jw = other._jw_key()
        if own_jw is not None and own_jw == other_jw:
            if (
                self.language_code
                and other.language_code
                and self.language_code != other.language_code
            ):
                return False
            if (
                self.meps_language
                and other.meps_language
                and self.meps_language != other.meps_language
            ):
                return False
            return True
        return bool(self.location and self.location == other.location)

    @property
    def dedupe_token(self) -> str:
        raw = repr(
            (
                self.source_id,
                self._jw_key(),
                self.meps_language,
                self.language_code,
                self.location,
            )
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class MediaPartition:
    unique_items: tuple[Mapping[str, Any], ...]
    duplicate_items: tuple[Mapping[str, Any], ...]


def media_identity(record: Mapping[str, Any]) -> MediaIdentity | None:
    location = _first_text(record, "url", "download_url", "file_path", "jworg_url")
    parsed = parse_jw_media_reference(
        location,
        original_filename=_first_text(record, "original_filename"),
    ) or {}
    language_code = _first_text(record, "language", "language_code", "api_code").upper()
    if not language_code:
        match = _JW_LANGUAGE_RE.search(urlsplit(location).path)
        if match:
            language_code = match.group(1).upper()
    source_id = _first_text(record, "jw_media_id", "natural_key", "guid")
    if not source_id and record.get("download_url") and record.get("source"):
        source_id = _text(record.get("id"))
    identity = MediaIdentity(
        source_id=source_id,
        key_symbol=(
            _first_text(record, "key_symbol", "pub")
            or _text(parsed.get("key_symbol"))
        ).lower(),
        doc_id=(
            _first_integer(record, "doc_id", "meps_doc_id", "docid")
            or _integer(parsed.get("doc_id"))
        ),
        issue_tag=(
            _first_integer(record, "issue_tag", "issue")
            or _integer(parsed.get("issue_tag"))
        ),
        track=_first_integer(record, "track") or _integer(parsed.get("track")),
        meps_language=(
            _first_integer(record, "meps_language")
            or _integer(parsed.get("meps_language"))
        ),
        language_code=language_code,
        location=_normalized_location(location),
    )
    if not (identity.source_id or identity._jw_key() or identity.location):
        return None
    return identity


def same_media(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    left_identity = media_identity(left)
    right_identity = media_identity(right)
    return bool(
        left_identity is not None
        and right_identity is not None
        and left_identity.matches(right_identity)
    )


def contains_media(
    existing_items: Sequence[Mapping[str, Any]],
    candidate: Mapping[str, Any],
) -> bool:
    candidate_identity = media_identity(candidate)
    if candidate_identity is None:
        return False
    return any(
        existing_identity is not None
        and existing_identity.matches(candidate_identity)
        for existing_identity in (media_identity(item) for item in existing_items)
    )


def partition_media_items(
    existing_items: Sequence[Mapping[str, Any]],
    candidates: Sequence[Mapping[str, Any]],
) -> MediaPartition:
    accepted: list[Mapping[str, Any]] = []
    duplicates: list[Mapping[str, Any]] = []
    identities = [
        identity
        for identity in (media_identity(item) for item in existing_items)
        if identity is not None
    ]
    for candidate in candidates:
        identity = media_identity(candidate)
        if identity is not None and any(existing.matches(identity) for existing in identities):
            duplicates.append(candidate)
            continue
        accepted.append(candidate)
        if identity is not None:
            identities.append(identity)
    return MediaPartition(tuple(accepted), tuple(duplicates))


__all__ = [
    "MediaIdentity",
    "MediaPartition",
    "contains_media",
    "media_identity",
    "partition_media_items",
    "same_media",
]
