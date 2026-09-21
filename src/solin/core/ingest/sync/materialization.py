"""Portable linked-media adoption using the shared atomic copy transaction."""

from pathlib import Path
import uuid

from solin.core.ingest.manifest import (
    CACHE_DIR_NAME, absolute_local_url_tail, cache_dir, is_absolute_local_url, to_manifest_url,
)
from solin.core.ingest.watched_folder_files import WatchedFolderCopyRequest, WatchedFolderFileStore


def portable_playlist_url(url: str, folder: Path) -> tuple[str, str | None]:
    """Adopt an external local file, or retain a portable unavailable reference."""
    if not url or url.startswith(("http://", "https://")):
        return url, None
    portable = to_manifest_url(url, folder)
    if portable != url:
        return portable, None
    source = Path(url)
    if source.is_absolute() and source.is_file():
        result = WatchedFolderFileStore().copy_file_transaction(WatchedFolderCopyRequest(
            source=source, folder=cache_dir(folder), operation_id=uuid.uuid4().hex,
        ))
        return to_manifest_url(str(result.destination), folder), str(result.destination)
    if is_absolute_local_url(url):
        tail = absolute_local_url_tail(url)
        if tail.name:
            relative = Path(CACHE_DIR_NAME) / tail.name
            return relative.as_posix(), str(folder / relative)
    return url, None
