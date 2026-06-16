from pathlib import Path

import pytest

from solin.core.ingest.watched_folder_files import WatchedFolderFileStore


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
    assert not (folder / "clip (1).mp4.solin_tmp").exists()


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
