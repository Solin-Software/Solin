from __future__ import annotations

import tempfile
import unittest
import os
from pathlib import Path

import pytest

from app.core.ingest import watched_folder as watched_folder_module
from app.core.ingest.watched_folder import (
    WatchedFolderSyncThread,
    local_file_availability_signature,
    meeting_folder_source_needs_processing,
    scan_meeting_folder_sources,
)


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


if __name__ == "__main__":
    unittest.main()
