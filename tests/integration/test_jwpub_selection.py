"""Observable selection, lazy-I/O, placement, and operation-lifetime contracts."""

from __future__ import annotations

from pathlib import Path
import threading
import time
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

from solin.controllers.media_operation_coordinator import MediaOperationCoordinator
from solin.core.jw.jwpub_import import JwpubPlaylistImportService
from solin.core.jw.publication_links import PubMediaFile
from solin.ui.qml.jwpub_selection import (
    JwpubSelectionBridge,
    JwpubSelectionDialog,
    JwpubSelectionFactory,
)
from tests._jwpub_catalog_fixture import build_catalog_jwpub
from tests._qt import dispose_widget


def wait_for(predicate, timeout=4.0):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        QApplication.processEvents()
        time.sleep(0.002)
    assert predicate()


@pytest.fixture
def selector(tmp_path, request, qt_object_owner):
    operations = MediaOperationCoordinator(parent=qt_object_owner)
    request.addfinalizer(operations.shutdown)
    resolver_calls = []

    def resolve(*args):
        resolver_calls.append(args)
        return PubMediaFile(url="https://example.invalid/media.mp4", title="Resolved media")

    settings = SimpleNamespace(path="")
    settings.source_path = lambda: settings.path
    settings.set_source_path = lambda path: setattr(settings, "path", path)
    completions = []
    insertions = []

    def insert(items, list_id, index, completed):
        insertions.append((items, list_id, index))
        completions.append(completed)

    current = [True]
    bridge = JwpubSelectionBridge(
        service=JwpubPlaylistImportService(resolve_media=resolve),
        settings=settings,
        operations=operations,
        language="T",
        dest_images_dir=str(tmp_path / "images"),
        scope_id="publication-test",
        is_current=lambda: current[0],
        placement_ref=lambda: {
            "items": [],
            "sections": [
                {"id": "a", "name": "A"},
                {"id": "b", "name": "B"},
            ],
        },
        insert=insert,
        parent=qt_object_owner,
    )
    request.addfinalizer(bridge.dispose)
    return SimpleNamespace(
        bridge=bridge,
        operations=operations,
        resolver_calls=resolver_calls,
        settings=settings,
        insertions=insertions,
        completions=completions,
        current=current,
        source=build_catalog_jwpub(tmp_path / "publication.jwpub"),
    )


def load(selector):
    selector.bridge.load_path(str(selector.source))
    wait_for(lambda: not selector.bridge.busy)
    assert selector.bridge.hasPublication
    assert not selector.bridge.errorText


def entry(model, row):
    return model.data(model.index(row), model.ENTRY_ROLE)


def test_selection_is_independent_of_filter_expansion_and_shared_media(selector):
    load(selector)
    bridge = selector.bridge
    model = bridge.model
    initial_rows = model.rowCount()
    bridge.toggleDocument("10")
    complete = model.selected_ids
    assert set(complete) == {"1", "4", "6"}
    assert model.rowCount() == initial_rows
    bridge.toggleExpanded("10")
    assert model.rowCount() == initial_rows + 3
    bridge.toggleMedia("10", "4")
    assert entry(model, 0)["checkState"] == Qt.CheckState.PartiallyChecked.value
    bridge.setSearchQuery("Second")
    assert model.rowCount() == 1
    assert model.selected_ids == tuple(value for value in complete if value != "4")
    bridge.selectAll()
    assert set(model.selected_ids) >= {"1", "2", "4", "6", "7", "9", "10"}
    assert model.rowCount() == 1  # Selecting all never expands filtered groups.
    bridge.setSearchQuery("")
    assert model.rowCount() == initial_rows + 3
    assert selector.resolver_calls == []
    assert not (selector.source.parent / "images").exists()


def test_only_requested_visible_expanded_media_produce_previews(selector, monkeypatch):
    load(selector)
    bridge = selector.bridge
    calls = []
    publication = bridge._lease.publication
    preview = publication.preview
    monkeypatch.setattr(
        publication,
        "preview",
        lambda media_id, **kw: calls.append(media_id) or preview(media_id, **kw),
    )
    bridge.selectAll()
    bridge.requestPreview("10/1")
    QTest.qWait(20)
    assert calls == []
    bridge.toggleExpanded("10")
    bridge.requestPreview("10/1")
    wait_for(lambda: "10/1" in bridge._previews)
    assert calls == ["1"]
    assert bridge._previews["10/1"].startswith("data:image/png;base64,")
    bridge.requestPreview("10/1")
    QTest.qWait(20)
    assert calls == ["1"]
    assert selector.resolver_calls == []


def test_placement_cancel_retains_refinement_and_commit_waits_for_ack(selector):
    load(selector)
    bridge = selector.bridge
    bridge.toggleDocument("10")
    bridge.toggleMedia("10", "6")
    finished = []
    bridge.finished.connect(finished.append)
    bridge.importSelected()
    assert bridge.showPlacement
    bridge.cancelSelection()
    assert not bridge.showPlacement
    assert set(bridge.model.selected_ids) == {"1", "4"}
    bridge.importSelected()
    bridge.confirmPlacement("section:a")
    wait_for(lambda: bool(selector.insertions))
    items, target, index = selector.insertions[0]
    assert target == "section:a" and index == 0
    assert [item["type"] for item in items] == ["video", "image"]
    assert len(selector.resolver_calls) == 1
    assert bridge.busy and not bridge.cancellable
    assert finished == []
    bridge.cancel()
    assert finished == []
    assert all(Path(item["url"]).is_file() for item in items if item["type"] == "image")
    selector.completions[0](True)
    assert finished == [True]


def test_rejected_target_reuses_prepared_batch_and_allows_new_placement(selector):
    load(selector)
    bridge = selector.bridge
    bridge.toggleDocument("10")
    bridge.importSelected()
    bridge.confirmPlacement("section:a")
    wait_for(lambda: bool(selector.insertions))
    selected = bridge.model.selected_ids
    selector.completions.pop(0)(False)
    assert bridge.showPlacement and not bridge.busy
    assert bridge.model.selected_ids == selected
    resolved_count = len(selector.resolver_calls)
    bridge.confirmPlacement("bottom")
    wait_for(lambda: len(selector.insertions) == 2)
    assert len(selector.insertions) == 2
    assert len(selector.resolver_calls) == resolved_count
    selector.completions.pop(0)(True)


def test_replacing_source_discards_prepared_media_from_previous_catalog(selector, tmp_path):
    load(selector)
    bridge = selector.bridge
    bridge.toggleDocument("10")
    bridge.importSelected()
    bridge.confirmPlacement("bottom")
    wait_for(lambda: bool(selector.insertions))
    paths = [Path(item["url"]) for item in selector.insertions[0][0] if item["type"] == "image"]
    selector.completions.pop(0)(False)
    replacement = build_catalog_jwpub(tmp_path / "replacement.jwpub")
    bridge.cancelSelection()
    bridge.load_path(str(replacement))
    wait_for(lambda: not bridge.busy)
    assert bridge._prepared is None
    assert not any(path.exists() for path in paths)
    assert bridge.model.selected_ids == ()


def test_refining_after_rejected_target_materializes_the_new_selection(selector):
    load(selector)
    bridge = selector.bridge
    bridge.toggleDocument("10")
    bridge.importSelected()
    bridge.confirmPlacement("bottom")
    wait_for(lambda: bool(selector.insertions))
    selector.completions.pop(0)(False)
    bridge.cancelSelection()
    bridge.toggleMedia("10", "4")
    assert bridge._prepared is None
    bridge.importSelected()
    bridge.confirmPlacement("bottom")
    wait_for(lambda: len(selector.insertions) == 2)
    assert {item["type"] for item in selector.insertions[1][0]} == {"image", "audio"}
    selector.completions.pop(0)(True)


def test_origin_change_discards_materialized_files_before_delivery(selector, monkeypatch):
    load(selector)
    bridge = selector.bridge
    entered = threading.Event()
    release = threading.Event()
    materialize = bridge._lease.publication.materialize

    def delayed(*args, **kwargs):
        result = materialize(*args, **kwargs)
        entered.set()
        assert release.wait(3)
        return result

    monkeypatch.setattr(bridge._lease.publication, "materialize", delayed)
    bridge.toggleDocument("10")
    bridge.toggleMedia("10", "4")
    bridge.toggleMedia("10", "6")
    bridge.importSelected()
    bridge.confirmPlacement("bottom")
    try:
        wait_for(entered.is_set)
        selector.current[0] = False
    finally:
        release.set()
    wait_for(lambda: not bridge._operation_ids)
    assert selector.insertions == []
    assert not list((selector.source.parent / "images").rglob("*.png"))


def test_invalid_replacement_preserves_previous_profile_source(selector, tmp_path):
    selector.bridge._public_talk = True
    selector.source = build_catalog_jwpub(
        tmp_path / "s34.jwpub",
        schema_sql="UPDATE Publication SET Symbol='S-34mp';",
    )
    load(selector)
    saved = selector.settings.path
    replacement = tmp_path / "corrupt.jwpub"
    replacement.write_bytes(b"not an archive")
    selector.bridge.load_path(str(replacement))
    wait_for(lambda: not selector.bridge.busy)
    assert selector.bridge.hasPublication
    assert selector.bridge.sourcePath == saved
    assert selector.settings.path == saved
    assert selector.bridge.errorText


def test_public_talk_forces_placement_unless_semantic_target_is_available(selector):
    load(selector)
    bridge = selector.bridge
    bridge._public_talk = True
    bridge._placement_ref = lambda: {"items": [], "sections": []}
    bridge.toggleDocument("10")
    bridge.importSelected()
    assert bridge.showPlacement
    bridge.cancelSelection()
    bridge._auto_target = lambda: ("section:official", 2**31 - 1)
    bridge.importSelected()
    wait_for(lambda: bool(selector.insertions))
    assert not bridge.showPlacement
    assert selector.insertions[0][1:] == ("section:official", 2**31 - 1)
    selector.completions[0](True)


def test_qml_loads_catalog_and_refinement_without_eager_previews(selector, request):
    bridge = selector.bridge
    dialog = JwpubSelectionDialog(bridge)
    request.addfinalizer(lambda: dispose_widget(dialog))
    dialog.show()
    QApplication.processEvents()
    assert dialog.qml_widget.errors() == []
    load(selector)
    bridge.toggleDocument("10")
    assert bridge._previews == {}
    bridge.toggleExpanded("10")
    wait_for(lambda: bool(bridge._previews))
    assert selector.resolver_calls == []
    assert dialog.qml_widget.rootObject() is not None


def test_cancelled_backend_preview_requeues_when_visible_again(selector, monkeypatch):
    load(selector)
    bridge = selector.bridge
    bridge.toggleDocument("10")
    bridge.toggleExpanded("10")
    publication = bridge._lease.publication
    preview = publication.preview
    entered, release = threading.Event(), threading.Event()
    calls = []

    def delayed(media_id, **kwargs):
        calls.append(media_id)
        if len(calls) == 1:
            entered.set()
            assert release.wait(3)
        return preview(media_id, **kwargs)

    monkeypatch.setattr(publication, "preview", delayed)
    bridge.requestPreview("10/1")
    try:
        wait_for(entered.is_set)
        bridge.releasePreview("10/1")
        bridge.requestPreview("10/1")
    finally:
        release.set()
    wait_for(lambda: "10/1" in bridge._previews)
    assert calls == ["1", "1"]
    assert selector.resolver_calls == []


def test_closed_dialog_retains_cleanup_owner_until_worker_finishes(selector, monkeypatch):
    load(selector)
    bridge = selector.bridge
    bridge.toggleDocument("10")
    bridge.toggleMedia("10", "4")
    bridge.toggleMedia("10", "6")
    bridge.importSelected()
    bridge.confirmPlacement("bottom")
    wait_for(lambda: bool(selector.insertions))
    selector.completions.pop()(False)
    bridge.cancelSelection()
    created = [Path(path) for path in bridge._created_paths]
    assert created and all(path.exists() for path in created)
    entered, release = threading.Event(), threading.Event()
    publication = bridge._lease.publication
    preview = publication.preview

    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return preview(*args, **kwargs)

    monkeypatch.setattr(publication, "preview", delayed)
    bridge.toggleExpanded("10")
    bridge.requestPreview("10/1")
    try:
        wait_for(entered.is_set)
        dialog = JwpubSelectionDialog(bridge)
        bridge.setParent(dialog)
        dialog.dispose()
        QCoreApplication.sendPostedEvents(dialog, QEvent.Type.DeferredDelete)
        assert any(path.exists() for path in created)
    finally:
        release.set()
    wait_for(lambda: all(not path.exists() for path in created))


def test_failed_delivery_closes_an_expired_origin(selector, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    outcomes = []
    selector.bridge.finished.connect(outcomes.append)

    def failing(*_args, **_kwargs):
        entered.set()
        assert release.wait(3)
        raise OSError("Source unavailable")

    monkeypatch.setattr(selector.bridge._service, "open", failing)
    selector.bridge.load_path(str(selector.source))
    try:
        wait_for(entered.is_set)
        selector.current[0] = False
    finally:
        release.set()
    wait_for(lambda: bool(outcomes))
    assert outcomes == [False]
    assert selector.insertions == []


def test_factory_reports_unresolved_references_after_successful_insertion(selector, monkeypatch):
    warnings, inserted, outcomes = [], [], []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: warnings.append(args))

    class ConfirmingDialog(JwpubSelectionDialog):
        def exec(self):
            self.bridge.finished.connect(outcomes.append)
            wait_for(lambda: self.bridge.hasPublication)
            self.bridge.toggleDocument("10")
            self.bridge.toggleMedia("10", "1")
            self.bridge.toggleMedia("10", "6")
            self.bridge.importSelected()
            wait_for(lambda: bool(outcomes))
            return QDialog.DialogCode.Accepted

    def insert(items, _list_id, _index, completed):
        inserted.extend(items)
        completed(True)

    factory = JwpubSelectionFactory(
        selector.settings,
        service=JwpubPlaylistImportService(resolve_media=lambda *_args: None),
        dialog_factory=ConfirmingDialog,
    )
    factory.select(
        [str(selector.source)],
        parent=None,
        operations=selector.operations,
        language="T",
        dest_images_dir=str(selector.source.parent / "images"),
        scope_id="partial-factory",
        is_current=lambda: True,
        placement_ref=lambda: None,
        insert=insert,
    )
    assert outcomes == [True]
    assert len(warnings) == 1
    assert len(inserted) == 1
    assert inserted[0]["url"] == ""
    assert inserted[0]["key_symbol"]
