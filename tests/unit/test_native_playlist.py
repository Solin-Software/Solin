from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
from types import SimpleNamespace
import uuid
import zipfile

import pytest

from solin.core.media.download_storage import cached_path_for
from solin.core.playlists.native import (
    NativePlaylistExportRequest,
    NativePlaylistImportRequest,
    NativePlaylistMissingMediaError,
    NativePlaylistTransferCancelled,
    NativePlaylistValidationError,
    SOLIN_PLAYLIST_MIME,
    export_native_playlist,
    import_native_playlist,
    rollback_native_playlist_import,
)
from solin.core.playlists.storage import PlaylistRepository


def _playlist(*items: dict) -> dict:
    return {
        "id": "playlist-old",
        "name": "Complete playlist",
        "color_hue": 142,
        "items": list(items),
        "sections": [
            {
                "id": "section-old",
                "title": "Section",
                "collapsed": True,
                "color_hue": 215,
            },
            {
                "id": "subsection-old",
                "title": "Subsection",
                "parent_id": "section-old",
                "collapsed": False,
                "color_hue": 145,
            },
        ],
        "markers": [
            {
                "id": "marker-old",
                "title": "Marker",
                "subsection_id": "subsection-old",
                "position": 1,
            }
        ],
    }


def _item(identifier: str, url: str, **extra: object) -> dict:
    item = {
        "id": identifier,
        "title": identifier,
        "url": url,
        "type": "video",
        "auto_title": False,
        "key_symbol": None,
        "track": None,
        "issue_tag": None,
        "doc_id": None,
        "meps_language": 0,
    }
    item.update(extra)
    return item


def _cache_remote(cache_dir: Path, url: str, data: bytes) -> Path:
    path = Path(cached_path_for(url, cache_dir))
    path.write_bytes(data)
    Path(f"{path}.done").write_text(url, encoding="utf-8")
    return path


def _export(tmp_path: Path, playlist: dict) -> Path:
    output = tmp_path / "playlist.solinplaylist"
    export_native_playlist(
        NativePlaylistExportRequest(
            playlist=playlist,
            output_path=output,
            media_cache_dir=tmp_path / "cache",
            thumbnail_cache_dir=tmp_path / "thumbs",
            generator_version="1.2.3",
        )
    )
    return output


def _archive_payload(path: Path) -> tuple[dict, dict[str, bytes]]:
    with zipfile.ZipFile(path) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        payloads = {info.filename: archive.read(info) for info in archive.infolist()}
    return manifest, payloads


def _rewrite_archive(
    path: Path,
    manifest: dict,
    payloads: dict[str, bytes],
    *,
    compressed_asset: str = "",
    extra: tuple[str, bytes, int] | None = None,
) -> None:
    with zipfile.ZipFile(path, "w", allowZip64=True) as archive:
        archive.writestr("mimetype", SOLIN_PLAYLIST_MIME, compress_type=zipfile.ZIP_STORED)
        for asset in manifest["assets"]:
            asset_path = asset["path"]
            compression = (
                zipfile.ZIP_DEFLATED
                if asset_path == compressed_asset
                else zipfile.ZIP_STORED
            )
            archive.writestr(asset_path, payloads[asset_path], compress_type=compression)
        if extra is not None:
            name, data, attributes = extra
            info = zipfile.ZipInfo(name)
            info.external_attr = attributes
            archive.writestr(info, data, compress_type=zipfile.ZIP_STORED)
        archive.writestr(
            "manifest.json",
            json.dumps(manifest, separators=(",", ":")),
            compress_type=zipfile.ZIP_STORED,
        )


def test_native_playlist_round_trip_preserves_state_sources_and_assets(tmp_path):
    local = tmp_path / "local.mp4"
    local.write_bytes(b"local-media")
    thumbs = tmp_path / "thumbs"
    thumbs.mkdir()
    (thumbs / "local.jpg").write_bytes(b"jpeg-thumbnail")
    cached_url = "https://example.test/downloaded.mp4"
    _cache_remote(tmp_path / "cache", cached_url, b"cached-media")
    jw_url = "https://download.jw.org/files/media_pub/pub-sjjm_T_1_r720P.mp4"
    direct_url = "https://example.test/stream.mp4"
    playlist = _playlist(
        _item(
            "local",
            os.fspath(local),
            section_id="subsection-old",
            start_trim_ticks=10,
            end_trim_ticks=20,
            image_framing={"scale": 1.5, "offset_x": 0.2},
            _tmp=True,
        ),
        _item("cached", cached_url, source_url=cached_url),
        _item("jw", jw_url, key_symbol="sjjm", track=1, meps_language=5),
        _item("direct", direct_url),
    )

    output = _export(tmp_path, playlist)

    with zipfile.ZipFile(output) as archive:
        infos = archive.infolist()
        assert infos[0].filename == "mimetype"
        assert infos[0].compress_type == zipfile.ZIP_STORED
        assert archive.read(infos[0]).decode("ascii") == SOLIN_PLAYLIST_MIME
        assert all(
            info.compress_type == zipfile.ZIP_STORED
            for info in infos
            if info.filename.startswith("assets/")
        )
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["schema_version"] == 1
        assert manifest["generator"]["version"] == "1.2.3"
        assert "_tmp" not in manifest["playlist"]["items"][0]
        assert all(item["url"] == "" for item in manifest["playlist"]["items"])
        assert {source["kind"] for source in manifest["sources"]} == {
            "embedded",
            "jw",
            "direct",
        }
        for asset in manifest["assets"]:
            data = archive.read(asset["path"])
            assert len(data) == asset["size"]
            assert hashlib.sha256(data).hexdigest() == asset["sha256"]
            uuid.UUID(asset["id"])

    progress = []
    result = import_native_playlist(
        NativePlaylistImportRequest(
            input_path=output,
            embedded_media_dir=tmp_path / "profile" / "embedded",
            thumbnail_cache_dir=tmp_path / "profile-cache" / "thumbs",
            resolve_jw_source=lambda source: "https://resolved.test/jw.mp4",
            progress=progress.append,
        )
    )

    imported = result.playlist
    assert imported["id"] != playlist["id"]
    assert imported["name"] == playlist["name"]
    assert imported["color_hue"] == 142
    assert [section["color_hue"] for section in imported["sections"]] == [215, 145]
    assert {section["id"] for section in imported["sections"]}.isdisjoint(
        {"section-old", "subsection-old"}
    )
    imported_section_ids = {section["id"] for section in imported["sections"]}
    subsection = next(section for section in imported["sections"] if section.get("parent_id"))
    assert subsection["parent_id"] in imported_section_ids
    assert imported["markers"][0]["subsection_id"] == subsection["id"]

    items = {item["title"]: item for item in imported["items"]}
    assert Path(items["local"]["url"]).read_bytes() == b"local-media"
    assert items["local"]["start_trim_ticks"] == 10
    assert items["local"]["image_framing"]["scale"] == 1.5
    assert Path(items["cached"]["url"]).read_bytes() == b"cached-media"
    assert items["cached"]["source_url"] == cached_url
    assert items["jw"]["url"] == "https://resolved.test/jw.mp4"
    assert items["direct"]["url"] == direct_url
    assert (
        tmp_path / "profile-cache" / "thumbs" / f"{items['local']['id']}.jpg"
    ).read_bytes() == b"jpeg-thumbnail"
    assert set(result.created_files) == {path for path in result.created_files}
    assert progress[-1].can_cancel is False


def test_export_missing_local_media_fails_without_replacing_destination(tmp_path):
    output = tmp_path / "playlist.solinplaylist"
    output.write_bytes(b"existing")

    with pytest.raises(NativePlaylistMissingMediaError) as caught:
        export_native_playlist(
            NativePlaylistExportRequest(
                playlist=_playlist(_item("missing", os.fspath(tmp_path / "missing.mp4"))),
                output_path=output,
                media_cache_dir=tmp_path / "cache",
                thumbnail_cache_dir=tmp_path / "thumbs",
            )
        )

    assert "missing.mp4" in str(caught.value)
    assert output.read_bytes() == b"existing"
    assert list(tmp_path.glob("*.part")) == []


def test_export_cancellation_is_atomic(tmp_path):
    source = tmp_path / "large.mp4"
    source.write_bytes(b"x" * (2 * 1024 * 1024))
    output = tmp_path / "playlist.solinplaylist"
    output.write_bytes(b"existing")
    cancellation = SimpleNamespace(is_set=lambda: False)

    def progress(value):
        if value.bytes_completed:
            cancellation.is_set = lambda: True

    with pytest.raises(NativePlaylistTransferCancelled):
        export_native_playlist(
            NativePlaylistExportRequest(
                playlist=_playlist(_item("item", os.fspath(source))),
                output_path=output,
                media_cache_dir=tmp_path / "cache",
                thumbnail_cache_dir=tmp_path / "thumbs",
                progress=progress,
                cancellation=cancellation,
            )
        )

    assert output.read_bytes() == b"existing"
    assert not list(tmp_path.glob(".*.part"))


def test_export_never_overwrites_a_media_source(tmp_path):
    source = tmp_path / "same.solinplaylist"
    source.write_bytes(b"irreplaceable-media")

    with pytest.raises(ValueError, match="cannot overwrite playlist media"):
        export_native_playlist(
            NativePlaylistExportRequest(
                playlist=_playlist(_item("item", os.fspath(source), type="video")),
                output_path=source,
                media_cache_dir=tmp_path / "cache",
                thumbnail_cache_dir=tmp_path / "thumbs",
            )
        )

    assert source.read_bytes() == b"irreplaceable-media"


def test_export_rejects_unsafe_item_identifier_before_thumbnail_lookup(tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"content")

    with pytest.raises(NativePlaylistValidationError, match="item id"):
        _export(tmp_path, _playlist(_item("../outside", os.fspath(source))))


@pytest.mark.parametrize("invalid_kind", ["compressed", "hash", "undeclared", "traversal", "symlink"])
def test_import_rejects_unsafe_or_corrupt_assets_without_profile_files(
    tmp_path,
    invalid_kind,
):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"content")
    archive_path = _export(tmp_path, _playlist(_item("item", os.fspath(source))))
    manifest, payloads = _archive_payload(archive_path)
    asset_path = manifest["assets"][0]["path"]
    compressed = ""
    extra = None
    if invalid_kind == "compressed":
        compressed = asset_path
    elif invalid_kind == "hash":
        manifest["assets"][0]["sha256"] = "0" * 64
    elif invalid_kind == "undeclared":
        extra = ("assets/extra.bin", b"extra", 0)
    elif invalid_kind == "traversal":
        extra = ("../escape.bin", b"extra", 0)
    elif invalid_kind == "symlink":
        extra = ("assets/link", b"target", (stat.S_IFLNK | 0o777) << 16)
    _rewrite_archive(
        archive_path,
        manifest,
        payloads,
        compressed_asset=compressed,
        extra=extra,
    )

    embedded = tmp_path / "profile" / "embedded"
    thumbs = tmp_path / "profile" / "thumbs"
    with pytest.raises(NativePlaylistValidationError):
        import_native_playlist(
            NativePlaylistImportRequest(
                input_path=archive_path,
                embedded_media_dir=embedded,
                thumbnail_cache_dir=thumbs,
            )
        )

    assert not [path for path in embedded.rglob("*") if path.is_file()]
    assert not [path for path in thumbs.rglob("*") if path.is_file()]


def test_import_rejects_future_schema_and_duplicate_casefolded_names(tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"content")
    archive_path = _export(tmp_path, _playlist(_item("item", os.fspath(source))))
    manifest, payloads = _archive_payload(archive_path)
    manifest["schema_version"] = 2
    _rewrite_archive(archive_path, manifest, payloads)

    request = NativePlaylistImportRequest(
        input_path=archive_path,
        embedded_media_dir=tmp_path / "embedded",
        thumbnail_cache_dir=tmp_path / "thumbs",
    )
    with pytest.raises(NativePlaylistValidationError, match="Unsupported"):
        import_native_playlist(request)

    manifest["schema_version"] = 1
    _rewrite_archive(archive_path, manifest, payloads)
    with zipfile.ZipFile(archive_path, "a") as archive:
        archive.writestr("MIMETYPE", SOLIN_PLAYLIST_MIME, compress_type=zipfile.ZIP_STORED)
    with pytest.raises(NativePlaylistValidationError, match="Duplicate"):
        import_native_playlist(request)


def test_import_result_can_be_rolled_back_after_strict_commit_failure(tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"content")
    archive_path = _export(tmp_path, _playlist(_item("item", os.fspath(source))))
    result = import_native_playlist(
        NativePlaylistImportRequest(
            input_path=archive_path,
            embedded_media_dir=tmp_path / "embedded",
            thumbnail_cache_dir=tmp_path / "thumbs",
        )
    )
    assert all(path.is_file() for path in result.created_files)

    rollback_native_playlist_import(result)

    assert all(not path.exists() for path in result.created_files)


def test_native_transfer_never_buffers_media_with_path_or_zipfile_convenience_reads(
    monkeypatch,
    tmp_path,
):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"content")
    monkeypatch.setattr(
        Path,
        "read_bytes",
        lambda _self: (_ for _ in ()).throw(AssertionError("Path.read_bytes used")),
    )
    monkeypatch.setattr(
        zipfile.ZipFile,
        "read",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("ZipFile.read used")
        ),
    )

    archive_path = _export(tmp_path, _playlist(_item("item", os.fspath(source))))
    result = import_native_playlist(
        NativePlaylistImportRequest(
            input_path=archive_path,
            embedded_media_dir=tmp_path / "embedded",
            thumbnail_cache_dir=tmp_path / "thumbs",
        )
    )

    with Path(result.playlist["items"][0]["url"]).open("rb") as handle:
        assert handle.read() == b"content"


def test_playlist_repository_save_strict_propagates_commit_failure(monkeypatch, tmp_path):
    repository = PlaylistRepository(tmp_path / "playlists.json")
    notifications = []
    repository.subscribe(lambda: notifications.append("changed"))
    monkeypatch.setattr(
        repository._json,
        "write",
        lambda _value: (_ for _ in ()).throw(OSError("disk full")),
    )

    with pytest.raises(OSError, match="disk full"):
        repository.save_strict([{"id": "playlist", "name": "Name", "items": []}])

    assert notifications == []
