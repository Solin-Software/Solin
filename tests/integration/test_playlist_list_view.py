from solin.widgets.playlist.list_view import PLAYLIST_PRIMARY_BUTTON_STYLESHEET, PLAYLIST_SECONDARY_BUTTON_STYLESHEET

from types import SimpleNamespace
from unittest.mock import Mock
from pathlib import Path
from time import monotonic

import pytest
from PySide6.QtCore import QThread, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

from solin.widgets.playlist.list_view import PlaylistListView
from solin.widgets.playlist.dialogs import LinkedFolderNameDialog, NameDialog
from solin.widgets.playlist import list_view
from solin.controllers.media_operation_coordinator import MediaOperationCoordinator
from solin.core.foundation.resource_lanes import ResourceLaneRegistry
from solin.core.ingest.watched_folder_files import WatchedFolderFileStore
from solin.core.ingest.watched_folder_playlists import WatchedFolderPlaylistStore


def test_playlist_list_view_styles_define_primary_and_secondary_buttons():
    assert "QPushButton" in PLAYLIST_SECONDARY_BUTTON_STYLESHEET
    assert "#388bfd" in PLAYLIST_PRIMARY_BUTTON_STYLESHEET


@pytest.mark.parametrize("confirmed", [False, True])
def test_linked_sync_reset_requires_confirmation_and_uses_worker(monkeypatch, confirmed):
    store = SimpleNamespace(reset_sync=Mock())
    submit = Mock()
    view = SimpleNamespace(
        tr=lambda text: text,
        _watched_folder_playlist_store=store,
        _submit_watched_folder_mutation=submit,
    )
    question = Mock(return_value=(QMessageBox.StandardButton.Yes if confirmed
                                 else QMessageBox.StandardButton.No))
    monkeypatch.setattr(QMessageBox, "question", question)
    PlaylistListView._reset_watched_folder_sync(view, "/linked/Playlist")
    assert question.call_args.args[-1] == QMessageBox.StandardButton.No
    store.reset_sync.assert_not_called()
    if confirmed:
        path, operation, callback = submit.call_args.args
        assert (path, operation) == ("/linked/Playlist", "reset_linked_playlist_sync")
        callback()
        store.reset_sync.assert_called_once_with(path)
    else:
        submit.assert_not_called()


def _wait_for(predicate):
    deadline = monotonic() + 5
    while not predicate() and monotonic() < deadline:
        QTest.qWait(5)
    assert predicate()


@pytest.fixture
def linked_folder_view(tmp_path):
    root = tmp_path / "linked"
    root.mkdir()
    lanes = ResourceLaneRegistry()
    operations = MediaOperationCoordinator(resource_lanes=lanes)
    view = PlaylistListView(
        [], None,
        watched_folder=str(root),
        persist_playlists=Mock(),
        watched_folder_file_store=WatchedFolderFileStore(),
        watched_folder_playlist_store=WatchedFolderPlaylistStore(lanes),
        media_tree_runtime=SimpleNamespace(operations=operations),
        schedule_cleanup=Mock(),
    )
    try:
        yield view, root, operations
    finally:
        view.cleanup()
        remaining = operations.shutdown(wait_ms=5000)
        QApplication.processEvents()
        view.deleteLater()
        operations.deleteLater()
        assert not remaining


def _answer_folder_dialog(monkeypatch, name, *, accepted=True):
    class AnsweredDialog(LinkedFolderNameDialog):
        def exec(self):
            self.name_edit.setText(name)
            QTimer.singleShot(0, self.accept if accepted else self.reject)
            return super().exec()

    monkeypatch.setattr(list_view, "LinkedFolderNameDialog", AnsweredDialog)


@pytest.mark.parametrize("invalid", [" leading", "trailing ", "name.", ".hidden", "COM².txt", "a/b", "a\\b", "2026-10-21 MW", "🎵" * 64])
def test_linked_folder_dialog_blocks_invalid_input_and_enter(invalid):
    dialog = LinkedFolderNameDialog()
    try:
        dialog.name_edit.setText(invalid)
        assert not dialog._ok_button.isEnabled()
        assert dialog._error_label.text()
        assert dialog.name_edit.text() == invalid
        dialog.name_edit.returnPressed.emit()
        assert dialog.result() != QDialog.DialogCode.Accepted
        dialog.name_edit.setText("Reunia\u0303o")
        assert dialog._ok_button.isEnabled()
        assert not dialog._error_label.text()
        assert dialog.get_name() == "Reunião"
        dialog.name_edit.returnPressed.emit()
        assert dialog.result() == QDialog.DialogCode.Accepted
    finally:
        dialog.deleteLater()


def test_linked_folder_dialog_checks_siblings_without_changing_app_playlist_semantics():
    dialog = LinkedFolderNameDialog(existing_names=["Café", "Music"], excluding_name="Music")
    ordinary = NameDialog()
    try:
        dialog.name_edit.setText("Cafe\u0301")
        assert not dialog._ok_button.isEnabled()
        dialog.name_edit.setText("MUSIC")
        assert dialog._ok_button.isEnabled()
        ordinary.name_edit.setText("  2026-10-21 MW / Local  ")
        assert ordinary.get_name() == "2026-10-21 MW / Local"
    finally:
        dialog.deleteLater()
        ordinary.deleteLater()


def test_create_linked_folder_uses_worker_and_refreshes_empty_card(linked_folder_view, monkeypatch):
    view, root, _operations = linked_folder_view
    files = view._watched_folder_file_store
    create = files.create_folder
    enumerate_names = files.folder_entry_names
    worker_calls = []

    def create_in_worker(*args):
        assert not QThread.isMainThread()
        worker_calls.append(True)
        return create(*args)

    def enumerate_in_worker(*args):
        assert not QThread.isMainThread()
        return enumerate_names(*args)

    monkeypatch.setattr(files, "create_folder", create_in_worker)
    monkeypatch.setattr(files, "folder_entry_names", enumerate_in_worker)
    _answer_folder_dialog(monkeypatch, "Reunia\u0303o")
    assert not view._create_folder_btn.isEnabled()
    view.refresh_watched()
    _wait_for(view._create_folder_btn.isEnabled)
    assert not view._wf_section.isHidden()
    assert not view._wf_cards
    view._create_folder_btn.click()
    assert not view._create_folder_btn.isEnabled()
    _wait_for(lambda: len(view._wf_cards) == 1 and view._create_folder_btn.isEnabled())

    folder = root / "Reunião"
    assert worker_calls == [True]
    assert Path(view._wf_cards[0].path) == folder
    assert list(folder.iterdir()) == []
    assert not view._playlists
    view._persist_playlists.assert_not_called()


def test_create_linked_folder_cancel_does_not_submit_mutation(linked_folder_view, monkeypatch):
    view, root, _operations = linked_folder_view
    view.refresh_watched()
    _wait_for(view._create_folder_btn.isEnabled)
    _answer_folder_dialog(monkeypatch, "Cancelled", accepted=False)
    view._create_folder_btn.click()
    assert not view._watched_mutation_operation_ids
    assert list(root.iterdir()) == []


def test_create_linked_folder_revalidates_a_late_collision(linked_folder_view, monkeypatch):
    view, root, _operations = linked_folder_view
    view.refresh_watched()
    _wait_for(view._create_folder_btn.isEnabled)
    (root / "PLAYLIST").mkdir()
    (root / "PLAYLIST" / "keep.txt").write_text("keep")
    warnings = Mock()
    monkeypatch.setattr(QMessageBox, "critical", warnings)
    _answer_folder_dialog(monkeypatch, "Playlist")
    view._create_folder_btn.click()
    _wait_for(lambda: bool(warnings.call_count) and view._create_folder_btn.isEnabled())

    assert len(list(root.iterdir())) == 1
    assert (root / "PLAYLIST" / "keep.txt").read_text() == "keep"
    assert len(view._wf_cards) == 1


def test_linked_folder_creation_disabled_for_missing_or_unconfigured_root(linked_folder_view):
    view, root, _operations = linked_folder_view
    view.set_watched_folder(str(root / "missing"))
    _wait_for(lambda: not view._watched_scan_operation_id)
    assert not view._create_folder_btn.isEnabled()
    assert not (root / "missing").exists()
    view.set_watched_folder("")
    assert view._wf_section.isHidden()
    assert view._wf_empty_lbl.isHidden()
    assert not view._create_folder_btn.isEnabled()


def test_linked_folder_rename_uses_same_dialog_and_policy(linked_folder_view, monkeypatch):
    view, root, _operations = linked_folder_view
    source = root / "Original"
    source.mkdir()
    (source / "keep.txt").write_text("keep")
    view.refresh_watched()
    _wait_for(view._create_folder_btn.isEnabled)
    _answer_folder_dialog(monkeypatch, "Renamed")
    view._rename_watched_folder(str(source))
    _wait_for(lambda: view._create_folder_btn.isEnabled() and (root / "Renamed").is_dir())

    assert not source.exists()
    assert (root / "Renamed" / "keep.txt").read_text() == "keep"
    assert Path(view._wf_cards[0].path).name == "Renamed"


@pytest.mark.parametrize("outcome", ["commit", "failed", "cancelled"])
def test_late_folder_mutation_completion_after_cleanup_does_not_touch_ui(
    linked_folder_view, monkeypatch, outcome
):
    view, root, _operations = linked_folder_view
    pending = []
    operations = SimpleNamespace(
        submit=lambda spec: pending.append(spec) or True,
        cancel=Mock(),
    )
    mutate = Mock()
    with monkeypatch.context() as patch:
        patch.setattr(view, "_media_tree_runtime", SimpleNamespace(operations=operations))
        view._submit_watched_folder_mutation(str(root / "New"), "create", mutate)
        spec = pending[0]
        view.cleanup()
        touch_ui = Mock(side_effect=AssertionError("The closed view must not be accessed"))
        patch.setattr(view, "_update_create_folder_enabled", touch_ui)
        patch.setattr(view, "_rebuild_watched_section", touch_ui)
        patch.setattr(QMessageBox, "critical", touch_ui)

        if outcome == "commit":
            spec.commit(str(root / "New"))
        elif outcome == "failed":
            spec.failed("Folder is unavailable", False)
        else:
            spec.cancelled()
        view._submit_watched_folder_mutation(str(root / "Late"), "create", mutate)

        assert len(pending) == 1
        assert not view._watched_mutation_operation_ids
        mutate.assert_not_called()
        touch_ui.assert_not_called()
