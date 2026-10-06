"""Resource transport and local filesystem failure contracts."""

import hashlib
import os
from pathlib import Path
import shutil
import unicodedata

import pytest

from solin.core.ingest.sync import resources
from solin.core.ingest.sync.discovery import (
    ACKNOWLEDGED,
    DISCOVERED,
    OVERRIDE_PREFIX,
    RESOURCE,
    reconcile_discoveries,
    record_discovery_edits,
    suppression_record,
)


@pytest.mark.parametrize(
    "name",
    [
        "../image.jpg",
        "/outside.jpg",
        "C:relative.jpg",
        "a/../b.jpg",
        "CON.jpg",
        "nul",
        "trailing. /image.jpg",
        "a:stream",
        "a?.jpg",
        ".solin_sync/ops.json",
        "image.solin-stage",
        "",
    ],
)
def test_rejects_nonportable_resource_paths(tmp_path, name):
    with pytest.raises(ValueError):
        resources.portable_resource_key(name, tmp_path)


def test_resource_keys_preserve_portable_paths_and_remote_urls(tmp_path):
    path = tmp_path / "Dé glória.jpg"
    path.write_bytes(b"image")
    assert resources.portable_resource_key(str(path), tmp_path) == path.name
    assert resources.portable_resource_key("nested\\image.jpg", tmp_path) == "nested/image.jpg"
    assert (
        resources.portable_resource_key("https://example.org/A.jpg?a=B", tmp_path)
        == "https://example.org/A.jpg?a=B"
    )


def test_automatic_identity_agrees_on_case_unicode_and_separators():
    nfc = "Media/Dé glória.jpg"
    nfd = unicodedata.normalize("NFD", nfc).upper().replace("/", "\\")
    assert resources.automatic_occurrence_id(nfc) == resources.automatic_occurrence_id(nfd)
    assert resources.automatic_occurrence_id(nfc) != resources.automatic_occurrence_id("other.jpg")


def test_case_collisions_fail_explicitly(tmp_path):
    first = tmp_path / "Image.jpg"
    second = tmp_path / "image.jpg"
    first.write_bytes(b"one")
    second.write_bytes(b"two")
    if len(list(tmp_path.iterdir())) != 2:
        pytest.skip("Filesystem is case insensitive")
    with pytest.raises(ValueError, match="collide"):
        resources.portable_resource_key("Image.jpg", tmp_path)


def test_collision_policy_is_identical_on_case_insensitive_machines(tmp_path, monkeypatch):
    original = Path.iterdir

    def colliding_names(path):
        if path == tmp_path:
            return iter([tmp_path / "Image.jpg", tmp_path / "image.jpg"])
        return original(path)

    monkeypatch.setattr(Path, "iterdir", colliding_names)
    with pytest.raises(ValueError, match="collide"):
        resources.portable_resource_key("Image.jpg", tmp_path)


def test_name_validation_indexes_an_unchanged_directory_once(tmp_path, monkeypatch):
    for index in range(100):
        (tmp_path / f"image{index}.jpg").write_bytes(b"image")
    original = Path.iterdir
    calls = []

    def counting_names(path):
        if path == tmp_path:
            calls.append(path)
        return original(path)

    monkeypatch.setattr(Path, "iterdir", counting_names)
    for index in range(100):
        assert resources.portable_resource_key(f"image{index}.jpg", tmp_path)
    assert len(calls) == 1
    (tmp_path / "new.jpg").write_bytes(b"new")
    assert resources.portable_resource_key("new.jpg", tmp_path) == "new.jpg"
    assert len(calls) == 2


def test_resource_symlink_cannot_escape_linked_folder(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    try:
        (root / "escape").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Symlink creation unavailable")
    with pytest.raises(ValueError, match="escapes"):
        resources.portable_resource_key("escape/image.jpg", root)


def test_hash_is_portable_and_cached_but_return_value_is_not_mutable_cache(tmp_path, monkeypatch):
    source = tmp_path / "image.jpg"
    source.write_bytes(b"image")
    digest = hashlib.file_digest
    calls = []

    def track(*args):
        calls.append(True)
        return digest(*args)

    monkeypatch.setattr(hashlib, "file_digest", track)
    first = resources.content_signature(source)
    assert first == {"sha256": hashlib.sha256(b"image").hexdigest(), "size": 5}
    first["size"] = 999
    assert resources.content_signature(source)["size"] == 5
    assert len(calls) == (2 if os.name == "nt" else 1)


def test_same_size_and_mtime_replacement_invalidates_hash(tmp_path):
    source = tmp_path / "image.jpg"
    source.write_bytes(b"old")
    info = source.stat()
    before = resources.content_signature(source)
    replacement = tmp_path / "replacement.jpg"
    replacement.write_bytes(b"new")
    os.utime(replacement, ns=(info.st_atime_ns, info.st_mtime_ns))
    os.replace(replacement, source)
    assert resources.content_signature(source) != before


def test_same_size_and_mtime_in_place_write_invalidates_hash(tmp_path):
    source = tmp_path / "image.jpg"
    source.write_bytes(b"old")
    info = source.stat()
    before = resources.content_signature(source)
    source.write_bytes(b"new")
    os.utime(source, ns=(info.st_atime_ns, info.st_mtime_ns))
    assert resources.content_signature(source) != before


def test_content_changed_during_hash_is_retryable(tmp_path, monkeypatch):
    source = tmp_path / "image.jpg"
    source.write_bytes(b"old")
    revision = resources._revision
    count = 0

    def change_revision(fd):
        nonlocal count
        count += 1
        result = revision(fd)
        return (*result[:-1], result[-1] + count)

    monkeypatch.setattr(resources, "_revision", change_revision)
    with pytest.raises(OSError, match="changed"):
        resources.content_signature(source)


def test_retired_file_recovers_after_archive_arrives_on_another_replica(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    source = first / "image.jpg"
    source.write_bytes(b"shared image")
    assert resources.retire_file(first, source.name)
    assert not source.exists()
    shutil.copytree(first / ".solin_sync", second / ".solin_sync")
    assert resources.recover_file(second, "image.jpg")
    assert (second / "image.jpg").read_bytes() == b"shared image"
    assert list((second / ".solin_sync" / "resources").rglob("*.json"))
    assert not list(second.rglob("*.solin-stage"))


def test_unlink_failure_preserves_original_and_recovery_copy(tmp_path, monkeypatch):
    source = tmp_path / "image.jpg"
    source.write_bytes(b"image")
    unlink = Path.unlink

    def locked(path, *args, **kwargs):
        if path == source:
            raise PermissionError("locked")
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", locked)
    with pytest.raises(PermissionError, match="locked"):
        resources.retire_file(tmp_path, source.name)
    assert source.read_bytes() == b"image"
    assert list((tmp_path / ".solin_sync" / "resources").rglob("*.json"))
    monkeypatch.setattr(Path, "unlink", unlink)
    assert resources.retire_file(tmp_path, source.name)
    assert resources.recover_file(tmp_path, source.name)


def test_archive_publication_failure_keeps_original(tmp_path, monkeypatch):
    source = tmp_path / "image.jpg"
    source.write_bytes(b"image")

    def blocked(*_args):
        raise PermissionError("cloud lock")

    monkeypatch.setattr(os, "replace", blocked)
    with pytest.raises(PermissionError):
        resources.retire_file(tmp_path, source.name)
    assert source.read_bytes() == b"image"
    assert not list(tmp_path.rglob("*.solin-stage"))


def test_recovery_keeps_new_visible_content(tmp_path):
    source = tmp_path / "image.jpg"
    source.write_bytes(b"old")
    resources.retire_file(tmp_path, source.name)
    source.write_bytes(b"new")
    assert resources.recover_file(tmp_path, source.name)
    assert source.read_bytes() == b"new"


def test_incompatible_archived_versions_require_resolution(tmp_path):
    source = tmp_path / "image.jpg"
    for value in (b"old", b"new"):
        source.write_bytes(value)
        resources.retire_file(tmp_path, source.name)
    with pytest.raises(OSError, match="Multiple"):
        resources.recover_file(tmp_path, source.name)
    assert not source.exists()


def test_archive_metadata_before_content_keeps_resource_unavailable(tmp_path):
    source = tmp_path / "image.jpg"
    source.write_bytes(b"image")
    resources.retire_file(tmp_path, source.name)
    digest = hashlib.sha256(b"image").hexdigest()
    for archived in (tmp_path / ".solin_sync").rglob(digest):
        archived.unlink()
    assert not resources.recover_file(tmp_path, source.name)


def test_corrupt_archive_does_not_publish_visible_resource(tmp_path):
    source = tmp_path / "image.jpg"
    source.write_bytes(b"image")
    resources.retire_file(tmp_path, source.name)
    digest = hashlib.sha256(b"image").hexdigest()
    archived = next((tmp_path / ".solin_sync").rglob(digest))
    archived.write_bytes(b"wrong")
    with pytest.raises(OSError, match="does not match"):
        resources.recover_file(tmp_path, source.name)
    assert not source.exists()


def test_recovery_does_not_overwrite_a_concurrent_visible_publication(tmp_path, monkeypatch):
    source = tmp_path / "image.jpg"
    source.write_bytes(b"old")
    resources.retire_file(tmp_path, source.name)
    publish_name = "rename" if os.name == "nt" else "link"
    publish = getattr(os, publish_name)

    def concurrent_publish(staging, destination):
        destination.write_bytes(b"concurrent")
        publish(staging, destination)

    monkeypatch.setattr(os, publish_name, concurrent_publish)
    assert resources.recover_file(tmp_path, source.name)
    assert source.read_bytes() == b"concurrent"


@pytest.mark.skipif(os.name != "nt", reason="Windows rename has no-replace semantics")
def test_windows_recovery_does_not_require_hardlink_support(tmp_path, monkeypatch):
    source = tmp_path / "image.jpg"
    source.write_bytes(b"recoverable")
    resources.retire_file(tmp_path, source.name)

    def no_hardlinks(*_args, **_kwargs):
        raise OSError("Filesystem does not support hard links")

    monkeypatch.setattr(os, "link", no_hardlinks)
    assert resources.recover_file(tmp_path, source.name)
    assert source.read_bytes() == b"recoverable"
    assert not list(tmp_path.rglob("*.solin-stage"))


def test_missing_and_remote_resources_are_not_retired(tmp_path):
    assert not resources.retire_file(tmp_path, "missing.jpg")
    assert not resources.recover_file(tmp_path, "missing.jpg")
    assert not resources.retire_file(tmp_path, "https://example.org/image.jpg")


def test_new_document_does_not_recover_previous_documents_file(tmp_path):
    source = tmp_path / "image.jpg"
    source.write_bytes(b"previous document image")
    resources.retire_file(tmp_path, source.name, document_id="a" * 64)
    assert not resources.recover_file(tmp_path, source.name, document_id="b" * 64)
    assert resources.recover_file(tmp_path, source.name, document_id="a" * 64)


def test_case_unicode_variants_absorb_fallback_without_changing_paths():
    explicit_path = "Media/Dé glória.jpg"
    fallback_path = unicodedata.normalize("NFD", explicit_path).upper()
    entities = {
        "fallback": {RESOURCE: fallback_path, DISCOVERED: True, "title": "fallback"},
        "explicit": {RESOURCE: explicit_path, "title": "organized"},
    }
    visible = reconcile_discoveries(entities)
    assert set(visible) == {"explicit"}
    assert visible["explicit"][RESOURCE] == explicit_path


def test_case_variant_deletion_suppresses_late_claim_but_deliberate_readd_survives():
    deletion_id, deletion = suppression_record("auto", "Image.jpg", automatic=True)
    base = {deletion_id: deletion, "old": {RESOURCE: "IMAGE.JPG"}}
    assert "old" not in reconcile_discoveries(base)
    desired = {"new": {RESOURCE: "image.jpg"}}
    result = record_discovery_edits(base, desired)
    assert deletion_id in result["new"][ACKNOWLEDGED]
    assert "new" in reconcile_discoveries(result)


def test_explicit_edits_update_absorbed_fallback_intentions():
    base = {
        "fallback": {
            RESOURCE: "IMAGE.JPG",
            DISCOVERED: True,
            OVERRIDE_PREFIX + "title": {"present": True, "value": "first edit"},
            OVERRIDE_PREFIX + "caption": {"present": False},
        },
        "explicit": {RESOURCE: "image.jpg", "title": "original", "caption": "old"},
    }
    desired = reconcile_discoveries(base)
    assert desired["explicit"]["title"] == "first edit"
    assert "caption" not in desired["explicit"]
    desired["explicit"]["title"] = "second edit"
    desired["explicit"]["caption"] = "new"
    recorded = record_discovery_edits(base, desired)
    assert recorded["fallback"][OVERRIDE_PREFIX + "title"]["value"] == "second edit"
    assert recorded["fallback"][OVERRIDE_PREFIX + "caption"]["value"] == "new"
    assert reconcile_discoveries(recorded)["explicit"]["title"] == "second edit"
    assert reconcile_discoveries(recorded)["explicit"]["caption"] == "new"


def test_http_resource_identity_preserves_case():
    assert resources.resource_identity_key(
        "https://example.org/Image.jpg"
    ) != resources.resource_identity_key("https://example.org/image.jpg")
