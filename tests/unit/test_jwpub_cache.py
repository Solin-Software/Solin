from __future__ import annotations

import hashlib
import io
import os
import stat
import threading
import time
import zipfile
from pathlib import Path

import pytest

from solin.core.meetings import jwpub_cache as cache_module
from solin.core.storage import archive_budget

from solin.core.meetings.jwpub_cache import (
    JwpubCache,
    JwpubChecksumStore,
    needs_jwpub_download,
)


def _write_archive(cache, issue="0", files=None, *, outer_files=None):
    archive = cache.jwpub_path("mwb", "T", issue)
    contents = io.BytesIO()
    with zipfile.ZipFile(contents, "w", compression=zipfile.ZIP_DEFLATED) as inner:
        for name, data in (files or {"publication.db": b"sqlite"}).items():
            inner.writestr(name, data)
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as outer:
        outer.writestr("contents", contents.getvalue())
        for name, data in (outer_files or {}).items():
            outer.writestr(name, data)
    return archive


def test_extract_streams_the_nested_payload_without_reading_whole_members(tmp_path, monkeypatch):
    cache = JwpubCache(tmp_path)
    _write_archive(cache)

    def reject_whole_read(*_args, **_kwargs):
        pytest.fail("Extraction must stream archive members")

    monkeypatch.setattr(zipfile.ZipFile, "read", reject_whole_read)

    extracted = cache.extract("mwb", "T", "0")

    assert extracted is not None
    assert (extracted / "publication.db").read_bytes() == b"sqlite"


@pytest.mark.parametrize(
    "name",
    [
        "../escape.db",
        "..\\escape.db",
        "C:escape.db",
        "/escape.db",
        "NUL.db",
        "image.jpg:stream",
        "folder./image.jpg",
    ],
)
def test_extract_rejects_nonportable_or_escaping_names_and_cleans_staging(tmp_path, name):
    cache = JwpubCache(tmp_path)
    archive = _write_archive(cache, files={"publication.db": b"sqlite", name: b"bad"})

    assert cache.extract("mwb", "T", "0") is None
    assert list(archive.parent.iterdir()) == [archive]


def test_extract_rejects_symlinks_instead_of_materializing_them(tmp_path):
    cache = JwpubCache(tmp_path)
    member = zipfile.ZipInfo("link.db")
    member.create_system = 3
    member.external_attr = (stat.S_IFLNK | 0o777) << 16
    archive = _write_archive(cache, files={"publication.db": b"sqlite", member: b"../outside"})

    assert cache.extract("mwb", "T", "0") is None
    assert list(archive.parent.iterdir()) == [archive]


def test_extract_enforces_combined_temporary_disk_budget_and_preserves_previous(
    tmp_path, monkeypatch
):
    cache = JwpubCache(tmp_path)
    archive = _write_archive(cache)
    extracted = cache.extract("mwb", "T", "0")
    _write_archive(cache, files={"publication.db": b"new", "large.jpg": b"x" * 4096})
    monkeypatch.setattr(archive_budget, "MAX_EXPANDED_BYTES", 2048)

    assert cache.extract("mwb", "T", "0") is None
    assert (extracted / "publication.db").read_bytes() == b"sqlite"
    assert set(archive.parent.iterdir()) == {archive, extracted}


@pytest.mark.parametrize("limit", ["bytes", "members"])
@pytest.mark.parametrize("headroom", [-1, 0])
def test_extract_accounts_for_outer_extras_and_both_layers_at_budget_boundary(
    tmp_path, monkeypatch, limit, headroom
):
    cache = JwpubCache(tmp_path)
    _write_archive(cache)
    previous = cache.extract("mwb", "T", "0")
    files = {"publication.db": b"new", "image.jpg": b"x" * 64}
    archive = _write_archive(cache, files=files, outer_files={"unused.bin": b"y" * 64})
    with zipfile.ZipFile(archive) as outer:
        expanded = sum(member.file_size for member in outer.infolist()) + sum(
            map(len, files.values())
        )
        count = len(outer.infolist()) + len(files)
    if limit == "bytes":
        monkeypatch.setattr(archive_budget, "MAX_EXPANDED_BYTES", expanded + headroom)
    else:
        monkeypatch.setattr(archive_budget, "MAX_ARCHIVE_MEMBERS", count + headroom)

    extracted = cache.extract("mwb", "T", "0")

    if headroom < 0:
        assert extracted is None
        assert (previous / "publication.db").read_bytes() == b"sqlite"
    else:
        assert extracted is not None
        assert (extracted / "publication.db").read_bytes() == b"new"
        assert (extracted / "image.jpg").read_bytes() == files["image.jpg"]
        assert not (extracted / "unused.bin").exists()
    assert set(archive.parent.iterdir()) == {archive, previous}


@pytest.mark.parametrize("layer", ["outer", "inner", "combined"])
def test_extract_rejects_member_count_before_opening_rejected_layer(tmp_path, monkeypatch, layer):
    cache = JwpubCache(tmp_path)
    _write_archive(cache)
    previous = cache.extract("mwb", "T", "0")
    files = {"publication.db": b"new"}
    extras = {}
    if layer == "inner":
        files.update({f"empty-{index}/": b"" for index in range(4)})
    elif layer == "outer":
        extras = {f"empty-{index}/": b"" for index in range(4)}
    archive = _write_archive(cache, files=files, outer_files=extras)
    monkeypatch.setattr(archive_budget, "MAX_ARCHIVE_MEMBERS", 1)
    original_open = zipfile.ZipFile.open
    opened = []

    def observe_open(archive, member, *args, **kwargs):
        opened.append("outer" if "contents" in archive.namelist() else "inner")
        return original_open(archive, member, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "open", observe_open)

    assert cache.extract("mwb", "T", "0") is None
    assert ("outer" if layer == "outer" else "inner") not in opened
    assert (previous / "publication.db").read_bytes() == b"sqlite"
    assert set(archive.parent.iterdir()) == {archive, previous}


def test_operation_cleans_used_extracts_but_preserves_protected_legacy_sources(tmp_path):
    cache = JwpubCache(tmp_path)
    other = JwpubCache(tmp_path)
    archive = _write_archive(cache)
    protected_archive = _write_archive(cache, "legacy")
    protected_extract = cache.extract("mwb", "T", "legacy")
    source = protected_extract / "publication.db"

    with cache.operation(protected_sources=(str(source),)):
        extracted = other.extract("mwb", "T", "0")
        assert other.is_cached("mwb", "T", "legacy")
        assert (extracted / "publication.db").exists()

    assert not extracted.exists()
    assert source.read_bytes() == b"sqlite"
    assert archive.exists() and protected_archive.exists()


def test_prune_never_deletes_publications_leased_by_an_active_operation(tmp_path):
    cache = JwpubCache(tmp_path)
    other = JwpubCache(tmp_path)
    archive = _write_archive(cache)
    unused_archive = _write_archive(cache, "unused")

    with cache.operation():
        extracted = cache.extract("mwb", "T", "0")
        assert other.prune(max_bytes=0) == (("mwb", "T", "unused"),)
        assert archive.exists() and extracted.exists()
        assert not unused_archive.exists()

    assert not extracted.exists()
    assert other.prune(max_bytes=0) == (("mwb", "T", "0"),)


def test_checksum_deletion_does_not_resurrect_from_another_live_store(tmp_path):
    path = tmp_path / "checksums.json"
    first = JwpubChecksumStore(path)
    first.save("mwb", "T", "0", "old")
    second = JwpubChecksumStore(path)

    first.delete("mwb", "T", "0")
    second.save("w", "T", "new", "other")

    reloaded = JwpubChecksumStore(path)
    assert reloaded.get("mwb", "T", "0") == ""
    assert reloaded.get("w", "T", "new") == "other"


def test_extract_restores_previous_directory_when_atomic_install_fails(tmp_path, monkeypatch):
    cache = JwpubCache(tmp_path)
    archive = _write_archive(cache)
    extracted = cache.extract("mwb", "T", "0")
    _write_archive(cache, files={"publication.db": b"replacement"})
    replace = os.replace

    def fail_install(source, destination):
        if Path(destination) == extracted and ".previous-" not in Path(source).name:
            raise PermissionError("destination busy")
        replace(source, destination)

    monkeypatch.setattr(cache_module.os, "replace", fail_install)

    assert cache.extract("mwb", "T", "0") is None
    assert (extracted / "publication.db").read_bytes() == b"sqlite"
    assert set(archive.parent.iterdir()) == {archive, extracted}


def test_operation_preserves_preexisting_extracts_even_when_the_body_fails(tmp_path):
    cache = JwpubCache(tmp_path)
    _write_archive(cache)
    _write_archive(cache, "unrelated")
    used = cache.extract("mwb", "T", "0")
    unrelated = cache.extract("mwb", "T", "unrelated")

    with pytest.raises(RuntimeError, match="materialization failed"), cache.operation():
        assert cache.db_path("mwb", "T", "0") == used / "publication.db"
        raise RuntimeError("materialization failed")

    assert (used / "publication.db").is_file()
    assert (unrelated / "publication.db").is_file()


def test_nested_operations_hold_extracts_until_the_outermost_lease_finishes(tmp_path):
    cache = JwpubCache(tmp_path)
    other = JwpubCache(tmp_path / ".")
    _write_archive(cache)

    with cache.operation():
        with other.operation():
            extracted = other.extract("mwb", "T", "0")
        assert (extracted / "publication.db").exists()
        assert cache.prune(max_bytes=0) == ()

    assert not extracted.exists()


def test_operation_drops_oversized_archive_after_final_lease_only(tmp_path, monkeypatch):
    cache = JwpubCache(tmp_path)
    archive = _write_archive(cache)
    monkeypatch.setattr(cache_module, "_MAX_RETAINED_BYTES", archive.stat().st_size - 1)

    with cache.operation():
        with cache.operation():
            extracted = cache.extract("mwb", "T", "0")
        assert archive.is_file() and extracted.is_dir()

    assert not archive.exists()
    assert not extracted.exists()


def test_prune_removes_expired_archives_then_evicts_lru_with_preferences(tmp_path):
    cache = JwpubCache(tmp_path)
    issues = ("expired", "preferred", "old", "new")
    archives = {issue: _write_archive(cache, issue) for issue in issues}
    for index, archive in enumerate(archives.values()):
        age = 200 if index == 0 else 40 - index * 10
        modified = time.time() - age
        os.utime(archive, (modified, modified))
    budget = archives["preferred"].stat().st_size + archives["new"].stat().st_size

    removed = cache.prune(
        preferred_publications=(("mwb", "T", "preferred"),),
        max_bytes=budget,
        max_age_seconds=100,
    )

    assert removed == (("mwb", "T", "expired"), ("mwb", "T", "old"))
    assert {issue for issue, archive in archives.items() if archive.exists()} == {
        "preferred",
        "new",
    }
    assert (
        sum(archive.stat().st_size for archive in archives.values() if archive.exists()) <= budget
    )


def test_prune_protects_missing_legacy_sources_and_removes_orphan_extracts(tmp_path):
    cache = JwpubCache(tmp_path)
    archive = _write_archive(cache)
    protected = cache.extract("mwb", "T", "0")
    orphan = cache.extract_dir("mwb", "T", "orphan")
    orphan.mkdir()
    (orphan / "publication.db").write_bytes(b"old")
    missing = protected / "images" / "missing.jpg"

    assert cache.prune(protected_sources=(str(missing),), max_bytes=0) == ()
    assert archive.exists() and protected.exists()
    assert not orphan.exists()


def test_using_an_archive_in_an_operation_refreshes_its_eviction_time(tmp_path):
    cache = JwpubCache(tmp_path)
    archive = _write_archive(cache)
    old = time.time() - 200
    os.utime(archive, (old, old))

    with cache.operation():
        assert cache.can_materialize("mwb", "T", "0")

    assert cache.prune(max_age_seconds=100) == ()
    assert archive.exists()


def test_prune_waits_for_an_operation_on_another_cache_instance(tmp_path):
    cache = JwpubCache(tmp_path)
    other = JwpubCache(tmp_path)
    archive = _write_archive(cache)
    started = threading.Event()
    finished = threading.Event()
    removed = []

    def prune():
        started.set()
        try:
            removed.extend(other.prune(max_bytes=0))
        finally:
            finished.set()

    worker = threading.Thread(target=prune)
    try:
        with cache.operation():
            extracted = cache.extract("mwb", "T", "0")
            worker.start()
            assert started.wait(5)
            assert not finished.wait(0.05)
            assert archive.is_file() and extracted.is_dir()
    finally:
        if worker.ident is not None:
            worker.join(5)

    assert not worker.is_alive()
    assert removed == [("mwb", "T", "0")]
    assert not archive.exists() and not extracted.exists()


def test_operation_cleanup_failure_does_not_mask_body_exception(tmp_path, monkeypatch):
    cache = JwpubCache(tmp_path)
    archive = _write_archive(cache)

    def busy(_path):
        raise PermissionError("in use")

    monkeypatch.setattr(cache, "_remove_tree", busy)
    with pytest.raises(RuntimeError, match="body failed"), cache.operation():
        extracted = cache.extract("mwb", "T", "0")
        raise RuntimeError("body failed")

    assert archive.exists() and extracted.exists()


def test_prune_does_not_report_failed_deletions_or_remove_unrelated_files(tmp_path, monkeypatch):
    cache = JwpubCache(tmp_path)
    busy = _write_archive(cache, "busy")
    removable = _write_archive(cache, "removable")
    unrelated = busy.parent / "notes.txt"
    unrelated.write_text("keep", encoding="utf-8")
    unlink = Path.unlink

    def fail_busy(path, *args, **kwargs):
        if path == busy:
            raise PermissionError("in use")
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_busy)

    assert cache.prune(max_bytes=0) == (("mwb", "T", "removable"),)
    assert busy.exists() and not removable.exists()
    assert unrelated.read_text(encoding="utf-8") == "keep"


@pytest.mark.parametrize(
    "files",
    [
        {"publication.db": b"sqlite", "IMAGE.jpg": b"first", "image.jpg": b"second"},
        {"publication.db": b"sqlite", "images": b"file", "images/image.jpg": b"nested"},
        {"publication.db": b"sqlite", ".solin-extraction-stale": b"spoof"},
    ],
)
def test_extract_rejects_path_collisions_and_cache_metadata_spoofing(tmp_path, files):
    cache = JwpubCache(tmp_path)
    archive = _write_archive(cache, files=files)

    assert cache.extract("mwb", "T", "0") is None
    assert list(archive.parent.iterdir()) == [archive]


def test_extract_cleans_temporary_files_after_unexpected_cancellation(tmp_path, monkeypatch):
    cache = JwpubCache(tmp_path)
    archive = _write_archive(cache)

    def interrupted(*_args):
        raise KeyboardInterrupt()

    monkeypatch.setattr(cache_module, "_safe_extract_all", interrupted)
    with pytest.raises(KeyboardInterrupt):
        cache.extract("mwb", "T", "0")

    assert list(archive.parent.iterdir()) == [archive]


def test_extract_handles_corrupt_compression_without_removing_previous_files(tmp_path):
    cache = JwpubCache(tmp_path)
    archive = _write_archive(cache)
    extracted = cache.extract("mwb", "T", "0")
    with zipfile.ZipFile(archive) as outer:
        member = outer.getinfo("contents")
    data = bytearray(archive.read_bytes())
    offset = member.header_offset + 30 + len(member.filename.encode()) + len(member.extra)
    data[offset] = (data[offset] & 0xF8) | 0x07  # Invalid DEFLATE block type.
    archive.write_bytes(data)

    assert cache.extract("mwb", "T", "0") is None
    assert (extracted / "publication.db").read_bytes() == b"sqlite"
    assert set(archive.parent.iterdir()) == {archive, extracted}


def test_protected_extract_cannot_be_invalidated_or_replaced_during_operation(tmp_path):
    cache = JwpubCache(tmp_path)
    archive = _write_archive(cache)
    extracted = cache.extract("mwb", "T", "0")
    source = extracted / "publication.db"
    _write_archive(cache, files={"publication.db": b"new"})

    with cache.operation(protected_sources=(str(source),)):
        cache.invalidate_extract("mwb", "T", "0")
        assert source.read_bytes() == b"sqlite"
        scratch = cache.extract("mwb", "T", "0")
        assert scratch != extracted
        assert cache.extract_dir("mwb", "T", "0") == scratch
        assert cache.db_path("mwb", "T", "0") == scratch / "publication.db"
        assert (scratch / "publication.db").read_bytes() == b"new"
        assert source.read_bytes() == b"sqlite"

    assert archive.exists() and source.exists()
    assert not scratch.exists()


def test_unprotected_operation_uses_new_scratch_without_replacing_existing_legacy_files(tmp_path):
    cache = JwpubCache(tmp_path)
    _write_archive(cache)
    legacy = cache.extract("mwb", "T", "0")
    _write_archive(cache, files={"publication.db": b"new"})

    with cache.operation():
        scratch = cache.extract("mwb", "T", "0")
        assert scratch != legacy
        assert (scratch / "publication.db").read_bytes() == b"new"
        assert (legacy / "publication.db").read_bytes() == b"sqlite"

    assert not scratch.exists()
    assert (legacy / "publication.db").read_bytes() == b"sqlite"


@pytest.mark.parametrize("scope", ["root", "publication", "extract"])
def test_protected_directories_preserve_all_descendants_during_operation_and_prune(tmp_path, scope):
    cache = JwpubCache(tmp_path)
    archive = _write_archive(cache)
    legacy = cache.extract("mwb", "T", "0")
    protected = {"root": tmp_path, "publication": archive.parent, "extract": legacy}[scope]

    with cache.operation(protected_sources=(str(protected),)):
        assert cache.is_cached("mwb", "T", "0")
        assert cache.prune(max_bytes=0, max_age_seconds=0) == ()

    assert cache.prune(protected_sources=(str(protected),), max_bytes=0, max_age_seconds=0) == ()
    assert archive.exists() and legacy.exists()


def test_explicit_prune_reclaims_owned_scratch_after_a_migration_guard_is_lifted(tmp_path):
    cache = JwpubCache(tmp_path)
    archive = _write_archive(cache)

    with cache.operation(protected_sources=(str(tmp_path),)):
        scratch = cache.extract("mwb", "T", "0")

    assert scratch.exists()
    assert cache.prune(protected_sources=(str(tmp_path),), max_bytes=0) == ()
    assert scratch.exists()
    assert cache.prune(max_bytes=0) == (("mwb", "T", "0"),)
    assert not scratch.exists() and not archive.exists()


def test_failed_migration_guard_preserves_oversized_archive_for_legacy_repair(
    tmp_path, monkeypatch
):
    cache = JwpubCache(tmp_path)
    archive = _write_archive(cache)
    legacy = cache.extract("mwb", "T", "0")
    monkeypatch.setattr(cache_module, "_MAX_RETAINED_BYTES", archive.stat().st_size - 1)

    with cache.operation(protected_sources=(str(tmp_path),)):
        assert cache.is_cached("mwb", "T", "0")

    assert archive.exists() and legacy.exists()


def test_jwpub_cache_paths_are_scoped_by_publication_language_and_issue(tmp_path):
    cache = JwpubCache(tmp_path)

    assert cache.jwpub_path("mwb", "T", "20260600") == (tmp_path / "mwb_T" / "mwb_T_20260600.jwpub")
    assert cache.extract_dir("mwb", "T", "20260600") == (tmp_path / "mwb_T" / "x_20260600")


def test_jwpub_cache_extracts_inner_archive_and_discovers_database(tmp_path):
    cache = JwpubCache(tmp_path)
    jwpub_path = cache.jwpub_path("mwb", "T", "20260600")

    inner_bytes = io.BytesIO()
    with zipfile.ZipFile(inner_bytes, "w") as inner:
        inner.writestr("sample.db", b"sqlite bytes")
    with zipfile.ZipFile(jwpub_path, "w") as outer:
        outer.writestr("contents", inner_bytes.getvalue())

    extract_dir = cache.extract("mwb", "T", "20260600")

    assert extract_dir == tmp_path / "mwb_T" / "x_20260600"
    assert extract_dir is not None
    assert cache.is_cached("mwb", "T", "20260600") is True
    assert cache.db_path("mwb", "T", "20260600") == extract_dir / "sample.db"


def test_jwpub_cache_rejects_archive_members_outside_extract_dir(tmp_path):
    cache = JwpubCache(tmp_path)
    jwpub_path = cache.jwpub_path("mwb", "T", "20260600")

    inner_bytes = io.BytesIO()
    with zipfile.ZipFile(inner_bytes, "w") as inner:
        inner.writestr("../escape.db", b"nope")
    with zipfile.ZipFile(jwpub_path, "w") as outer:
        outer.writestr("contents", inner_bytes.getvalue())

    assert cache.extract("mwb", "T", "20260600") is None
    assert not (tmp_path / "mwb_T" / "escape.db").exists()
    assert not (tmp_path / "escape.db").exists()


def test_jwpub_cache_keeps_the_last_complete_extract_when_replacement_fails(tmp_path):
    cache = JwpubCache(tmp_path)
    jwpub_path = cache.jwpub_path("mwb", "T", "20260600")

    inner_bytes = io.BytesIO()
    with zipfile.ZipFile(inner_bytes, "w") as inner:
        inner.writestr("sample.db", b"first")
    with zipfile.ZipFile(jwpub_path, "w") as outer:
        outer.writestr("contents", inner_bytes.getvalue())
    extract_dir = cache.extract("mwb", "T", "20260600")

    with zipfile.ZipFile(jwpub_path, "w") as outer:
        outer.writestr("unexpected", b"broken")

    assert cache.extract("mwb", "T", "20260600") is None
    assert extract_dir is not None
    assert (extract_dir / "sample.db").read_bytes() == b"first"
    assert cache.is_cached("mwb", "T", "20260600") is True


def test_jwpub_cache_repairs_only_missing_files_without_removing_live_files(tmp_path):
    cache = JwpubCache(tmp_path)
    jwpub_path = cache.jwpub_path("wcg", "T", "0")
    inner_bytes = io.BytesIO()
    with zipfile.ZipFile(inner_bytes, "w") as inner:
        inner.writestr("publication.db", b"sqlite")
        inner.writestr("images/missing.jpg", b"restored")
        inner.writestr("images/healthy.jpg", b"archive version")
    with zipfile.ZipFile(jwpub_path, "w") as outer:
        outer.writestr("contents", inner_bytes.getvalue())
    extract_dir = cache.extract_dir("wcg", "T", "0")
    extract_dir.mkdir(parents=True)
    (extract_dir / "publication.db").write_bytes(b"sqlite")
    healthy = extract_dir / "images" / "healthy.jpg"
    healthy.parent.mkdir(parents=True)
    healthy.write_bytes(b"currently presented")
    missing = extract_dir / "images" / "missing.jpg"

    assert cache.repair_extract_for_source(missing) is True

    assert missing.read_bytes() == b"restored"
    assert healthy.read_bytes() == b"currently presented"
    assert cache.is_cached("wcg", "T", "0") is True
    assert not any(path.name.startswith(".x_0.") for path in jwpub_path.parent.iterdir())


def test_stale_extract_stays_on_disk_until_replacement_is_ready(tmp_path):
    cache = JwpubCache(tmp_path)
    extract_dir = cache.extract_dir("mwb", "T", "20260600")
    extract_dir.mkdir(parents=True)
    presented = extract_dir / "publication.db"
    presented.write_bytes(b"currently presented")

    cache.mark_extract_stale("mwb", "T", "20260600")

    assert presented.read_bytes() == b"currently presented"
    assert cache.is_cached("mwb", "T", "20260600") is False


def test_jwpub_checksum_store_persists_and_compares_values(tmp_path):
    path = tmp_path / "checksums.json"
    store = JwpubChecksumStore(path)

    assert store.get("mwb", "T", "20260600") == ""
    assert store.has_changed("mwb", "T", "20260600", "") is False
    assert store.has_changed("mwb", "T", "20260600", "abc") is True

    store.save("mwb", "T", "20260600", "abc")
    reloaded = JwpubChecksumStore(path)

    assert reloaded.get("mwb", "T", "20260600") == "abc"
    assert reloaded.has_changed("mwb", "T", "20260600", "abc") is False
    assert reloaded.has_changed("mwb", "T", "20260600", "def") is True


def test_jwpub_download_gate_accepts_valid_local_archive(tmp_path):
    cache = JwpubCache(tmp_path / "jwpub")
    store = JwpubChecksumStore(tmp_path / "checksums.json")
    archive = cache.jwpub_path("w", "T", "20260400")
    archive.write_bytes(b"valid archive")
    cache.extract_dir("w", "T", "20260400").mkdir(parents=True)
    (cache.extract_dir("w", "T", "20260400") / "pub.db").write_bytes(b"sqlite")
    checksum = hashlib.md5(archive.read_bytes()).hexdigest()

    assert needs_jwpub_download(cache, store, "w", "T", "20260400", checksum) is False
    assert store.get("w", "T", "20260400") == checksum


def test_jwpub_download_gate_redownloads_when_remote_checksum_differs(tmp_path):
    cache = JwpubCache(tmp_path / "jwpub")
    store = JwpubChecksumStore(tmp_path / "checksums.json")
    store.save("w", "T", "20260400", "old")
    archive = cache.jwpub_path("w", "T", "20260400")
    archive.write_bytes(b"old archive")
    cache.extract_dir("w", "T", "20260400").mkdir(parents=True)
    (cache.extract_dir("w", "T", "20260400") / "pub.db").write_bytes(b"sqlite")

    assert needs_jwpub_download(cache, store, "w", "T", "20260400", "remote-new") is True


def test_jwpub_download_gate_keeps_legacy_extracted_cache(tmp_path):
    cache = JwpubCache(tmp_path / "jwpub")
    store = JwpubChecksumStore(tmp_path / "checksums.json")
    cache.extract_dir("mwb", "T", "20260500").mkdir(parents=True)
    (cache.extract_dir("mwb", "T", "20260500") / "pub.db").write_bytes(b"sqlite")

    assert needs_jwpub_download(cache, store, "mwb", "T", "20260500", "remote") is False
    assert store.get("mwb", "T", "20260500") == "remote"
