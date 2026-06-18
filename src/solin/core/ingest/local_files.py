"""Local-file availability policies shared by linked-folder workflows."""

from __future__ import annotations

from collections.abc import Iterable
import os


def local_file_availability_signature(
    urls: Iterable[str],
) -> tuple[tuple[str, bool], ...]:
    """Return a stable snapshot of local-file availability.

    Linked-folder workflows intentionally keep missing local files in saved data
    so cloud-sync placeholders can be shown. This signature lets presentation
    adapters detect when only machine-local availability changed.
    """
    states: dict[str, bool] = {}
    for url in urls:
        if not url or url.startswith(("http://", "https://")):
            continue
        norm = os.path.normcase(os.path.normpath(os.path.abspath(url)))
        states[norm] = os.path.exists(url)
    return tuple(sorted(states.items()))
