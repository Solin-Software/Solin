"""Filesystem operations for watched-folder media workflows."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
import os
import shutil
from pathlib import Path
import re
from typing import Any, Protocol

from solin.core.ingest.local_files import (
    LocalFileAvailabilitySignature,
    local_file_availability_signature,
)
from solin.core.ingest.meeting_folder_sources import (
    meeting_folder_source_needs_processing,
    scan_meeting_folder_sources,
)
from solin.core.media.download_storage import safe_remove
from solin.core.media.operations import MediaOperationCancelled
from solin.core.ingest.staging import WATCHED_FOLDER_STAGING_SUFFIX


_DEFAULT_COPY_CHUNK_SIZE = 4 * 1024 * 1024
_SAFE_OPERATION_ID = re.compile(r"[^A-Za-z0-9._-]+")


class CancellationProbe(Protocol):
    def is_set(self) -> bool:
        ...


CopyProgress = Callable[[int, int], None]


@dataclass(frozen=True, slots=True)
class WatchedFolderCopyRequest:
    source: Path
    folder: Path
    operation_id: str
    chunk_size: int = _DEFAULT_COPY_CHUNK_SIZE

    def __post_init__(self) -> None:
        if not self.operation_id.strip():
            raise ValueError("operation_id must not be empty")
        if (
            not isinstance(self.chunk_size, int)
            or isinstance(self.chunk_size, bool)
            or self.chunk_size < 64 * 1024
        ):
            raise ValueError("chunk_size must be an integer of at least 64 KiB")


@dataclass(frozen=True, slots=True)
class WatchedFolderCopyResult:
    source: Path
    destination: Path
    bytes_copied: int
    already_present: bool = False


class WatchedFolderFileStore:
    """Performs watched-folder copy, rename, delete, and contained removal."""

    def copy_file_into_folder(self, source: str | Path, folder: str | Path) -> str:
        result = self.copy_file_transaction(
            WatchedFolderCopyRequest(
                source=Path(source),
                folder=Path(folder),
                operation_id="legacy-copy",
            )
        )
        return os.fspath(result.destination)

    def copy_file_transaction(
        self,
        request: WatchedFolderCopyRequest,
        *,
        progress: CopyProgress | None = None,
        cancellation: CancellationProbe | None = None,
    ) -> WatchedFolderCopyResult:
        """Copy in chunks, then atomically publish a collision-safe destination."""

        source = request.source.resolve(strict=True)
        folder = request.folder.resolve(strict=True)
        if not source.is_file():
            raise OSError(f"Media source is not a file: {source}")
        if not folder.is_dir():
            raise OSError(f"Linked playlist folder is not a directory: {folder}")
        total = source.stat().st_size
        if self.is_inside(source, folder):
            if progress is not None:
                progress(total, total)
            return WatchedFolderCopyResult(source, source, 0, already_present=True)

        equivalent_destination = self._equivalent_destination(
            source,
            folder,
            cancellation=cancellation,
        )
        if equivalent_destination is not None:
            if progress is not None:
                progress(total, total)
            return WatchedFolderCopyResult(
                source,
                equivalent_destination,
                0,
                already_present=True,
            )

        operation_id = _SAFE_OPERATION_ID.sub("_", request.operation_id).strip("._")
        operation_id = operation_id[:80] or "copy"
        staging = folder / f".{source.name}.{operation_id}{WATCHED_FOLDER_STAGING_SUFFIX}"
        safe_remove(staging)
        copied = 0
        try:
            if progress is not None:
                progress(0, total)
            with source.open("rb") as source_file, staging.open("xb") as staging_file:
                while True:
                    if cancellation is not None and cancellation.is_set():
                        raise MediaOperationCancelled("Media copy cancelled")
                    chunk = source_file.read(request.chunk_size)
                    if not chunk:
                        break
                    staging_file.write(chunk)
                    copied += len(chunk)
                    if progress is not None:
                        progress(copied, total)
                staging_file.flush()
                os.fsync(staging_file.fileno())
            shutil.copystat(source, staging)
            if cancellation is not None and cancellation.is_set():
                raise MediaOperationCancelled("Media copy cancelled")
            destination = self._reserve_unique_child_path(folder, source.name)
            try:
                os.replace(staging, destination)
            except OSError:
                safe_remove(destination)
                raise
        except (OSError, MediaOperationCancelled):
            safe_remove(staging)
            raise
        except Exception:  # noqa: BLE001 - transaction cleanup before callback propagation
            safe_remove(staging)
            raise
        return WatchedFolderCopyResult(source, destination, copied)

    def rename_folder(self, folder: str | Path, new_name: str) -> str:
        if Path(new_name).name != new_name or not new_name.strip():
            raise ValueError(f"Invalid folder name: {new_name!r}")
        folder_path = Path(folder)
        destination = folder_path.parent / new_name
        folder_path.rename(destination)
        return os.fspath(destination)

    def delete_folder(self, folder: str | Path) -> None:
        shutil.rmtree(folder, ignore_errors=False)

    def remove_file_inside(self, file_path: str | Path, folder: str | Path) -> bool:
        if not file_path:
            return False
        path = Path(file_path)
        if not path.exists():
            return True
        if not path.is_file() or not self.is_inside(path, folder):
            return False
        path.unlink()
        return True

    def scan_meeting_sources(self, folder_path: str | Path) -> list[dict[str, Any]]:
        return scan_meeting_folder_sources(folder_path)

    def meeting_source_needs_processing(
        self,
        source: dict[str, Any],
        record: dict[str, Any] | None,
    ) -> bool:
        return meeting_folder_source_needs_processing(source, record)

    def file_availability_signature(
        self,
        urls: Iterable[str],
    ) -> LocalFileAvailabilitySignature:
        return local_file_availability_signature(urls)

    @staticmethod
    def is_inside(path: str | Path, folder: str | Path) -> bool:
        try:
            Path(path).resolve().relative_to(Path(folder).resolve())
            return True
        except (OSError, ValueError):
            return False

    @staticmethod
    def _unique_child_path(folder: Path, filename: str) -> Path:
        destination = folder / filename
        if not destination.exists():
            return destination
        stem = destination.stem
        suffix = destination.suffix
        counter = 1
        while destination.exists():
            destination = folder / f"{stem} ({counter}){suffix}"
            counter += 1
        return destination

    @staticmethod
    def _reserve_unique_child_path(folder: Path, filename: str) -> Path:
        requested = folder / filename
        stem = requested.stem
        suffix = requested.suffix
        counter = 0
        while True:
            destination = (
                requested
                if counter == 0
                else folder / f"{stem} ({counter}){suffix}"
            )
            try:
                descriptor = os.open(
                    destination,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                )
            except FileExistsError:
                counter += 1
                continue
            os.close(descriptor)
            return destination

    @staticmethod
    def _equivalent_destination(
        source: Path,
        folder: Path,
        *,
        cancellation: CancellationProbe | None,
    ) -> Path | None:
        stem = source.stem
        suffix = source.suffix
        source_name_key = os.path.normcase(source.name)
        stem_key = os.path.normcase(stem)
        suffix_key = os.path.normcase(suffix)
        candidates: list[tuple[int, Path]] = []
        for candidate in folder.iterdir():
            if not candidate.is_file():
                continue
            candidate_name_key = os.path.normcase(candidate.name)
            if candidate_name_key == source_name_key:
                candidates.append((0, candidate))
                continue
            if not (
                candidate_name_key.startswith(f"{stem_key} (")
                and candidate_name_key.endswith(f"){suffix_key}")
            ):
                continue
            counter_text = candidate.name[
                len(stem) + 2 : len(candidate.name) - len(suffix) - 1
            ]
            if counter_text.isdecimal():
                candidates.append((int(counter_text), candidate))
        for _counter, candidate in sorted(candidates):
            if WatchedFolderFileStore._same_file_content(
                source,
                candidate,
                cancellation=cancellation,
            ):
                return candidate
        return None

    @staticmethod
    def _same_file_content(
        source: Path,
        destination: Path,
        *,
        cancellation: CancellationProbe | None,
    ) -> bool:
        try:
            if not destination.is_file():
                return False
            source_stat = source.stat()
            destination_stat = destination.stat()
            if source_stat.st_size != destination_stat.st_size:
                return False
            source_signature = (
                source_stat.st_size,
                source_stat.st_mtime_ns,
            )
            destination_signature = (
                destination_stat.st_size,
                destination_stat.st_mtime_ns,
            )
            with source.open("rb") as source_file, destination.open("rb") as target_file:
                while True:
                    if cancellation is not None and cancellation.is_set():
                        raise MediaOperationCancelled("Media copy cancelled")
                    source_chunk = source_file.read(_DEFAULT_COPY_CHUNK_SIZE)
                    target_chunk = target_file.read(_DEFAULT_COPY_CHUNK_SIZE)
                    if source_chunk != target_chunk:
                        return False
                    if not source_chunk:
                        break
                final_source = os.fstat(source_file.fileno())
                final_destination = os.fstat(target_file.fileno())
                return source_signature == (
                    final_source.st_size,
                    final_source.st_mtime_ns,
                ) and destination_signature == (
                    final_destination.st_size,
                    final_destination.st_mtime_ns,
                )
        except FileNotFoundError:
            return False
