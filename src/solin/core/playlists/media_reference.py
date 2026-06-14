"""Pure parsing of JW media references from URLs and filenames."""

from __future__ import annotations

import re
from pathlib import Path
from typing import TypedDict, cast

from ..jw.metadata import MEPS_FROM_LANG, parse_jworg_url


class JwMediaReference(TypedDict):
    key_symbol: str | None
    track: int | None
    issue_tag: int | None
    doc_id: int | None
    meps_language: int


_JW_FILENAME_RE = re.compile(
    r"^([A-Za-z][A-Za-z0-9]{1,11})_([A-Z]{1,4})_(\d{1,4})(?:_.*)?$",
    re.IGNORECASE,
)
_JW_DOCID_FILENAME_RE = re.compile(
    r"^(\d{5,12})_([A-Z]{1,4})_[A-Za-z]+_(\d+)",
    re.IGNORECASE,
)
_JW_PUB_FILENAME_RE = re.compile(
    r"pub-([A-Za-z0-9]+)_([A-Z]{1,4})_(\d{1,4})(?:_.*)?",
    re.IGNORECASE,
)


def _reference(
    *,
    key_symbol: str | None,
    track: int | None,
    doc_id: int | None,
    language_code: str,
) -> JwMediaReference:
    return {
        "key_symbol": key_symbol,
        "track": track,
        "issue_tag": None,
        "doc_id": doc_id,
        "meps_language": MEPS_FROM_LANG.get(language_code.upper(), 0),
    }


def parse_jw_media_reference(
    url: str,
    *,
    original_filename: str = "",
) -> JwMediaReference | None:
    """Extract a JW media reference from a CDN URL or local filename."""
    if url.startswith(("http://", "https://")):
        parsed = parse_jworg_url(url)
        if parsed:
            return cast(JwMediaReference, parsed)

    candidates: list[str] = []
    if original_filename:
        candidates.append(Path(original_filename.split("?", 1)[0]).stem)
    url_stem = Path(url.split("?", 1)[0]).stem
    if url_stem and url_stem not in candidates:
        candidates.append(url_stem)

    for stem in candidates:
        match = _JW_PUB_FILENAME_RE.search(stem)
        if match:
            return _reference(
                key_symbol=match.group(1).lower(),
                track=int(match.group(3)),
                doc_id=None,
                language_code=match.group(2),
            )

        match = _JW_DOCID_FILENAME_RE.match(stem)
        if match:
            return _reference(
                key_symbol=None,
                track=int(match.group(3)),
                doc_id=int(match.group(1)),
                language_code=match.group(2),
            )

        match = _JW_FILENAME_RE.match(stem)
        if match:
            return _reference(
                key_symbol=match.group(1).lower(),
                track=int(match.group(3)),
                doc_id=None,
                language_code=match.group(2),
            )

    return None
