"""Canonical media identity and duplicate-partitioning policy."""

from __future__ import annotations

import hashlib
import os
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Generic, Hashable, TypeVar
from urllib.parse import urlsplit, urlunsplit

from solin.core.jw.identifiers import is_jw_url
from solin.core.media.jw_reference import parse_jw_media_reference


_JW_LANGUAGE_RE = re.compile(
    r"(?:pub-)?[A-Za-z0-9]+_([A-Z]{1,4})_",
    re.IGNORECASE,
)
_IdentityKey = TypeVar("_IdentityKey", bound=Hashable)


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
    alternate_locations: tuple[str, ...] = ()

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
        other_locations = other.locations
        return any(location in other_locations for location in self.locations)

    @property
    def locations(self) -> tuple[str, ...]:
        return tuple(
            location
            for location in (self.location, *self.alternate_locations)
            if location
        )

    @property
    def dedupe_token(self) -> str:
        raw = repr(
            (
                self.source_id,
                self._jw_key(),
                self.meps_language,
                self.language_code,
                self.locations,
            )
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


class _LanguageConstraintIndex(Generic[_IdentityKey]):
    """O(1) wildcard-compatible language constraints for authoritative IDs."""

    def __init__(self) -> None:
        self._keys: set[_IdentityKey] = set()
        self._languages: set[tuple[_IdentityKey, str]] = set()
        self._meps_languages: set[tuple[_IdentityKey, int]] = set()
        self._constraints: set[tuple[_IdentityKey, str, int]] = set()

    def add(self, key: _IdentityKey, language: str, meps_language: int) -> None:
        self._keys.add(key)
        self._languages.add((key, language))
        self._meps_languages.add((key, meps_language))
        self._constraints.add((key, language, meps_language))

    def matches(self, key: _IdentityKey, language: str, meps_language: int) -> bool:
        if key not in self._keys:
            return False
        if not language and not meps_language:
            return True
        if not language:
            return (
                (key, 0) in self._meps_languages
                or (key, meps_language) in self._meps_languages
            )
        if not meps_language:
            return (
                (key, "") in self._languages
                or (key, language) in self._languages
            )
        return any(
            (key, current_language, current_meps) in self._constraints
            for current_language in ("", language)
            for current_meps in (0, meps_language)
        )


class _LocationIdentityIndex:
    """Track location aliases while honoring rejected same-JW constraints."""

    def __init__(self) -> None:
        self._totals: dict[str, int] = defaultdict(int)
        self._by_jw_key: dict[tuple[str, tuple[str, int, int, int]], int] = (
            defaultdict(int)
        )

    def add(self, identity: MediaIdentity) -> None:
        jw_key = identity._jw_key()
        for location in identity.locations:
            self._totals[location] += 1
            if jw_key is not None:
                self._by_jw_key[(location, jw_key)] += 1

    def matches(
        self,
        locations: tuple[str, ...],
        rejected_jw_key: tuple[str, int, int, int] | None,
    ) -> bool:
        for location in locations:
            total = self._totals.get(location, 0)
            if rejected_jw_key is None:
                if total:
                    return True
                continue
            if total > self._by_jw_key.get((location, rejected_jw_key), 0):
                return True
        return False


class _MediaIdentityIndex:
    """Index possible matches without weakening ``MediaIdentity.matches``."""

    def __init__(self, identities: Iterable[MediaIdentity] = ()) -> None:
        self._source_constraints = _LanguageConstraintIndex[str]()
        self._jw_constraints = _LanguageConstraintIndex[
            tuple[str, int, int, int]
        ]()
        self._without_source_jw_constraints = _LanguageConstraintIndex[
            tuple[str, int, int, int]
        ]()
        self._locations = _LocationIdentityIndex()
        self._without_source_locations = _LocationIdentityIndex()
        for identity in identities:
            self.add(identity)

    def add(self, identity: MediaIdentity) -> None:
        if identity.source_id:
            self._source_constraints.add(
                identity.source_id,
                identity.language_code,
                identity.meps_language,
            )
        if (jw_key := identity._jw_key()) is not None:
            self._jw_constraints.add(
                jw_key,
                identity.language_code,
                identity.meps_language,
            )
            if not identity.source_id:
                self._without_source_jw_constraints.add(
                    jw_key,
                    identity.language_code,
                    identity.meps_language,
                )
        self._locations.add(identity)
        if not identity.source_id:
            self._without_source_locations.add(identity)

    def matches(self, candidate: MediaIdentity) -> bool:
        if candidate.source_id and self._source_constraints.matches(
            candidate.source_id,
            candidate.language_code,
            candidate.meps_language,
        ):
            return True
        if (jw_key := candidate._jw_key()) is not None:
            jw_constraints = (
                self._without_source_jw_constraints
                if candidate.source_id
                else self._jw_constraints
            )
            if jw_constraints.matches(
                jw_key,
                candidate.language_code,
                candidate.meps_language,
            ):
                return True
        locations = (
            self._without_source_locations
            if candidate.source_id
            else self._locations
        )
        return locations.matches(candidate.locations, jw_key)


@dataclass(frozen=True, slots=True)
class MediaPartition:
    unique_items: tuple[Mapping[str, Any], ...]
    duplicate_items: tuple[Mapping[str, Any], ...]


def media_identity(record: Mapping[str, Any]) -> MediaIdentity | None:
    locations = tuple(
        dict.fromkeys(
            normalized
            for key in (
                "source_url",
                "url",
                "download_url",
                "file_path",
                "jworg_url",
            )
            if (normalized := _normalized_location(_text(record.get(key))))
        )
    )
    location = locations[0] if locations else ""
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
        location=location,
        alternate_locations=locations[1:],
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
    return _MediaIdentityIndex(
        identity
        for item in existing_items
        if (identity := media_identity(item)) is not None
    ).matches(candidate_identity)


def partition_media_items(
    existing_items: Sequence[Mapping[str, Any]],
    candidates: Sequence[Mapping[str, Any]],
) -> MediaPartition:
    accepted: list[Mapping[str, Any]] = []
    duplicates: list[Mapping[str, Any]] = []
    identities = _MediaIdentityIndex(
        identity
        for item in existing_items
        if (identity := media_identity(item)) is not None
    )
    for candidate in candidates:
        identity = media_identity(candidate)
        if identity is not None and identities.matches(identity):
            duplicates.append(candidate)
            # Preserve aliases discovered in a duplicate record so later
            # candidates resolve through the same equivalence component.
            identities.add(identity)
            continue
        accepted.append(candidate)
        if identity is not None:
            identities.add(identity)
    return MediaPartition(tuple(accepted), tuple(duplicates))


__all__ = [
    "MediaIdentity",
    "MediaPartition",
    "contains_media",
    "media_identity",
    "partition_media_items",
    "same_media",
]
