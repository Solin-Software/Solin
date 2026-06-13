from __future__ import annotations

import json
import os

from ...core.foundation import paths as _paths
from ...core.foundation.exception_logging import log_ignored_exception
from ...core.storage.json_files import read_json_file, write_json_atomic


def _load_playlists() -> list[dict]:
    try:
        if os.path.exists(_paths.PLAYLISTS_FILE):
            data = read_json_file(_paths.PLAYLISTS_FILE)
            return data.get("playlists", []) if isinstance(data, dict) else []
    except (OSError, UnicodeError, json.JSONDecodeError, AttributeError):
        log_ignored_exception(__name__, "Could not load playlists file")
    return []


def _save_playlists(playlists: list[dict]) -> None:
    try:
        write_json_atomic(_paths.PLAYLISTS_FILE, {"playlists": playlists})
    except (OSError, UnicodeError, TypeError, ValueError):
        log_ignored_exception(__name__, "Could not save playlists file")


def load_pending_deletions() -> list[str]:
    try:
        if os.path.exists(_paths.PENDING_DEL_FILE):
            data = read_json_file(_paths.PENDING_DEL_FILE)
            return data.get("pending", []) if isinstance(data, dict) else []
    except (OSError, UnicodeError, json.JSONDecodeError, AttributeError):
        log_ignored_exception(__name__, "Could not load pending deletions file")
    return []


def save_pending_deletions(paths: list[str]) -> None:
    try:
        write_json_atomic(_paths.PENDING_DEL_FILE, {"pending": paths})
    except (OSError, UnicodeError, TypeError, ValueError):
        log_ignored_exception(__name__, "Could not save pending deletions file")
