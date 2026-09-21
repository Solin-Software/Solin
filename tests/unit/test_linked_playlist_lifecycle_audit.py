"""Facade-level lifecycle regressions, including failures and stale GUI views."""

from pathlib import Path
import shutil

import pytest

from solin.core.ingest.watched_folder import (
    load_manifest_playlist,
    save_manifest_playlist,
    stage_manifest_playlist,
)
from solin.core.ingest.watched_folder_files import WatchedFolderFileStore
from solin.core.ingest.watched_folder_playlists import WatchedFolderPlaylistStore
from solin.core.playlists.linked_folder import INTENT, STATE, playlist_sync


def _organized(folder: Path) -> dict:
    folder.mkdir()
    (folder / "slide.jpg").write_bytes(b"image")
    save_manifest_playlist(str(folder), {
        "sections": [{"id": "section", "title": "Organization"}],
        "items": [{"id": "slide", "url": str(folder / "slide.jpg"),
                   "title": "Custom title", "type": "image", "section_id": "section"}],
    })
    return load_manifest_playlist(str(folder))


def test_plain_discovery_does_not_report_endless_pending_publication(tmp_path):
    (tmp_path / "slide.jpg").write_bytes(b"image")
    view = load_manifest_playlist(str(tmp_path))
    assert len(view["items"]) == 1
    assert not view["__sync_pending"]


def test_failed_folder_delete_preserves_document_and_organization(tmp_path, monkeypatch):
    folder = tmp_path / "linked"
    before = _organized(folder)
    files = WatchedFolderFileStore()

    def locked(_folder):
        raise PermissionError("Provider has locked the folder")

    monkeypatch.setattr(files, "delete_folder", locked)
    with pytest.raises(PermissionError, match="locked"):
        WatchedFolderPlaylistStore().delete_folder(str(folder), files)

    after = load_manifest_playlist(str(folder))
    assert after[STATE]["document_id"] == before[STATE]["document_id"]
    assert after["sections"] == before["sections"]
    assert after["items"] == before["items"]


def test_edit_from_open_view_after_rename_keeps_portable_resource(tmp_path):
    folder = tmp_path / "linked"
    view = _organized(folder)
    store = WatchedFolderPlaylistStore()
    renamed = Path(store.rename_folder(str(folder), "renamed", WatchedFolderFileStore()))

    # The open tree still holds the old absolute URL when the next edit occurs.
    view["items"][0]["title"] = "Edit after rename"
    stage_manifest_playlist(str(folder), view)
    after = load_manifest_playlist(str(renamed))

    assert len(after["items"]) == 1
    assert after["items"][0]["url"] == str(renamed / "slide.jpg")
    assert after["items"][0]["title"] == "Edit after rename"
    assert after["items"][0]["section_id"] == "section"


def test_reset_preserves_organized_view_with_fresh_document(tmp_path):
    folder = tmp_path / "linked"
    before = _organized(folder)
    WatchedFolderPlaylistStore().reset_sync(str(folder))
    after = load_manifest_playlist(str(folder))
    assert after[STATE]["document_id"] != before[STATE]["document_id"]
    assert after["items"] == before["items"]
    assert after["sections"] == before["sections"]
    assert playlist_sync(folder).pending_intents() == []


def test_explicit_reset_resolves_conflicting_descriptors_preserving_known_view(tmp_path):
    folder = tmp_path / "linked"
    before = _organized(folder)
    other = tmp_path / "other"
    _organized(other)
    shutil.copytree(other / ".solin_sync" / "playlist" / "documents",
                    folder / ".solin_sync" / "playlist" / "documents", dirs_exist_ok=True)
    WatchedFolderPlaylistStore().reset_sync(str(folder))
    after = load_manifest_playlist(str(folder))
    assert after[STATE]["document_id"] != before[STATE]["document_id"]
    assert after["items"] == before["items"]
    assert after["sections"] == before["sections"]


def test_rename_intent_write_failure_rolls_back_adapter_and_binding(tmp_path, monkeypatch):
    from solin.core.storage.json_repository import JsonFileRepository

    folder = tmp_path / "linked"
    before = _organized(folder)
    before["items"][0]["title"] = "Pending title"
    stage_manifest_playlist(str(folder), before)
    original = JsonFileRepository.write

    def interrupted(repository, value):
        if any(str(item.get("url", "")).startswith(str(tmp_path / "renamed"))
               for item in value.get("items", [])):
            raise PermissionError("Intent storage temporarily locked")
        return original(repository, value)

    with monkeypatch.context() as patch:
        patch.setattr(JsonFileRepository, "write", interrupted)
        with pytest.raises(PermissionError, match="temporarily locked"):
            WatchedFolderPlaylistStore().rename_folder(str(folder), "renamed", WatchedFolderFileStore())
    assert folder.is_dir()
    assert playlist_sync(folder).folder == folder
    assert load_manifest_playlist(str(folder))["items"][0]["title"] == "Pending title"


@pytest.mark.parametrize("failure", [PermissionError, RuntimeError, KeyboardInterrupt])
def test_rename_second_intent_failure_restores_all_pending_bytes(tmp_path, monkeypatch, failure):
    from solin.core.storage.json_repository import JsonFileRepository

    folder = tmp_path / "linked"
    view = _organized(folder)
    view["items"][0]["title"] = "First pending edit"
    stage_manifest_playlist(str(folder), view)
    view["items"][0]["title"] = "Second pending edit"
    view.pop(INTENT)
    stage_manifest_playlist(str(folder), view)
    service = playlist_sync(folder)
    original_bytes = {path: path.read_bytes()
                      for path in (service.replica.state_dir / "intents").glob("*.json")}
    assert len(original_bytes) == 2
    write = JsonFileRepository.write
    rewritten = 0

    def interrupted(repository, value):
        nonlocal rewritten
        write(repository, value)
        rewritten += 1
        if rewritten == 2:
            # Replacement may finish before an fsync or subsequent storage
            # operation reports failure, so restore the attempted write too.
            raise failure("Second intent could not finish publication")

    with monkeypatch.context() as patch:
        patch.setattr(JsonFileRepository, "write", interrupted)
        with pytest.raises(failure, match="Second intent"):
            WatchedFolderPlaylistStore().rename_folder(str(folder), "renamed", WatchedFolderFileStore())
    assert rewritten == 2
    assert folder.is_dir()
    assert service.folder == service.replica.folder == folder
    assert {path: path.read_bytes() for path in original_bytes} == original_bytes
    after = load_manifest_playlist(str(folder))
    assert after["items"][0]["title"] == "Second pending edit"
    assert after["items"][0]["url"] == str(folder / "slide.jpg")


def test_reset_can_recover_preserved_reference_when_old_archive_arrives_late(tmp_path):
    from solin.core.ingest.sync.resources import retire_file

    folder = tmp_path / "linked"
    before = _organized(folder)
    # Simulate a concurrent computer archiving the file while this computer's
    # known occurrence still references it. Cloud archive bytes arrive later.
    retire_file(folder, "slide.jpg", document_id=before[STATE]["document_id"])
    shared_archive = folder / ".solin_sync" / "resources"
    delayed = tmp_path / "delayed_archive"
    shutil.move(str(shared_archive), delayed)
    WatchedFolderPlaylistStore().reset_sync(str(folder))
    shutil.move(str(delayed), shared_archive)
    after = load_manifest_playlist(str(folder))
    assert len(after["items"]) == 1
    assert (folder / "slide.jpg").read_bytes() == b"image"
