from pathlib import Path
import os

import pytest

from solin.core.ingest.watched_folder_files import WatchedFolderFileStore
from solin.core.foundation.thread_workers import CancellationFlag
from solin.core.ingest.staging import WATCHED_FOLDER_STAGING_SUFFIX
from solin.core.ingest.watched_folder import scan_subfolder
from solin.core.ingest.watched_folder_files import WatchedFolderCopyRequest
from solin.core.media.operations import MediaOperationCancelled


def test_watched_folder_file_store_copies_with_unique_name(tmp_path):
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"one")
    folder = tmp_path / "folder"
    folder.mkdir()
    (folder / "clip.mp4").write_bytes(b"existing")
    store = WatchedFolderFileStore()

    copied = Path(store.copy_file_into_folder(source, folder))

    assert copied == folder / "clip (1).mp4"
    assert copied.read_bytes() == b"one"
    assert not list(folder.glob(f"*{WATCHED_FOLDER_STAGING_SUFFIX}"))


def test_watched_folder_copy_transaction_reports_chunk_progress(tmp_path):
    source = tmp_path / "large.mp4"
    source.write_bytes(b"a" * (192 * 1024))
    folder = tmp_path / "folder"
    folder.mkdir()
    updates: list[tuple[int, int]] = []
    store = WatchedFolderFileStore()

    result = store.copy_file_transaction(
        WatchedFolderCopyRequest(
            source=source,
            folder=folder,
            operation_id="copy-1",
            chunk_size=64 * 1024,
        ),
        progress=lambda completed, total: updates.append((completed, total)),
    )

    assert result.destination == folder / "large.mp4"
    assert result.bytes_copied == 192 * 1024
    assert result.destination.read_bytes() == source.read_bytes()
    assert updates == [
        (0, 192 * 1024),
        (64 * 1024, 192 * 1024),
        (128 * 1024, 192 * 1024),
        (192 * 1024, 192 * 1024),
    ]


def test_watched_folder_copy_cancellation_removes_staging_and_destination(tmp_path):
    source = tmp_path / "large.mp4"
    source.write_bytes(b"a" * (256 * 1024))
    folder = tmp_path / "folder"
    folder.mkdir()
    cancellation = CancellationFlag()
    store = WatchedFolderFileStore()

    def cancel_after_first_chunk(completed: int, _total: int) -> None:
        if completed:
            cancellation.set()

    with pytest.raises(MediaOperationCancelled):
        store.copy_file_transaction(
            WatchedFolderCopyRequest(
                source=source,
                folder=folder,
                operation_id="copy-cancel",
                chunk_size=64 * 1024,
            ),
            progress=cancel_after_first_chunk,
            cancellation=cancellation,
        )

    assert list(folder.iterdir()) == []


def test_watched_folder_copy_does_not_duplicate_an_existing_contained_file(tmp_path):
    folder = tmp_path / "folder"
    folder.mkdir()
    source = folder / "clip.mp4"
    source.write_bytes(b"video")
    store = WatchedFolderFileStore()

    result = store.copy_file_transaction(
        WatchedFolderCopyRequest(source, folder, "copy-contained")
    )

    assert result.destination == source
    assert result.already_present is True
    assert [path.name for path in folder.iterdir()] == ["clip.mp4"]


def test_watched_folder_scanner_ignores_transaction_staging(tmp_path):
    folder = tmp_path / "folder"
    folder.mkdir()
    staging = folder / f".clip.mp4.copy-1{WATCHED_FOLDER_STAGING_SUFFIX}"
    staging.write_bytes(b"partial")
    assert scan_subfolder(str(folder)) == []


def test_watched_folder_file_store_renames_and_deletes_folder(tmp_path):
    folder = tmp_path / "old"
    folder.mkdir()
    store = WatchedFolderFileStore()

    renamed = Path(store.rename_folder(folder, "new"))
    assert renamed == tmp_path / "new"
    assert renamed.exists()

    store.delete_folder(renamed)
    assert not renamed.exists()


def test_watched_folder_file_store_rejects_nested_rename(tmp_path):
    folder = tmp_path / "old"
    folder.mkdir()
    store = WatchedFolderFileStore()

    with pytest.raises(ValueError, match="Invalid folder name"):
        store.rename_folder(folder, "../escape")


def test_watched_folder_file_store_removes_only_contained_files(tmp_path):
    folder = tmp_path / "folder"
    folder.mkdir()
    inside = folder / "clip.mp4"
    inside.write_bytes(b"clip")
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"clip")
    store = WatchedFolderFileStore()

    assert store.remove_file_inside(inside, folder) is True
    assert not inside.exists()
    assert store.remove_file_inside(outside, folder) is False
    assert outside.exists()


def test_watched_folder_file_store_scans_meeting_sources(tmp_path):
    meeting = tmp_path / "2026-05-27 MW"
    meeting.mkdir()
    source = meeting / "talk.mp4"
    source.write_bytes(b"video")
    store = WatchedFolderFileStore()

    folders = store.scan_meeting_sources(tmp_path)

    assert folders[0]["monday"] == "2026-05-25"
    assert folders[0]["meeting_tag"] == "MW"
    assert folders[0]["sources"][0]["name"] == "talk.mp4"


def test_watched_folder_file_store_reports_availability_signature(tmp_path):
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"video")
    store = WatchedFolderFileStore()

    signature = store.file_availability_signature(
        [str(media), "https://example.test/clip.mp4"]
    )

    key = os.path.normcase(os.path.normpath(os.path.abspath(str(media))))
    assert signature == ((key, True),)


def test_watched_folder_file_store_applies_meeting_processing_policy():
    source = {"signature": {"size": 10, "mtime_ns": 123}}
    record = {
        "status": "processed",
        "signature": {"size": 10, "mtime_ns": 123},
    }
    store = WatchedFolderFileStore()

    assert store.meeting_source_needs_processing(source, None) is True
    assert store.meeting_source_needs_processing(source, record) is False
