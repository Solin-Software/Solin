from __future__ import annotations

import hashlib
import io
import zipfile

from solin.core.meetings.jwpub_cache import (
    JwpubCache,
    JwpubChecksumStore,
    needs_jwpub_download,
)


def test_jwpub_cache_paths_are_scoped_by_publication_language_and_issue(tmp_path):
    cache = JwpubCache(tmp_path)

    assert cache.jwpub_path("mwb", "T", "20260600") == (
        tmp_path / "mwb_T" / "mwb_T_20260600.jwpub"
    )
    assert cache.extract_dir("mwb", "T", "20260600") == (
        tmp_path / "mwb_T" / "x_20260600"
    )


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

    assert (
        needs_jwpub_download(cache, store, "w", "T", "20260400", "remote-new")
        is True
    )


def test_jwpub_download_gate_keeps_legacy_extracted_cache(tmp_path):
    cache = JwpubCache(tmp_path / "jwpub")
    store = JwpubChecksumStore(tmp_path / "checksums.json")
    cache.extract_dir("mwb", "T", "20260500").mkdir(parents=True)
    (cache.extract_dir("mwb", "T", "20260500") / "pub.db").write_bytes(b"sqlite")

    assert (
        needs_jwpub_download(cache, store, "mwb", "T", "20260500", "remote")
        is False
    )
    assert store.get("mwb", "T", "20260500") == "remote"
