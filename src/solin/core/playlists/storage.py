from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from solin.core.foundation.exception_logging import log_ignored_exception
from solin.core.storage.json_files import read_json_file, write_json_atomic


@dataclass(frozen=True, slots=True)
class PlaylistStoragePaths:
    playlists_file: Path
    pending_deletions_file: Path


def load_playlists(paths: PlaylistStoragePaths) -> list[dict]:
    try:
        if paths.playlists_file.exists():
            data = read_json_file(paths.playlists_file)
            return data.get("playlists", []) if isinstance(data, dict) else []
    except (OSError, UnicodeError, json.JSONDecodeError, AttributeError):
        log_ignored_exception(__name__, "Could not load playlists file")
    return []


def save_playlists(
    playlists: list[dict],
    paths: PlaylistStoragePaths,
) -> None:
    try:
        write_json_atomic(paths.playlists_file, {"playlists": playlists})
    except (OSError, UnicodeError, TypeError, ValueError):
        log_ignored_exception(__name__, "Could not save playlists file")


def load_pending_deletions(paths: PlaylistStoragePaths) -> list[str]:
    try:
        if paths.pending_deletions_file.exists():
            data = read_json_file(paths.pending_deletions_file)
            return data.get("pending", []) if isinstance(data, dict) else []
    except (OSError, UnicodeError, json.JSONDecodeError, AttributeError):
        log_ignored_exception(__name__, "Could not load pending deletions file")
    return []


def save_pending_deletions(
    media_paths: list[str],
    paths: PlaylistStoragePaths,
) -> None:
    try:
        write_json_atomic(paths.pending_deletions_file, {"pending": media_paths})
    except (OSError, UnicodeError, TypeError, ValueError):
        log_ignored_exception(__name__, "Could not save pending deletions file")
