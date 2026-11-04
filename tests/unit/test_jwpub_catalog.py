from __future__ import annotations

import io
import hashlib
import os
import shutil
import stat
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from solin.core.jw import jwpub_import
from solin.core.jw.jwpub_import import (
    JwpubImportCancelled,
    JwpubImportRequest,
    JwpubPlaylistImportService,
    JwpubSourceChangedError,
)
from solin.core.jw.publication_links import PubMediaFile
from solin.core.storage import archive_budget
from tests._jwpub_catalog_fixture import IMAGE, PREVIEW, build_catalog_jwpub


@pytest.fixture
def source(tmp_path):
    return build_catalog_jwpub(tmp_path / "publication.jwpub")


def _open(source, resolver=None):
    return JwpubPlaylistImportService(resolver).open(JwpubImportRequest(str(source), language="S"))


def test_catalog_is_immutable_grouped_ordered_and_collapses_repeated_relations(source):
    with _open(source) as session:
        catalog = session.catalog
        assert catalog.title == "Synthetic publication"
        assert catalog.symbol == "fixture"
        assert catalog.source_path == str(source)
        assert [document.id for document in catalog.documents] == ["10", "20", "ungrouped"]
        assert [[item.id for item in document.media] for document in catalog.documents] == [
            ["4", "1", "6"],
            ["2", "7", "4", "10"],
            ["9"],
        ]
        assert all(
            item.id not in {"3", "5", "8"} for doc in catalog.documents for item in doc.media
        )
        video = catalog.documents[0].media[0]
        assert video.preview_member_path == "preview.png"
        assert video.media_type == "video"
        with pytest.raises(FrozenInstanceError):
            catalog.title = "changed"
        with pytest.raises(FrozenInstanceError):
            video.title = "changed"


@pytest.mark.parametrize(
    "direct,grouped,expected",
    [
        (True, True, [["1", "4", "6"], ["2", "7", "10"], ["9"]]),
        (False, False, [["1", "2", "4", "6", "7", "9", "10"]]),
        (True, False, [["1", "2", "4", "6", "7", "9", "10"]]),
    ],
)
def test_direct_document_variant_and_fallback_group(tmp_path, direct, grouped, expected):
    source = build_catalog_jwpub(tmp_path / "variant.jwpub", direct=direct, grouped=grouped)
    with _open(source) as session:
        assert [[item.id for item in doc.media] for doc in session.catalog.documents] == expected


def test_open_does_not_extract_images_and_preview_reads_only_requested_member(source, monkeypatch):
    calls = []
    original = zipfile.ZipFile.open

    def track(archive, name, *args, **kwargs):
        calls.append(name.filename if isinstance(name, zipfile.ZipInfo) else name)
        return original(archive, name, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "open", track)

    def no_network(*_args):
        pytest.fail("Catalog inspection and preview must not resolve URLs")

    with _open(source, no_network) as session:
        assert set(calls) == {"contents", "manifest.json", "nested/publication.db"}
        assert session.preview("4") == PREVIEW
        assert session.preview("4") == PREVIEW
        assert calls.count("preview.png") == 1
        assert session.preview("6") is None
        assert "images/first.png" not in calls


def test_selection_materializes_in_catalog_order_only_and_preserves_language(source, tmp_path):
    calls = []

    def resolve(*args):
        calls.append(args)
        return PubMediaFile(url="https://example.test/media", title="Resolved")

    destination = tmp_path / "images"
    created = []
    with _open(source, resolve) as session:
        items, stem = session.materialize(
            ["6", "1", "4", "4"],
            dest_images_dir=str(destination),
            created_paths=created,
        )
    assert [item["source_item_id"] for item in items] == ["4", "1", "6"]
    assert [call[-1] for call in calls] == ["T", "E"]
    assert [item["meps_language"] for item in items] == [5, 5, 0]
    assert stem == source.stem
    assert len(created) == 1
    assert Path(created[0]).read_bytes() == IMAGE
    assert {path.name for path in destination.iterdir()} == {Path(created[0]).name}


def test_shared_physical_outputs_are_stable_across_members_sessions_and_imports(source, tmp_path):
    destination = str(tmp_path / "images")
    created = []
    with _open(source) as session:
        first, _ = session.materialize(
            ["1", "2"], dest_images_dir=destination, created_paths=created
        )
        second_created = []
        second, _ = session.materialize(
            ["2"], dest_images_dir=destination, created_paths=second_created
        )
    with _open(source) as session:
        third, _ = session.materialize(
            ["1"], dest_images_dir=destination, created_paths=second_created
        )
    assert first[0]["url"] == first[1]["url"] == second[0]["url"] == third[0]["url"]
    assert len(created) == 1
    assert second_created == []
    assert len(list(Path(destination).iterdir())) == 1


def test_local_video_needs_no_network_and_remains_usable_after_close(source, tmp_path):
    def no_network(*_args):
        pytest.fail("Embedded media must be used locally")

    created = []
    with _open(source, no_network) as session:
        items, _ = session.materialize(
            ["7"], dest_images_dir=str(tmp_path / "media"), created_paths=created
        )
    assert items[0]["type"] == "video"
    assert Path(items[0]["url"]).read_bytes() == b"embedded video bytes"
    assert created == [items[0]["url"]]


@pytest.mark.parametrize("fails", [False, True])
def test_unresolved_media_preserves_recoverable_reference(source, tmp_path, fails):
    def resolve(*_args):
        if fails:
            raise OSError("offline")
        return None

    with _open(source, resolve) as session:
        items, _ = session.materialize(["10"], dest_images_dir=str(tmp_path / "media"))
    assert items[0]["url"] == ""
    assert items[0]["doc_id"] == 990
    assert items[0]["meps_language"] == 5


def test_no_destination_whole_import_retains_usable_outputs(source):
    items, _ = JwpubPlaylistImportService().read(
        JwpubImportRequest(str(source), resolve_urls=False)
    )
    paths = {Path(item["url"]) for item in items if item["url"]}
    try:
        assert paths
        assert all(path.is_file() for path in paths)
        assert {item["source_item_id"] for item in items} == {"1", "2", "4", "6", "7", "9", "10"}
    finally:
        for root in {path.parent for path in paths}:
            shutil.rmtree(root)


def test_empty_and_unknown_selection_has_no_outputs_or_network(source, tmp_path):
    destination = tmp_path / "media"
    with _open(source) as session:
        assert session.materialize([], dest_images_dir=str(destination)) == ([], source.stem)
        with pytest.raises(ValueError, match="Selection"):
            session.materialize(["absent"], dest_images_dir=str(destination))
    assert not destination.exists()


@pytest.mark.parametrize("change", ["modify", "replace", "remove"])
def test_changed_source_is_rejected_before_preview_or_materialization(source, tmp_path, change):
    with _open(source) as session:
        if change == "modify":
            with source.open("ab") as output:
                output.write(b"change")
        elif change == "remove":
            source.unlink()
        else:
            replacement = tmp_path / "replacement"
            shutil.copy2(source, replacement)
            os.replace(replacement, source)
        with pytest.raises(JwpubSourceChangedError):
            session.validate_source()
        with pytest.raises(JwpubSourceChangedError):
            session.preview("1")
        with pytest.raises(JwpubSourceChangedError):
            session.materialize(["1"], dest_images_dir=str(tmp_path / "media"))


def test_source_change_during_resolution_rolls_back_new_files(source, tmp_path):
    def replace(*_args):
        source.write_bytes(b"replacement")
        return None

    created = ["previous-owned-output"]
    destination = tmp_path / "media"
    with _open(source, replace) as session:
        with pytest.raises(JwpubSourceChangedError):
            session.materialize(["1", "6"], dest_images_dir=str(destination), created_paths=created)
    assert not list(destination.iterdir())
    assert created == ["previous-owned-output"]


def test_cancel_after_reused_and_new_outputs_removes_only_new_files(source, tmp_path):
    cancelled = False

    def resolve(*_args):
        nonlocal cancelled
        cancelled = True
        return None

    destination = tmp_path / "media"
    with _open(source, resolve) as session:
        existing, _ = session.materialize(["1"], dest_images_dir=str(destination))
        created = []

        # Catalog order puts the orphan after remote media, so append a second
        # selection with a callback that cancels after local staging appears.
        def cancel_during_copy():
            return len(list(destination.glob("*.part"))) > 0

        with pytest.raises(JwpubImportCancelled):
            session.materialize(
                ["1", "9"],
                dest_images_dir=str(destination),
                cancelled=cancel_during_copy,
                created_paths=created,
            )
        assert Path(existing[0]["url"]).is_file()
        assert list(destination.iterdir()) == [Path(existing[0]["url"])]
        assert created == []
        with pytest.raises(JwpubImportCancelled):
            session.materialize(
                ["1", "6"],
                dest_images_dir=str(destination),
                cancelled=lambda: cancelled,
                created_paths=created,
            )
        assert created == []


def test_close_is_idempotent_cleans_snapshot_and_rejects_future_operations(source, monkeypatch):
    roots = []
    original = jwpub_import.tempfile.TemporaryDirectory

    def track(*args, **kwargs):
        root = original(*args, **kwargs)
        roots.append(Path(root.name))
        return root

    monkeypatch.setattr(jwpub_import.tempfile, "TemporaryDirectory", track)
    session = _open(source)
    assert {p.name for p in roots[0].iterdir()} == {"contents.zip", "publication.db"}
    session.close()
    session.close()
    assert not roots[0].exists()
    with pytest.raises(RuntimeError, match="closed"):
        session.preview("1")


def test_parallel_previews_and_materialization_serialize_safely(source, tmp_path):
    with _open(source) as session, ThreadPoolExecutor(max_workers=3) as pool:
        previews = [pool.submit(session.preview, "4") for _ in range(2)]
        imported = pool.submit(session.materialize, ["1"], dest_images_dir=str(tmp_path / "media"))
        assert [future.result() for future in previews] == [PREVIEW, PREVIEW]
        assert Path(imported.result()[0][0]["url"]).read_bytes() == IMAGE


@pytest.mark.parametrize("layer", ["outer", "inner"])
@pytest.mark.parametrize(
    "name", ["../escape", "C:/escape", "a\\escape", "a/../escape", "nul.txt", "/absolute"]
)
def test_unsafe_archive_members_rejected_in_both_layers(tmp_path, layer, name):
    source = build_catalog_jwpub(tmp_path / "unsafe.jwpub", **{f"extra_{layer}": {name: b"bad"}})
    with pytest.raises(ValueError):
        _open(source)


@pytest.mark.parametrize("kind", ["bytes", "members"])
def test_shared_archive_budget_accounts_for_both_layers(source, monkeypatch, kind):
    with zipfile.ZipFile(source) as outer:
        outer_bytes = sum(info.file_size for info in outer.infolist())
        outer_members = len(outer.infolist())
    if kind == "bytes":
        monkeypatch.setattr(archive_budget, "MAX_EXPANDED_BYTES", outer_bytes)
    else:
        monkeypatch.setattr(archive_budget, "MAX_ARCHIVE_MEMBERS", outer_members)
    with pytest.raises(ValueError, match="budget"):
        _open(source)


def test_cancel_open_cleans_temporary_snapshot(source, monkeypatch):
    roots = []
    original = jwpub_import.tempfile.TemporaryDirectory
    count = 0

    def cancelled():
        nonlocal count
        count += 1
        return count > 4

    def track(*args, **kwargs):
        directory = original(*args, **kwargs)
        roots.append(Path(directory.name))
        return directory

    monkeypatch.setattr(jwpub_import.tempfile, "TemporaryDirectory", track)
    with pytest.raises(JwpubImportCancelled):
        JwpubPlaylistImportService().open(JwpubImportRequest(str(source)), cancelled=cancelled)
    assert roots and all(not path.exists() for path in roots)


def test_symbolic_archive_member_rejected(tmp_path):
    source = build_catalog_jwpub(tmp_path / "symlink.jwpub")
    with zipfile.ZipFile(source, "a") as outer:
        info = zipfile.ZipInfo("linked")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        outer.writestr(info, "outside")
    with pytest.raises(ValueError, match="symbolic"):
        _open(source)


def test_case_collisions_and_file_directory_conflicts_rejected(tmp_path):
    for name in ["Images/FIRST.PNG", "images"]:
        source = build_catalog_jwpub(tmp_path / "collision.jwpub", extra_inner={name: b"conflict"})
        with pytest.raises(ValueError, match="conflict"):
            _open(source)


def test_missing_contents_or_database_rejected_and_invalid_image_has_no_preview(tmp_path):
    empty = tmp_path / "empty.jwpub"
    with zipfile.ZipFile(empty, "w") as archive:
        archive.writestr("other", b"nothing")
    with pytest.raises(ValueError, match="contents"):
        _open(empty)
    contents = io.BytesIO()
    with zipfile.ZipFile(contents, "w") as inner:
        inner.writestr("file", b"none")
    with zipfile.ZipFile(empty, "w") as outer:
        outer.writestr("contents", contents.getvalue())
    with pytest.raises(ValueError, match="database"):
        _open(empty)
    source = build_catalog_jwpub(
        tmp_path / "bad-image.jwpub", extra_inner={"preview.png": b"not an image"}
    )
    with _open(source) as session:
        assert session.preview("4") is None


@pytest.mark.parametrize("category", [9, 10, 15, 25])
def test_decorative_categories_are_excluded(tmp_path, category):
    source = build_catalog_jwpub(
        tmp_path / "decoration.jwpub",
        schema_sql=f"UPDATE Multimedia SET CategoryType={category} WHERE MultimediaId=1",
    )
    with _open(source) as session:
        assert all(item.id != "1" for doc in session.catalog.documents for item in doc.media)


@pytest.mark.parametrize("reverse", [False, True])
def test_linked_preview_is_not_imported_even_when_category_is_content(tmp_path, reverse):
    sql = "UPDATE Multimedia SET CategoryType=8 WHERE MultimediaId=5;"
    if reverse:
        sql += """UPDATE Multimedia SET LinkMultimediaId=NULL WHERE MultimediaId=4;
                  UPDATE Multimedia SET LinkMultimediaId=4 WHERE MultimediaId=5;"""
    source = build_catalog_jwpub(tmp_path / "preview.jwpub", schema_sql=sql)
    with _open(source) as session:
        assert session.preview("4") == PREVIEW
        assert all(item.id != "5" for doc in session.catalog.documents for item in doc.media)


def test_missing_optional_schema_uses_manifest_and_generic_group(tmp_path):
    source = build_catalog_jwpub(
        tmp_path / "minimal.jwpub",
        schema_sql="""
        DROP TABLE Publication; DROP TABLE Document; DROP TABLE DocumentMultimedia;
        ALTER TABLE Multimedia RENAME TO Original;
        CREATE TABLE Multimedia (MultimediaId INTEGER PRIMARY KEY, FilePath TEXT, MimeType TEXT);
        INSERT INTO Multimedia SELECT MultimediaId,FilePath,MimeType FROM Original
            WHERE MultimediaId=1;
        DROP TABLE Original;
    """,
    )
    with _open(source) as session:
        assert session.catalog.title == "Manifest title"
        assert session.catalog.symbol == "manifest"
        assert len(session.catalog.documents) == 1
        assert session.catalog.documents[0].media[0].meps_language == 5
        assert session.preview("1") == IMAGE


def test_empty_publication_has_valid_empty_catalog(tmp_path):
    source = build_catalog_jwpub(tmp_path / "empty.jwpub", schema_sql="DELETE FROM Multimedia;")
    with _open(source) as session:
        assert session.catalog.documents == ()
        assert session.materialize(None, dest_images_dir=str(tmp_path / "out"))[0] == []


def test_cancellation_after_publishing_new_file_rolls_back_output(source, tmp_path):
    destination = tmp_path / "media"
    created = ["previous"]
    with _open(source) as session:
        with pytest.raises(JwpubImportCancelled):
            session.materialize(
                ["1", "9"],
                dest_images_dir=str(destination),
                created_paths=created,
                cancelled=lambda: destination.exists() and any(destination.glob("*.png")),
            )
    assert created == ["previous"]
    assert list(destination.iterdir()) == []


def test_failure_after_new_output_preserves_conflicting_preexisting_file(source, tmp_path):
    destination = tmp_path / "media"
    destination.mkdir()
    conflicting = destination / f"{hashlib.sha256(IMAGE + b'orphan').hexdigest()}.png"
    conflicting.write_bytes(b"a preexisting damaged resource")
    created = []
    with _open(source) as session:
        with pytest.raises(ValueError):
            session.materialize(["1", "9"], dest_images_dir=str(destination), created_paths=created)
    assert created == []
    assert list(destination.iterdir()) == [conflicting]
    assert conflicting.read_bytes() == b"a preexisting damaged resource"


def test_reused_materialization_does_not_reread_archive_or_removed_outputs(
    source, tmp_path, monkeypatch
):
    destination = str(tmp_path / "media")
    with _open(source) as session:
        first, _ = session.materialize(["1"], dest_images_dir=destination)
        real_open = zipfile.ZipFile.open
        reads = []

        def track(archive, name, *args, **kwargs):
            reads.append(name)
            return real_open(archive, name, *args, **kwargs)

        monkeypatch.setattr(zipfile.ZipFile, "open", track)
        assert session.materialize(["1"], dest_images_dir=destination)[0] == first
        assert reads == []
        Path(first[0]["url"]).unlink()
        created = []
        session.materialize(["1"], dest_images_dir=destination, created_paths=created)
        assert created == [first[0]["url"]]
        assert Path(created[0]).read_bytes() == IMAGE


def test_reused_output_changed_after_first_import_is_revalidated(source, tmp_path):
    destination = str(tmp_path / "media")
    with _open(source) as session:
        first, _ = session.materialize(["1"], dest_images_dir=destination)
        Path(first[0]["url"]).write_bytes(b"changed content")
        with pytest.raises(ValueError):
            session.materialize(["1"], dest_images_dir=destination)


def test_source_change_preserving_size_and_timestamp_is_rejected(source):
    with _open(source) as session:
        original = source.stat()
        replacement = bytearray(source.read_bytes())
        replacement[-1] ^= 1
        source.write_bytes(replacement)
        os.utime(source, ns=(original.st_atime_ns, original.st_mtime_ns))
        with pytest.raises(JwpubSourceChangedError):
            session.validate_source()


def test_cancellation_during_source_validation_preserves_cancellation_type(source, tmp_path):
    with _open(source) as session:
        with pytest.raises(JwpubImportCancelled):
            session.validate_source(cancelled=lambda: True)
        with pytest.raises(JwpubImportCancelled):
            session.materialize(
                ["1"], dest_images_dir=str(tmp_path / "out"), cancelled=lambda: True
            )


def test_filesystem_without_hardlinks_uses_exclusive_copy_and_reuses_outputs(
    source, tmp_path, monkeypatch
):
    def unavailable(*_args):
        raise OSError("Hard links unsupported by this filesystem")

    monkeypatch.setattr(jwpub_import.os, "link", unavailable)
    created = []
    with _open(source) as session:
        items, _ = session.materialize(
            ["1", "2"], dest_images_dir=str(tmp_path / "media"), created_paths=created
        )
    assert len(created) == 1
    assert items[0]["url"] == items[1]["url"]
    assert Path(created[0]).read_bytes() == IMAGE


def test_concurrent_sessions_cannot_adopt_another_uncommitted_batch(source, tmp_path):
    entered = threading.Event()
    release = threading.Event()
    cancelled = threading.Event()
    destination = str(tmp_path / "media")

    def resolve(*_args):
        entered.set()
        assert release.wait(timeout=5)
        return None

    with (
        _open(source, resolve) as first,
        _open(source) as second,
        ThreadPoolExecutor(max_workers=2) as pool,
    ):
        first_created, second_created = [], []
        pending = pool.submit(
            first.materialize,
            ["1", "6"],
            dest_images_dir=destination,
            cancelled=cancelled.is_set,
            created_paths=first_created,
        )
        try:
            assert entered.wait(timeout=5)
            following = pool.submit(
                second.materialize,
                ["1"],
                dest_images_dir=destination,
                created_paths=second_created,
            )
            with pytest.raises(TimeoutError):
                following.result(timeout=0.1)
            cancelled.set()
            release.set()
            with pytest.raises(JwpubImportCancelled):
                pending.result(timeout=5)
            items, _ = following.result(timeout=5)
            assert first_created == []
            assert second_created == [items[0]["url"]]
            assert Path(items[0]["url"]).read_bytes() == IMAGE
        finally:
            cancelled.set()
            release.set()


def test_staging_close_failure_cleans_partial_file(source, tmp_path, monkeypatch):
    original = jwpub_import.tempfile.NamedTemporaryFile

    class FailingClose:
        def __init__(self, *args, **kwargs):
            self.file = original(*args, **kwargs)

        def __enter__(self):
            return self.file.__enter__()

        def __exit__(self, *exc):
            self.file.__exit__(*exc)
            raise OSError("injected close failure")

    monkeypatch.setattr(jwpub_import.tempfile, "NamedTemporaryFile", FailingClose)
    destination = tmp_path / "media"
    with _open(source) as session:
        with pytest.raises(OSError, match="close failure"):
            session.materialize(["1"], dest_images_dir=str(destination))
    assert list(destination.iterdir()) == []
