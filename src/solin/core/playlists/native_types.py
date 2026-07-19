"""Framework-independent contracts for native Solin playlist transfers."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


SOLIN_PLAYLIST_MIME = "application/vnd.solin.playlist+zip"
SOLIN_PLAYLIST_FORMAT = "com.solin.playlist"
SOLIN_PLAYLIST_SCHEMA_VERSION = 1
SOLIN_PLAYLIST_MANIFEST_MAX_BYTES = 16 * 1024 * 1024
SOLIN_PLAYLIST_MAX_ENTRIES = 10_000


class NativePlaylistError(ValueError):
    """Base class for native playlist format and transfer failures."""


class NativePlaylistValidationError(NativePlaylistError):
    """The archive or in-memory playlist violates the native format contract."""


class NativePlaylistVersionError(NativePlaylistValidationError):
    """The archive uses a schema version this build cannot read."""


class NativePlaylistMissingMediaError(NativePlaylistError):
    """Local playlist media cannot be embedded because it is unavailable."""

    def __init__(self, missing_paths: list[str]) -> None:
        self.missing_paths = tuple(missing_paths)
        detail = "\n".join(f"- {path}" for path in self.missing_paths)
        super().__init__(f"Local playlist media is missing or unreadable:\n{detail}")


class NativePlaylistTransferCancelled(NativePlaylistError):
    """A native playlist transfer was cancelled cooperatively."""


class CancellationProbe(Protocol):
    """Small protocol implemented by threading.Event and the Qt cancellation flag."""

    def is_set(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class NativePlaylistProgress:
    """Progress payload that can cross worker/UI boundaries without Qt types."""

    stage: str
    detail: str = ""
    completed: int = 0
    total: int = 0
    can_cancel: bool = True
    items_completed: int = 0
    items_total: int = 0
    bytes_completed: int = 0
    bytes_total: int = 0


ProgressCallback = Callable[[NativePlaylistProgress], None]
JwSourceResolver = Callable[[Mapping[str, Any]], str | None]
PlaylistNameValidator = Callable[[object], str]


@dataclass(frozen=True, slots=True)
class NativePlaylistExportRequest:
    """All state required to export one immutable playlist snapshot."""

    playlist: Mapping[str, Any]
    output_path: str | Path
    media_cache_dir: str | Path
    thumbnail_cache_dir: str | Path
    generator_version: str = ""
    progress: ProgressCallback | None = None
    cancellation: CancellationProbe | None = None


@dataclass(frozen=True, slots=True)
class NativePlaylistExportResult:
    output_path: Path
    assets_written: int
    embedded_media: int
    thumbnails: int
    bytes_written: int


@dataclass(frozen=True, slots=True)
class NativePlaylistImportRequest:
    """Profile-owned destinations and policies for one native playlist import."""

    input_path: str | Path
    embedded_media_dir: str | Path
    thumbnail_cache_dir: str | Path
    staging_dir: str | Path | None = None
    resolve_jw_source: JwSourceResolver | None = None
    validate_playlist_name: PlaylistNameValidator | None = None
    progress: ProgressCallback | None = None
    cancellation: CancellationProbe | None = None


@dataclass(frozen=True, slots=True)
class NativePlaylistImportResult:
    """Strict import result; callers own persistence and rollback after return."""

    playlist: dict[str, Any]
    source_path: Path
    created_files: tuple[Path, ...] = field(default_factory=tuple)
    assets_imported: int = 0
    bytes_imported: int = 0


__all__ = [
    "CancellationProbe",
    "JwSourceResolver",
    "NativePlaylistError",
    "NativePlaylistExportRequest",
    "NativePlaylistExportResult",
    "NativePlaylistImportRequest",
    "NativePlaylistImportResult",
    "NativePlaylistMissingMediaError",
    "NativePlaylistProgress",
    "NativePlaylistTransferCancelled",
    "NativePlaylistValidationError",
    "NativePlaylistVersionError",
    "ProgressCallback",
    "PlaylistNameValidator",
    "SOLIN_PLAYLIST_FORMAT",
    "SOLIN_PLAYLIST_MANIFEST_MAX_BYTES",
    "SOLIN_PLAYLIST_MAX_ENTRIES",
    "SOLIN_PLAYLIST_MIME",
    "SOLIN_PLAYLIST_SCHEMA_VERSION",
]
