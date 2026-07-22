"""Local-file availability policies shared by linked-folder workflows."""

from __future__ import annotations

from collections.abc import Iterable
import os
import stat

from solin.core.media.local_source import local_source_revision_from_stat


LocalFileState = tuple[bool, int, int, int, int, int]
LocalFileAvailabilitySignature = tuple[tuple[str, LocalFileState], ...]


def local_file_availability_signature(
    urls: Iterable[str],
) -> LocalFileAvailabilitySignature:
    """Return availability plus content identity for local media files.

    Linked-folder workflows intentionally keep missing local files in saved data
    so cloud-sync placeholders can be shown. This signature lets presentation
    adapters detect when only machine-local availability changed.
    """
    states: dict[str, LocalFileState] = {}
    for url in urls:
        if not url or url.startswith(("http://", "https://")):
            continue
        norm = os.path.normcase(os.path.normpath(os.path.abspath(url)))
        try:
            source_stat = os.stat(url)
        except OSError:
            states[norm] = (False, 0, 0, 0, 0, 0)
        else:
            is_file = stat.S_ISREG(source_stat.st_mode)
            if not is_file:
                states[norm] = (False, 0, 0, 0, 0, 0)
                continue
            revision = local_source_revision_from_stat(source_stat)
            states[norm] = (
                True,
                revision.size,
                revision.mtime_ns,
                revision.ctime_ns,
                revision.device,
                revision.inode,
            )
    return tuple(sorted(states.items()))


__all__ = [
    "LocalFileAvailabilitySignature",
    "LocalFileState",
    "local_file_availability_signature",
]
