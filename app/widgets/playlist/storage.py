from __future__ import annotations

import json
import os

from ...core.foundation import paths as _paths
from ...core.foundation.exception_logging import log_ignored_exception


def _load_playlists() -> list[dict]:
    try:
        os.makedirs(_paths.DATA_DIR, exist_ok=True)
        if os.path.exists(_paths.PLAYLISTS_FILE):
            with open(_paths.PLAYLISTS_FILE, "r", encoding="utf-8") as handle:
                return json.load(handle).get("playlists", [])
    except Exception:
        log_ignored_exception(__name__, "Could not load playlists file")
    return []


def _save_playlists(playlists: list[dict]) -> None:
    try:
        os.makedirs(_paths.DATA_DIR, exist_ok=True)
        with open(_paths.PLAYLISTS_FILE, "w", encoding="utf-8") as handle:
            json.dump({"playlists": playlists}, handle, ensure_ascii=False, indent=2)
    except Exception:
        log_ignored_exception(__name__, "Could not save playlists file")


def _load_pending_deletions() -> list[str]:
    try:
        if os.path.exists(_paths.PENDING_DEL_FILE):
            with open(_paths.PENDING_DEL_FILE, "r", encoding="utf-8") as handle:
                return json.load(handle).get("pending", [])
    except Exception:
        log_ignored_exception(__name__, "Could not load pending deletions file")
    return []


def _save_pending_deletions(paths: list[str]) -> None:
    try:
        os.makedirs(_paths.DATA_DIR, exist_ok=True)
        with open(_paths.PENDING_DEL_FILE, "w", encoding="utf-8") as handle:
            json.dump({"pending": paths}, handle, ensure_ascii=False, indent=2)
    except Exception:
        log_ignored_exception(__name__, "Could not save pending deletions file")
