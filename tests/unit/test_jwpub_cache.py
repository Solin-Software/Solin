from __future__ import annotations

import io
import zipfile

from solin.core.meetings.jwpub_cache import JwpubCache, JwpubChecksumStore


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
