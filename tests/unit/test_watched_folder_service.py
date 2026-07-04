from __future__ import annotations

import tempfile
import unittest
import os
import uuid
from pathlib import Path

import pytest

from solin.core.ingest import watched_folder as watched_folder_module
from solin.core.ingest.manifest import MANIFEST_REPOSITORY
from solin.core.ingest.local_files import local_file_availability_signature
from solin.core.ingest.meeting_folder_sources import (
    meeting_folder_source_needs_processing,
    scan_meeting_folder_sources,
)
from solin.core.ingest.watched_folder import WatchedFolderSyncThread


def _write_manifest(folder: Path, payload: dict) -> None:
    def replace_manifest(manifest: dict) -> None:
        manifest.clear()
        manifest.update(payload)

    MANIFEST_REPOSITORY.update(folder, replace_manifest, strict=False)


def _read_manifest(folder: Path) -> dict:
    return MANIFEST_REPOSITORY.load(folder, strict=True)


class LocalFileAvailabilitySignatureTests(unittest.TestCase):
    def test_local_file_deletion_changes_signature_without_remote_urls(self):
        with tempfile.TemporaryDirectory() as tmp:
            media = Path(tmp) / "talk.mp4"
            media.write_bytes(b"video")
            remote = "https://cdn.example/video.mp4"
            key = os.path.normcase(os.path.normpath(os.path.abspath(str(media))))

            present = local_file_availability_signature([str(media), remote])
            self.assertEqual(present, ((key, True),))

            media.unlink()

            missing = local_file_availability_signature([str(media), remote])
            self.assertEqual(missing, ((key, False),))
            self.assertNotEqual(present, missing)


class MeetingFolderSourceScannerTests(unittest.TestCase):
    def test_scans_direct_supported_sources_without_manifest_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            meeting = root / "2026-05-26 MW"
            meeting.mkdir()
            regular = root / "regular playlist"
            regular.mkdir()

            for name in [
                "talk.mp4",
                "slides.pdf",
                "playlist.jwlplaylist",
                "publication.jwpub",
                "deck.pptx",
                "notes.docx",
                ".hidden.mp4",
                "_solin_manifest.json",
                "ignored.txt",
            ]:
                (meeting / name).write_bytes(b"data")
            nested = meeting / "nested"
            nested.mkdir()
            (nested / "nested.mp4").write_bytes(b"data")
            (regular / "talk.mp4").write_bytes(b"data")

            folders = scan_meeting_folder_sources(str(root))

            self.assertEqual(len(folders), 1)
            folder = folders[0]
            self.assertEqual(folder["monday"], "2026-05-25")
            self.assertEqual(folder["meeting_tag"], "MW")
            self.assertEqual(
                {source["name"] for source in folder["sources"]},
                {
                    "talk.mp4",
                    "slides.pdf",
                    "playlist.jwlplaylist",
                    "publication.jwpub",
                    "deck.pptx",
                    "notes.docx",
                },
            )
        self.assertFalse((meeting / ".solin_cache").exists())

    def test_playlist_scan_excludes_meeting_folder_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Music").mkdir()
            (root / "2026-05-26 MW").mkdir()
            (root / "2026-05-31 WE").mkdir()

            playlists = watched_folder_module.scan_root(str(root))

            self.assertEqual([playlist["name"] for playlist in playlists], ["Music"])

    def test_source_processing_decision_suppresses_removed_until_file_changes(self):
        source = {
            "source_key": "file",
            "signature": {"size": 10, "mtime_ns": 123},
        }
        processed_without_nodes = {
            "status": "processed",
            "signature": {"size": 10, "mtime_ns": 123},
            "node_ids": [],
        }
        failed_same_file = {
            "status": "failed",
            "signature": {"size": 10, "mtime_ns": 123},
            "node_ids": [],
        }
        changed_file = {
            "status": "processed",
            "signature": {"size": 11, "mtime_ns": 123},
            "node_ids": ["old"],
        }

        self.assertTrue(meeting_folder_source_needs_processing(source, None))
        self.assertFalse(
            meeting_folder_source_needs_processing(source, processed_without_nodes)
        )
        self.assertFalse(meeting_folder_source_needs_processing(source, failed_same_file))
        self.assertTrue(meeting_folder_source_needs_processing(source, changed_file))


def test_watched_folder_cancellation_terminates_libreoffice(monkeypatch, tmp_path):
    process = type(
        "_Process",
        (),
        {
            "returncode": None,
            "terminated": False,
            "poll": lambda self: self.returncode,
            "terminate": lambda self: (
                setattr(self, "terminated", True),
                setattr(self, "returncode", -15),
            ),
            "wait": lambda self, timeout=None: self.returncode,
            "kill": lambda self: setattr(self, "returncode", -9),
            "communicate": lambda self: ("", ""),
        },
    )()
    monkeypatch.setattr(
        watched_folder_module.subprocess,
        "Popen",
        lambda *args, **kwargs: process,
    )
    monkeypatch.setattr(
        WatchedFolderSyncThread,
        "isInterruptionRequested",
        lambda self: True,
    )
    thread = WatchedFolderSyncThread(
        str(tmp_path),
        media_lang="E",
        fallback_lang_code="E",
    )

    with pytest.raises(InterruptedError):
        thread._run_libreoffice(["soffice"])

    assert process.terminated is True


def _install_fake_pdf_renderer(monkeypatch):
    from solin.core.rendering import pdf as pdf_module

    def fake_render(
        _pdf_path,
        output_dir,
        *,
        page_name_format,
        page_stem,
        progress_cb,
        **_kwargs,
    ):
        paths = []
        for page_number in range(1, 3):
            if progress_cb is not None:
                progress_cb(page_number, 2)
            path = Path(output_dir) / page_name_format.format(
                stem=page_stem,
                n=page_number,
            )
            path.write_bytes(f"page-{page_number}".encode())
            paths.append(str(path))
        return paths

    monkeypatch.setattr(pdf_module, "render_pdf_pages_sync", fake_render)


def _sync_thread(tmp_path):
    return WatchedFolderSyncThread(
        str(tmp_path),
        media_lang="E",
        fallback_lang_code="E",
    )


def test_cancelled_pdf_render_does_not_publish_partial_cache(monkeypatch, tmp_path):
    _install_fake_pdf_renderer(monkeypatch)
    source = tmp_path / "slides.pdf"
    source.write_bytes(b"pdf")
    cache = tmp_path / ".solin_cache"
    checks = 0

    def interruption_requested(_self):
        nonlocal checks
        checks += 1
        return checks >= 2

    monkeypatch.setattr(
        WatchedFolderSyncThread,
        "isInterruptionRequested",
        interruption_requested,
    )
    thread = _sync_thread(tmp_path)

    with pytest.raises(InterruptedError):
        thread._process_pdf(source, cache)

    assert list(cache.glob("slides-page_*.jpg")) == []
    assert watched_folder_module.pages_already_exist(source, cache) == []


def test_cancelled_libreoffice_render_does_not_publish_partial_cache(
    monkeypatch,
    tmp_path,
):
    from solin.core.rendering import libreoffice as libreoffice_module

    _install_fake_pdf_renderer(monkeypatch)
    source = tmp_path / "deck.pptx"
    source.write_bytes(b"presentation")
    cache = tmp_path / ".solin_cache"
    checks = 0

    def interruption_requested(_self):
        nonlocal checks
        checks += 1
        return checks >= 2

    def fake_libreoffice(args):
        output_dir = Path(args[args.index("--outdir") + 1])
        (output_dir / "deck.pdf").write_bytes(b"pdf")
        return watched_folder_module.subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(libreoffice_module, "libreoffice_path", lambda: "soffice")
    monkeypatch.setattr(
        WatchedFolderSyncThread,
        "isInterruptionRequested",
        interruption_requested,
    )
    thread = _sync_thread(tmp_path)
    monkeypatch.setattr(thread, "_run_libreoffice", fake_libreoffice)

    with pytest.raises(InterruptedError):
        thread._process_lo(source, cache)

    assert list(cache.glob("deck-page_*.jpg")) == []
    assert watched_folder_module.pages_already_exist(source, cache) == []


def test_completed_page_cache_is_reused_only_while_source_matches(
    monkeypatch,
    tmp_path,
):
    _install_fake_pdf_renderer(monkeypatch)
    source = tmp_path / "slides.pdf"
    source.write_bytes(b"pdf")
    cache = tmp_path / ".solin_cache"
    monkeypatch.setattr(
        WatchedFolderSyncThread,
        "isInterruptionRequested",
        lambda _self: False,
    )
    thread = _sync_thread(tmp_path)

    paths, virtual_items = thread._process_pdf(source, cache)

    assert virtual_items == []
    assert watched_folder_module.pages_already_exist(source, cache) == paths

    source.write_bytes(b"changed-pdf")

    assert watched_folder_module.pages_already_exist(source, cache) == []


def test_same_stem_documents_use_independent_page_caches(monkeypatch, tmp_path):
    _install_fake_pdf_renderer(monkeypatch)
    pdf_source = tmp_path / "report.pdf"
    deck_source = tmp_path / "report.pptx"
    pdf_source.write_bytes(b"pdf")
    deck_source.write_bytes(b"presentation")
    cache = tmp_path / ".solin_cache"
    monkeypatch.setattr(
        WatchedFolderSyncThread,
        "isInterruptionRequested",
        lambda _self: False,
    )
    thread = _sync_thread(tmp_path)

    pdf_paths, _ = thread._process_pdf(pdf_source, cache)
    deck_paths, _ = thread._process_pdf(deck_source, cache)

    assert set(pdf_paths).isdisjoint(deck_paths)
    assert watched_folder_module.pages_already_exist(pdf_source, cache) == pdf_paths
    assert watched_folder_module.pages_already_exist(deck_source, cache) == deck_paths


def test_document_cache_rename_updates_saved_playlist_urls(tmp_path):
    _write_manifest(
        tmp_path,
        {
            "version": 1,
            "processed": {
                "report.pdf": {
                    "type": "pdf",
                    "outputs": ["report-page_001.jpg"],
                },
            },
            "playlist": {
                "items": [
                    {"url": ".solin_cache/report-page_001.jpg"},
                ],
            },
        },
    )

    watched_folder_module._commit_processed_entry(
        tmp_path,
        "report.pdf",
        {
            "type": "pdf",
            "outputs": ["report.pdf-unique-page_001.jpg"],
        },
    )

    manifest = _read_manifest(tmp_path)
    assert manifest["playlist"]["items"][0]["url"] == (
        ".solin_cache/report.pdf-unique-page_001.jpg"
    )


def test_watched_playlist_adopts_external_generated_files_into_cache(tmp_path):
    external = tmp_path.parent / f"external-page-{uuid.uuid4().hex}.jpg"
    external.write_bytes(b"page")
    playlist = {
        "items": [
            {
                "id": "page-id",
                "title": "Report — p. 1",
                "url": str(external),
                "type": "image",
            },
        ],
    }

    try:
        watched_folder_module.save_manifest_playlist(str(tmp_path), playlist)

        manifest = _read_manifest(tmp_path)
        saved_url = manifest["playlist"]["items"][0]["url"]
        runtime_url = playlist["items"][0]["url"]

        assert saved_url.startswith(".solin_cache/")
        assert "external-page" in saved_url
        assert Path(runtime_url).is_file()
        assert Path(runtime_url).parent == tmp_path / ".solin_cache"
        assert Path(runtime_url).read_bytes() == b"page"
        assert str(external) not in _read_manifest(
            tmp_path,
        )["playlist"]["items"][0]["url"]
    finally:
        external.unlink(missing_ok=True)


def test_watched_playlist_round_trips_prepared_image_framing(tmp_path):
    image = tmp_path / "slide.png"
    image.write_bytes(b"image")
    framing = {
        "version": 1,
        "zoom": 1.4,
        "norm_x": 0.12,
        "norm_y": -0.08,
    }
    playlist = {
        "items": [{
            "id": "slide-id",
            "title": "Slide",
            "url": str(image),
            "type": "image",
            "image_framing": framing,
        }],
        "sections": [],
        "markers": [],
    }

    watched_folder_module.save_manifest_playlist(str(tmp_path), playlist)
    loaded = watched_folder_module.load_manifest_playlist(str(tmp_path))
    manifest = _read_manifest(tmp_path)

    assert loaded["items"][0]["image_framing"] == framing
    assert manifest["playlist"]["items"][0]["image_framing"] == framing


def test_loading_watched_playlist_heals_existing_external_cache_url(tmp_path):
    external = tmp_path.parent / f"legacy-page-{uuid.uuid4().hex}.jpg"
    external.write_bytes(b"legacy")
    _write_manifest(
        tmp_path,
        {
            "version": 1,
            "processed": {},
            "playlist": {
                "items": [
                    {
                        "id": "page-id",
                        "title": "Legacy — p. 1",
                        "url": str(external),
                        "type": "image",
                    },
                ],
                "sections": [],
                "markers": [],
            },
        },
    )

    try:
        playlist = watched_folder_module.load_manifest_playlist(str(tmp_path))

        manifest = _read_manifest(tmp_path)
        saved_url = manifest["playlist"]["items"][0]["url"]
        runtime_url = playlist["items"][0]["url"]

        assert saved_url.startswith(".solin_cache/")
        assert str(external) not in saved_url
        assert Path(runtime_url).is_file()
        assert Path(runtime_url).read_bytes() == b"legacy"
    finally:
        external.unlink(missing_ok=True)


def test_scan_subfolder_includes_cache_files_referenced_by_playlist(tmp_path):
    cache = tmp_path / ".solin_cache"
    cache.mkdir()
    page = cache / "page_001.jpg"
    page.write_bytes(b"page")
    _write_manifest(
        tmp_path,
        {
            "version": 1,
            "processed": {},
            "playlist": {
                "items": [
                    {
                        "id": "page-id",
                        "title": "Report — p. 1",
                        "url": ".solin_cache/page_001.jpg",
                        "type": "image",
                    },
                ],
            },
        },
    )

    items = watched_folder_module.scan_subfolder(str(tmp_path))

    assert [item["url"] for item in items] == [str(page)]


def test_watched_playlist_does_not_persist_missing_windows_absolute_urls(tmp_path):
    playlist = {
        "items": [
            {
                "id": "page-id",
                "title": "Report — p. 1",
                "url": (
                    "C:\\Users\\TestUser\\AppData\\Local\\SolinDev\\"
                    "cache\\profiles\\profile_1\\pdf_pages\\report\\page_001.jpg"
                ),
                "type": "image",
            },
        ],
    }

    watched_folder_module.save_manifest_playlist(str(tmp_path), playlist)

    manifest = _read_manifest(tmp_path)
    saved_url = manifest["playlist"]["items"][0]["url"]

    assert saved_url == ".solin_cache/page_001.jpg"
    assert "C:\\Users" not in saved_url
    assert playlist["items"][0]["url"] == str(
        tmp_path / ".solin_cache" / "page_001.jpg"
    )


def test_partial_legacy_page_cache_is_scheduled_for_reprocessing(tmp_path):
    source = tmp_path / "slides.pdf"
    source.write_bytes(b"pdf")
    cache = tmp_path / ".solin_cache"
    cache.mkdir()
    partial_page = cache / "slides-page_001.jpg"
    partial_page.write_bytes(b"partial")
    fingerprint = watched_folder_module._file_fingerprint(source)
    _write_manifest(
        tmp_path,
        {
            "version": 1,
            "processed": {
                source.name: {
                    "type": "pdf",
                    **fingerprint,
                    "outputs": [partial_page.name],
                    "virtual_items": [],
                },
            },
        },
    )

    assert watched_folder_module.get_pending_files(str(tmp_path)) == [str(source)]


def test_completed_entry_is_committed_before_cancellation(monkeypatch, tmp_path):
    first = tmp_path / "first.jwlplaylist"
    second = tmp_path / "second.jwlplaylist"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    _write_manifest(
        tmp_path,
        {
            "version": 1,
            "processed": {},
            "playlist": {"items": [{"title": "keep me"}]},
        },
    )
    monkeypatch.setattr(
        watched_folder_module,
        "get_pending_files",
        lambda _path: [str(first), str(second)],
    )
    interruption_checks = 0

    def interruption_requested(_self):
        nonlocal interruption_checks
        interruption_checks += 1
        return interruption_checks >= 4

    monkeypatch.setattr(
        WatchedFolderSyncThread,
        "isInterruptionRequested",
        interruption_requested,
    )
    thread = _sync_thread(tmp_path)
    processed = []

    def process_file(fp, cache, _ext):
        processed.append(fp.name)
        output = cache / f"{fp.stem}-output.jpg"
        output.write_bytes(b"complete")
        return [str(output)], []

    monkeypatch.setattr(thread, "_process_file", process_file)

    thread.run()

    manifest = _read_manifest(tmp_path)
    assert processed == [first.name]
    assert first.name in manifest["processed"]
    assert second.name not in manifest["processed"]
    assert manifest["playlist"] == {"items": [{"title": "keep me"}]}


def test_manifest_commit_failure_rolls_back_new_outputs(monkeypatch, tmp_path):
    source = tmp_path / "media.jwlplaylist"
    source.write_bytes(b"playlist")
    output_name = "embedded_random.mp4"
    monkeypatch.setattr(
        watched_folder_module,
        "get_pending_files",
        lambda _path: [str(source)],
    )
    monkeypatch.setattr(
        watched_folder_module,
        "_commit_processed_entry",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )
    monkeypatch.setattr(
        WatchedFolderSyncThread,
        "isInterruptionRequested",
        lambda _self: False,
    )
    thread = _sync_thread(tmp_path)

    def process_file(_fp, cache, _ext):
        output = cache / output_name
        output.write_bytes(b"complete")
        return [str(output)], []

    monkeypatch.setattr(thread, "_process_file", process_file)

    thread.run()

    assert not (tmp_path / ".solin_cache" / output_name).exists()
    assert source.name not in _read_manifest(tmp_path)["processed"]


def test_cancelled_embedded_playlist_output_stays_in_staging(monkeypatch, tmp_path):
    from solin.core.playlists import reader as playlist_reader

    source = tmp_path / "media.jwlplaylist"
    source.write_bytes(b"playlist")
    cache = tmp_path / ".solin_cache"
    cache.mkdir()
    monkeypatch.setattr(
        playlist_reader,
        "read_jwlplaylist",
        lambda *_args, **_kwargs: {
            "items": [
                {"filename": "first.mp4", "data": b"first"},
                {"filename": "second.mp4", "data": b"second"},
            ],
        },
    )
    interruption_checks = 0

    def interruption_requested(_self):
        nonlocal interruption_checks
        interruption_checks += 1
        return interruption_checks >= 3

    monkeypatch.setattr(
        WatchedFolderSyncThread,
        "isInterruptionRequested",
        interruption_requested,
    )
    thread = _sync_thread(tmp_path)

    with pytest.raises(InterruptedError):
        thread._process_jwlplaylist(source, cache)

    assert list(cache.iterdir()) == []


if __name__ == "__main__":
    unittest.main()
