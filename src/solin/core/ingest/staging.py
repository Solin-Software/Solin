"""Shared naming contract for linked-folder transaction staging files."""

from pathlib import Path


WATCHED_FOLDER_STAGING_SUFFIX = ".solin-stage"


def is_watched_folder_staging_path(path: str | Path) -> bool:
    return Path(path).name.endswith(WATCHED_FOLDER_STAGING_SUFFIX)
