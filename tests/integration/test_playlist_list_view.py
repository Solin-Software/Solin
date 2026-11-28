from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PySide6.QtCore import QEventLoop, QThread, QTimer
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

from solin.controllers.media_operation_coordinator import MediaOperationCoordinator
from solin.core.foundation.resource_lanes import ResourceLaneRegistry
from solin.core.ingest.watched_folder_files import WatchedFolderFileStore
from solin.core.ingest.watched_folder_playlists import WatchedFolderPlaylistStore
from solin.core.media.operations import MediaOperationRecord, MediaOperationState
from solin.widgets.playlist import list_view
from solin.widgets.playlist.dialogs import LinkedFolderNameDialog, NameDialog
from solin.widgets.playlist.list_view import (
    PLAYLIST_PRIMARY_BUTTON_STYLESHEET,
    PLAYLIST_SECONDARY_BUTTON_STYLESHEET,
    PlaylistListView,
)


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


@contextmanager
def _expect_folder_operations(
    operations: MediaOperationCoordinator, **expected: MediaOperationState | None
) -> Iterator[None]:
    """Wait for the requested operations. None accepts any terminal outcome."""
    loop = QEventLoop()
    watchdog = QTimer()
    watchdog.setSingleShot(True)
    watchdog.timeout.connect(loop.quit)
    pending = expected.copy()
    finished: dict[str, MediaOperationRecord] = {}
    unexpected_outcome = False

    def on_finished(operation_id: str, _state: str) -> None:
        nonlocal unexpected_outcome
        record = operations.record(operation_id)
        if record is None or record.operation_type not in pending:
            return
        expected_state = pending.pop(record.operation_type)
        finished[record.operation_type] = record
        if expected_state is not None and record.state != expected_state:
            unexpected_outcome = True
        if unexpected_outcome or not pending:
            loop.quit()

    operations.operationFinished.connect(on_finished)
    try:
        yield
        if pending and not unexpected_outcome:
            # Let Python workers acquire the GIL while Qt delivers their results.
            watchdog.start(5000)
            loop.exec()
    finally:
        watchdog.stop()
        operations.operationFinished.disconnect(on_finished)

    if pending or unexpected_outcome:
        outcomes = {
            kind: f"{record.state.value} ({record.error})" if record.error else record.state.value
            for kind, record in finished.items()
        }
        pytest.fail(
            f"Expected folder operations {expected}, observed {outcomes}, "
            f"still waiting for {list(pending)} "
            f"(active={operations.active_count}, queued={operations.queued_count})",
            pytrace=False,
        )


@pytest.fixture
def linked_folder_errors(monkeypatch):
    errors = Mock(return_value=QMessageBox.StandardButton.Ok)
    # A real modal error dialog would block the coordinator's failure callback.
    monkeypatch.setattr(QMessageBox, "critical", errors)
    return errors


@pytest.fixture
def linked_folder_view(tmp_path, linked_folder_errors):
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
            if accepted:
                self.accept()
            else:
                self.reject()
            return self.result()

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
    view, root, operations = linked_folder_view
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
    with _expect_folder_operations(operations, linked_folder_catalog_scan=MediaOperationState.READY):
        view.refresh_watched()
    assert view._create_folder_btn.isEnabled()
    assert not view._wf_section.isHidden()
    assert not view._wf_cards
    with _expect_folder_operations(
        operations,
        create_linked_playlist_folder=MediaOperationState.READY,
        linked_folder_catalog_scan=MediaOperationState.READY,
    ):
        view._create_folder_btn.click()
        assert not view._create_folder_btn.isEnabled()
    assert view._create_folder_btn.isEnabled()
    assert len(view._wf_cards) == 1

    folder = root / "Reunião"
    assert worker_calls == [True]
    assert Path(view._wf_cards[0].path) == folder
    assert list(folder.iterdir()) == []
    assert not view._playlists
    view._persist_playlists.assert_not_called()


def test_create_linked_folder_cancel_does_not_submit_mutation(linked_folder_view, monkeypatch):
    view, root, operations = linked_folder_view
    with _expect_folder_operations(operations, linked_folder_catalog_scan=MediaOperationState.READY):
        view.refresh_watched()
    assert view._create_folder_btn.isEnabled()
    _answer_folder_dialog(monkeypatch, "Cancelled", accepted=False)
    view._create_folder_btn.click()
    assert not view._watched_mutation_operation_ids
    assert list(root.iterdir()) == []


def test_create_linked_folder_revalidates_a_late_collision(
    linked_folder_view, linked_folder_errors, monkeypatch
):
    view, root, operations = linked_folder_view
    with _expect_folder_operations(operations, linked_folder_catalog_scan=MediaOperationState.READY):
        view.refresh_watched()
    assert view._create_folder_btn.isEnabled()
    (root / "PLAYLIST").mkdir()
    (root / "PLAYLIST" / "keep.txt").write_text("keep")
    _answer_folder_dialog(monkeypatch, "Playlist")
    with _expect_folder_operations(
        operations,
        create_linked_playlist_folder=MediaOperationState.FAILED,
        linked_folder_catalog_scan=MediaOperationState.READY,
    ):
        view._create_folder_btn.click()
    linked_folder_errors.assert_called_once()
    assert view._create_folder_btn.isEnabled()

    assert len(list(root.iterdir())) == 1
    assert (root / "PLAYLIST" / "keep.txt").read_text() == "keep"
    assert len(view._wf_cards) == 1


def test_linked_folder_creation_disabled_for_missing_or_unconfigured_root(linked_folder_view):
    view, root, operations = linked_folder_view
    with _expect_folder_operations(operations, linked_folder_catalog_scan=None):
        view.set_watched_folder(str(root / "missing"))
    assert not view._create_folder_btn.isEnabled()
    assert not (root / "missing").exists()
    view.set_watched_folder("")
    assert view._wf_section.isHidden()
    assert view._wf_empty_lbl.isHidden()
    assert not view._create_folder_btn.isEnabled()


def test_linked_folder_rename_uses_same_dialog_and_policy(linked_folder_view, monkeypatch):
    view, root, operations = linked_folder_view
    source = root / "Original"
    source.mkdir()
    (source / "keep.txt").write_text("keep")
    with _expect_folder_operations(operations, linked_folder_catalog_scan=MediaOperationState.READY):
        view.refresh_watched()
    assert view._create_folder_btn.isEnabled()
    _answer_folder_dialog(monkeypatch, "Renamed")
    with _expect_folder_operations(
        operations,
        rename_linked_playlist_folder=MediaOperationState.READY,
        linked_folder_catalog_scan=MediaOperationState.READY,
    ):
        view._rename_watched_folder(str(source))

    assert view._create_folder_btn.isEnabled()
    assert not source.exists()
    assert (root / "Renamed").is_dir()
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
