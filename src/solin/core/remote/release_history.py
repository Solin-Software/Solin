"""Immutable release-history results shared with presentation consumers."""

from __future__ import annotations

from dataclasses import dataclass

from solin.core.remote.update_policy import ReleaseNote


class ReleaseHistoryCursorError(ValueError):
    """The consumer must restart history with cursor=None."""


@dataclass(frozen=True, slots=True)
class ReleaseHistoryPage:
    """Own-version notes with an opaque cursor for continuing the same query."""

    items: tuple[ReleaseNote, ...] = ()
    next_cursor: str | None = None
