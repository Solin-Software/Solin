"""Immutable release-history results shared with presentation consumers."""

from __future__ import annotations

from dataclasses import dataclass

from solin.core.releases.manifest import ReleaseAsset
from solin.core.releases.version import ReleaseVersion


@dataclass(frozen=True, slots=True)
class ReleaseHistoryEntry:
    version: ReleaseVersion
    markdown: str
    language: str
    assets: tuple[ReleaseAsset, ...]


class ReleaseHistoryCursorError(ValueError):
    """The consumer must restart history with cursor=None."""


@dataclass(frozen=True, slots=True)
class ReleaseHistoryPage:
    """Own-version notes with an opaque cursor for continuing the same query."""

    items: tuple[ReleaseHistoryEntry, ...] = ()
    next_cursor: str | None = None
