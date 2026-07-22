"""Stable stat-based revision identities for local media sources."""

from __future__ import annotations

from typing import NamedTuple
import os
from pathlib import Path
import stat


class LocalSourceRevision(NamedTuple):
    """Cheap local-file identity suitable for cache and watcher invalidation."""

    size: int
    mtime_ns: int
    ctime_ns: int
    device: int
    inode: int


def local_source_revision_from_stat(source_stat: os.stat_result) -> LocalSourceRevision:
    return LocalSourceRevision(
        source_stat.st_size,
        source_stat.st_mtime_ns,
        source_stat.st_ctime_ns,
        source_stat.st_dev,
        source_stat.st_ino,
    )


def source_signature_from_stat(source_stat: os.stat_result) -> str:
    """Serialize the strongest portable stat-based local revision available."""

    return ":".join(str(value) for value in local_source_revision_from_stat(source_stat))


def local_source_signature(source: str | Path) -> str:
    """Return a revision signature for a regular local file, or an empty string."""

    try:
        source_stat = Path(source).stat()
    except OSError:
        return ""
    if not stat.S_ISREG(source_stat.st_mode):
        return ""
    return source_signature_from_stat(source_stat)


__all__ = [
    "LocalSourceRevision",
    "local_source_revision_from_stat",
    "local_source_signature",
    "source_signature_from_stat",
]
