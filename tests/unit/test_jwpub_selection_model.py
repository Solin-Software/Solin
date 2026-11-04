"""Catalog replacement keeps every observable model transition consistent."""

import pytest
from PySide6.QtCore import QPersistentModelIndex

from solin.core.jw.jwpub_catalog import JwpubCatalog, JwpubDocument, JwpubMedia
from solin.ui.qml.jwpub_selection_model import JwpubSelectionModel


def catalog(document_id="old", media_id="1", title="Content"):
    return JwpubCatalog(
        "Publication",
        "fixture",
        "fixture.jwpub",
        (JwpubDocument(document_id, title, (JwpubMedia(media_id, "Image", "image"),)),),
    )


def entries(model):
    return [model.data(model.index(row), model.ENTRY_ROLE) for row in range(model.rowCount())]


@pytest.mark.parametrize(
    "replacement",
    [
        catalog("new", "2"),
        catalog("old", "2"),
        JwpubCatalog("Empty", "fixture", "empty.jwpub", ()),
    ],
)
def test_replacement_preserves_old_data_until_the_transition_and_invalidates_indexes(
    qt_object_owner,
    replacement,
):
    model = JwpubSelectionModel(qt_object_owner)
    model.set_catalog(catalog())
    model.toggle_document("old")
    model.toggle_expanded("old")
    previous = entries(model)
    previous_index = QPersistentModelIndex(model.index(1))
    snapshots, errors = [], []

    def before_change(*_args):
        try:
            snapshots.append(entries(model))
        except Exception as error:  # noqa: BLE001 - record observer failures outside Qt dispatch
            errors.append(error)

    model.modelAboutToBeReset.connect(before_change)
    model.rowsAboutToBeRemoved.connect(before_change)
    model.set_catalog(replacement)

    assert errors == []
    assert snapshots and all(snapshot == previous for snapshot in snapshots)
    assert not previous_index.isValid()
    assert model.selected_ids == ()
    assert [entry["documentId"] for entry in entries(model)] == [
        document.id for document in replacement.documents
    ]


def test_replacement_keeps_search_and_drops_selection_expansion_and_old_previews(qt_object_owner):
    model = JwpubSelectionModel(qt_object_owner)
    model.set_catalog(catalog(title="Matching content"))
    model.set_query("matching")
    model.toggle_document("old")
    model.toggle_expanded("old")
    previews = {"old/1": "old-preview"}
    model.set_previews(previews)

    model.set_catalog(
        JwpubCatalog(
            "Replacement",
            "fixture",
            "replacement.jwpub",
            (
                catalog(title="Matching replacement").documents[0],
                catalog("other", "2", "Hidden content").documents[0],
            ),
        )
    )

    assert model.rowCount() == 1
    assert model.selected_ids == ()
    assert not entries(model)[0]["expanded"]
    model.toggle_document("old")
    model.toggle_expanded("old")
    assert entries(model)[1]["thumbnail"] == ""
    assert previews == {"old/1": "old-preview"}
