"""Cumulative expansion and member limits shared by nested ZIP consumers."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import IO
from zipfile import ZipInfo

# Count the nested ZIP payload as well as its expanded files within the existing
# temporary-storage allowance. Extra outer members consume the same allowance.
MAX_EXPANDED_BYTES = 2 * 1024**3
# Allow ZIP64 publications beyond the classic ZIP limit of 65,535 entries while
# bounding per-member work, including empty files and directories in both layers.
MAX_ARCHIVE_MEMBERS = 100_000
_COPY_CHUNK_SIZE = 1024 * 1024


class ArchiveBudget:
    """Preflight declared sizes and bound actual reads across archive layers.

    Declared sizes also account for members a consumer skips. Actual output is
    counted independently so understated metadata cannot bypass the read limit.
    """

    def __init__(self) -> None:
        self._max_bytes = MAX_EXPANDED_BYTES
        self._max_members = MAX_ARCHIVE_MEMBERS
        self._declared_bytes = 0
        self._expanded_bytes = 0
        self._members = 0

    def check_members(self, members: Sequence[ZipInfo]) -> None:
        """Reject an over-budget layer before opening any of its members."""
        if len(members) > self._max_members - self._members:
            raise ValueError("Archive exceeds member count budget")
        total = max(self._declared_bytes, self._expanded_bytes)
        for member in members:
            if member.file_size < 0:
                raise ValueError("Archive member has a negative expanded size")
            total += member.file_size
            if total > self._max_bytes:
                raise ValueError("Archive exceeds expanded byte budget")
        self._declared_bytes = total
        self._members += len(members)

    def copy_stream(
        self,
        source: IO[bytes],
        destination: IO[bytes] | None = None,
        *,
        check_cancelled: Callable[[], None] | None = None,
    ) -> None:
        """Copy or discard a member, reading at most one byte beyond the budget."""
        while True:
            if check_cancelled is not None:
                check_cancelled()
            chunk = source.read(min(_COPY_CHUNK_SIZE, self._max_bytes - self._expanded_bytes + 1))
            if not chunk:
                return
            self._expanded_bytes += len(chunk)
            if self._expanded_bytes > self._max_bytes:
                raise ValueError("Archive exceeds expanded byte budget")
            if destination is not None:
                destination.write(chunk)
