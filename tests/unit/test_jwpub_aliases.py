"""Collapse proven catalog aliases without merging intentional image occurrences."""

from pathlib import Path

import pytest

from solin.core.jw.jwpub_import import JwpubImportRequest, JwpubPlaylistImportService
from tests._jwpub_catalog_fixture import IMAGE, build_alias_jwpub


@pytest.mark.parametrize("first_id", ["1", "2"])
def test_equivalent_alias_keeps_first_relation_and_does_not_become_ungrouped(tmp_path, first_id):
    sql = ""
    if first_id == "2":
        sql = "UPDATE DocumentMultimedia SET MultimediaId = 3 - MultimediaId;"
    source = build_alias_jwpub(tmp_path / "aliases.jwpub", schema_sql=sql)
    destination = tmp_path / "media"
    created = []
    with JwpubPlaylistImportService().open(JwpubImportRequest(str(source))) as session:
        assert [(doc.id, [item.id for item in doc.media]) for doc in session.catalog.documents] == [
            ("10", [first_id])
        ]
        items, _ = session.materialize(
            None, dest_images_dir=str(destination), created_paths=created
        )
    assert [item["source_item_id"] for item in items] == [first_id]
    assert len(created) == 1
    assert Path(created[0]).read_bytes() == IMAGE


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE DocumentMultimedia SET BeginParagraphOrdinal = 3, EndParagraphOrdinal = 3 "
        "WHERE MultimediaId = 2;",
        "UPDATE DocumentMultimedia SET EndParagraphOrdinal = 4 WHERE MultimediaId = 2;",
        "UPDATE DocumentMultimedia SET BeginParagraphOrdinal = NULL WHERE MultimediaId = 2;",
        "UPDATE DocumentMultimedia SET EndParagraphOrdinal = NULL WHERE MultimediaId = 2;",
        "ALTER TABLE DocumentMultimedia DROP COLUMN BeginParagraphOrdinal;",
        "ALTER TABLE DocumentMultimedia DROP COLUMN EndParagraphOrdinal;",
        "UPDATE Multimedia SET FilePath = 'images/duplicate.png' WHERE MultimediaId = 2;",
        "UPDATE Multimedia SET Label = 'Another label' WHERE MultimediaId = 2;",
        "UPDATE Multimedia SET Caption = 'Another caption' WHERE MultimediaId = 2;",
        "UPDATE Multimedia SET LabelRich = '<i>Image</i>' WHERE MultimediaId = 2;",
        "UPDATE Multimedia SET CaptionRich = '<i>Caption</i>' WHERE MultimediaId = 2;",
        "UPDATE Multimedia SET CaptionContent = X'0102' WHERE MultimediaId = 2;",
        "UPDATE Multimedia SET CreditLine = 'Credit' WHERE MultimediaId = 2;",
        "UPDATE Multimedia SET CreditLineRich = '<i>Credit</i>' WHERE MultimediaId = 2;",
        "UPDATE Multimedia SET CreditLineContent = X'0304' WHERE MultimediaId = 2;",
        "UPDATE Multimedia SET SuppressZoom = 1 WHERE MultimediaId = 2;",
        "UPDATE Multimedia SET SizeConstraint = 1 WHERE MultimediaId = 2;",
        "UPDATE Multimedia SET Specialty = 'special' WHERE MultimediaId = 2;",
        "UPDATE Multimedia SET Edition = 'edition' WHERE MultimediaId = 2;",
        "UPDATE Multimedia SET DataType = 1 WHERE MultimediaId = 2;",
        "UPDATE Multimedia SET MajorType = 2 WHERE MultimediaId = 2;",
        "UPDATE Multimedia SET MinorType = 2 WHERE MultimediaId = 2;",
        "UPDATE Multimedia SET MimeType = 'image/jpeg' WHERE MultimediaId = 2;",
        "UPDATE Multimedia SET CategoryType = 17 WHERE MultimediaId = 2;",
        "UPDATE Multimedia SET LinkMultimediaId = 1 WHERE MultimediaId = 2;",
        "UPDATE Multimedia SET KeySymbol = 'other' WHERE MultimediaId = 2;",
        "UPDATE Multimedia SET Track = 1 WHERE MultimediaId = 2;",
        "UPDATE Multimedia SET IssueTagNumber = 20270300 WHERE MultimediaId = 2;",
        "UPDATE Multimedia SET MepsDocumentId = 100 WHERE MultimediaId = 2;",
        "UPDATE Multimedia SET MepsLanguageIndex = 0 WHERE MultimediaId = 2;",
        "UPDATE Multimedia SET Width = 960 WHERE MultimediaId = 2;",
        "UPDATE Multimedia SET Height = 540 WHERE MultimediaId = 2;",
    ],
)
def test_distinct_position_member_or_presentation_preserves_both_records(tmp_path, sql):
    source = build_alias_jwpub(tmp_path / "distinct.jwpub", schema_sql=sql)
    with JwpubPlaylistImportService().open(JwpubImportRequest(str(source))) as session:
        assert [item.id for item in session.catalog.documents[0].media] == ["1", "2"]


def test_missing_dimensions_do_not_bridge_conflicting_explicit_dimensions(tmp_path):
    source = build_alias_jwpub(
        tmp_path / "ambiguous.jwpub",
        schema_sql="""
            INSERT INTO Multimedia (MultimediaId, FilePath, MimeType, CategoryType,
                Label, Caption, KeySymbol, Track, IssueTagNumber, MepsDocumentId, Width, Height)
            SELECT 3, FilePath, MimeType, CategoryType, Label, Caption, KeySymbol, Track,
                IssueTagNumber, MepsDocumentId, 960, 540 FROM Multimedia WHERE MultimediaId = 1;
            INSERT INTO DocumentMultimedia VALUES (3, 10, 3, 2, 2);
        """,
    )
    with JwpubPlaylistImportService().open(JwpubImportRequest(str(source))) as session:
        assert [item.id for item in session.catalog.documents[0].media] == ["1", "2", "3"]


@pytest.mark.parametrize("document_id", [10, 20])
def test_alias_direct_document_link_is_retained_only_when_it_adds_another_document(
    tmp_path, document_id
):
    source = build_alias_jwpub(
        tmp_path / "direct.jwpub",
        schema_sql=f"UPDATE Multimedia SET DocumentId = {document_id} WHERE MultimediaId = 2;",
    )
    with JwpubPlaylistImportService().open(JwpubImportRequest(str(source))) as session:
        expected = [("10", ["1"])]
        if document_id == 20:
            expected.append(("20", ["2"]))
        assert [(doc.id, [item.id for item in doc.media]) for doc in session.catalog.documents] == (
            expected
        )


def test_same_member_in_different_documents_is_not_an_alias(tmp_path):
    source = build_alias_jwpub(
        tmp_path / "documents.jwpub",
        schema_sql="UPDATE DocumentMultimedia SET DocumentId = 20 WHERE MultimediaId = 2;",
    )
    with JwpubPlaylistImportService().open(JwpubImportRequest(str(source))) as session:
        assert [(doc.id, [item.id for item in doc.media]) for doc in session.catalog.documents] == [
            ("10", ["1"]),
            ("20", ["2"]),
        ]


@pytest.mark.parametrize(
    "sql", ["DROP TABLE Document;", "DELETE FROM Document WHERE DocumentId = 10;"]
)
@pytest.mark.parametrize("separate_occurrence", [False, True])
def test_missing_document_fallback_keeps_surviving_occurrences_without_restoring_aliases(
    tmp_path, sql, separate_occurrence
):
    if separate_occurrence:
        sql += "INSERT INTO DocumentMultimedia VALUES (3, 10, 2, 12, 12);"
    source = build_alias_jwpub(tmp_path / "fallback.jwpub", schema_sql=sql)
    with JwpubPlaylistImportService().open(JwpubImportRequest(str(source))) as session:
        expected_ids = ["1", "2"] if separate_occurrence else ["1"]
        assert [(doc.id, [item.id for item in doc.media]) for doc in session.catalog.documents] == [
            ("ungrouped", expected_ids)
        ]


def test_alias_at_one_position_does_not_remove_same_id_at_another_position(tmp_path):
    source = build_alias_jwpub(
        tmp_path / "repeated.jwpub",
        schema_sql="INSERT INTO DocumentMultimedia VALUES (3, 10, 2, 12, 12);",
    )
    destination = tmp_path / "media"
    created = []
    service = JwpubPlaylistImportService()
    with service.open(JwpubImportRequest(str(source))) as session:
        assert [item.id for item in session.catalog.documents[0].media] == ["1", "2"]
        items, _ = session.materialize(
            None, dest_images_dir=str(destination), created_paths=created
        )
    assert [item["source_item_id"] for item in items] == ["1", "2"]
    assert items[0]["url"] == items[1]["url"]
    assert len(created) == 1
    reused = []
    with service.open(JwpubImportRequest(str(source))) as session:
        repeated, _ = session.materialize(
            ["2"], dest_images_dir=str(destination), created_paths=reused
        )
    assert repeated[0]["url"] == items[0]["url"]
    assert reused == []
    assert list(destination.iterdir()) == [Path(created[0])]
