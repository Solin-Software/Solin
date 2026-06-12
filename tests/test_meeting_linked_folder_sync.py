from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from app.core.foundation.settings_keys import SettingsKey
from app.core.ingest.manifest import CACHE_DIR_NAME, MANIFEST_FILE
from app.core.meetings.linked_folder_sync import (
    MeetingLinkedFolderSync,
    MeetingSyncIdentity,
)
from app.widgets.meetings.tree_controller import MeetingTreeController


class _Prefs:
    def __init__(self, values: dict[str, object] | None = None):
        self._values = values or {}

    def value(self, key, default=None, *_args):
        return self._values.get(key, default)


def _identity(pub_type: str = "mwb") -> MeetingSyncIdentity:
    return MeetingSyncIdentity(
        tree_key=f"{pub_type}:2026-05-25:T:issue",
        pub_type=pub_type,
        monday=date(2026, 5, 25),
        canonical_hash="hash",
    )


class MeetingLinkedFolderSyncTests(unittest.TestCase):
    def test_folder_name_uses_configured_meeting_day(self):
        service = MeetingLinkedFolderSync(_Prefs({
            SettingsKey.MEETING_MIDWEEK_DAY: 2,
            SettingsKey.MEETING_WEEKEND_DAY: 5,
        }))

        self.assertEqual(service.folder_name_for(_identity("mwb")), "2026-05-27 MW")
        self.assertEqual(service.folder_name_for(_identity("wt")), "2026-05-30 WE")

    def test_folder_name_falls_back_to_monday_when_day_is_unconfigured(self):
        service = MeetingLinkedFolderSync(_Prefs())

        self.assertEqual(service.folder_name_for(_identity("mwb")), "2026-05-25 MW")

    def test_locate_reuses_existing_folder_before_creating(self):
        service = MeetingLinkedFolderSync(_Prefs({
            SettingsKey.MEETING_MIDWEEK_DAY: 2,
        }))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            older = root / "2026-05-26 MW"
            desired = root / "2026-05-27 MW"
            older.mkdir()
            desired.mkdir()

            folder = service.locate_folder(str(root), _identity("mwb"), create=True)

            self.assertEqual(folder, desired)
            self.assertEqual(
                sorted(path.name for path in root.iterdir()),
                ["2026-05-26 MW", "2026-05-27 MW"],
            )

    def test_save_and_load_manifest_round_trips_relative_paths(self):
        service = MeetingLinkedFolderSync(_Prefs({
            SettingsKey.MEETING_MIDWEEK_DAY: 2,
        }))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / "2026-05-27 MW"
            folder.mkdir()
            media = folder / "talk.mp4"
            media.write_bytes(b"video")
            outside_thumb = root / "thumb.jpg"
            outside_thumb.write_bytes(b"thumb")
            nodes = [{
                "id": "media",
                "type": "media",
                "title": "Talk",
                "children": [],
                "media_ref": {"file_path": str(media), "mime_type": "video/mp4"},
                "thumbnail_local_path": str(outside_thumb),
                "thumbnail_cache_key": "thumb.jpg",
            }]

            revision = service.save_tree(
                folder,
                _identity("mwb"),
                nodes=nodes,
                deleted_source_keys={"official"},
                linked_folder_files={str(media): "media"},
                meeting_folder_imports={},
                expected_revision=0,
            )
            raw = json.loads((folder / MANIFEST_FILE).read_text(encoding="utf-8"))
            saved_node = raw["meeting_tree"]["nodes"][0]

            self.assertEqual(revision, 1)
            self.assertEqual(saved_node["media_ref"]["file_path"], "talk.mp4")
            self.assertNotIn("thumbnail_local_path", saved_node)
            loaded = service.load_tree(str(root), _identity("mwb"))
            self.assertIsNotNone(loaded)
            assert loaded is not None
            self.assertEqual(
                loaded.nodes[0]["media_ref"]["file_path"],
                str(media),
            )
            self.assertEqual(loaded.deleted_source_keys, {"official"})

    def test_manifest_import_records_are_portable_by_source_path(self):
        service = MeetingLinkedFolderSync(_Prefs())
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / "2026-05-25 MW"
            folder.mkdir()
            media = folder / "talk.mp4"
            media.write_bytes(b"video")

            service.save_tree(
                folder,
                _identity("mwb"),
                nodes=[],
                deleted_source_keys=set(),
                linked_folder_files={},
                meeting_folder_imports={
                    r"C:\OtherPC\2026-05-25 MW\talk.mp4": {
                        "source_key": r"C:\OtherPC\2026-05-25 MW\talk.mp4",
                        "path": str(media),
                        "name": "talk.mp4",
                        "kind": "media",
                        "signature": {"size": 5, "mtime_ns": 123},
                        "status": "processed",
                        "node_ids": ["node"],
                    },
                },
                expected_revision=0,
            )

            raw = json.loads((folder / MANIFEST_FILE).read_text(encoding="utf-8"))
            imports = raw["meeting_tree"]["meeting_folder_imports"]
            self.assertEqual(list(imports), ["talk.mp4"])
            self.assertEqual(imports["talk.mp4"]["path"], "talk.mp4")
            self.assertEqual(imports["talk.mp4"]["source_key"], "talk.mp4")

            loaded = service.load_tree(str(root), _identity("mwb"))
            self.assertIsNotNone(loaded)
            assert loaded is not None
            self.assertEqual(
                loaded.meeting_folder_imports["talk.mp4"]["path"],
                str(media),
            )

    def test_materialize_copies_manual_physical_file_to_root_without_deleting_origin(self):
        service = MeetingLinkedFolderSync(_Prefs())
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.mp4"
            source.write_bytes(b"video")
            folder = root / "2026-05-25 MW"
            folder.mkdir()
            nodes = [{
                "id": "media",
                "type": "media",
                "children": [],
                "media_ref": {"file_path": str(source)},
            }]

            materialized, linked = service.materialize_tree_files(
                nodes,
                folder,
                generated_roots=[],
            )

            copied = folder / "source.mp4"
            self.assertTrue(source.exists())
            self.assertTrue(copied.exists())
            self.assertEqual(materialized[0]["media_ref"]["file_path"], str(copied))
            self.assertEqual(linked[str(copied)], "media")

    def test_materialize_copies_meeting_generated_physical_file_to_cache(self):
        service = MeetingLinkedFolderSync(_Prefs())
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "official-image.jpg"
            source.write_bytes(b"image")
            folder = root / "2026-05-25 MW"
            folder.mkdir()
            nodes = [{
                "id": "official",
                "type": "media",
                "children": [],
                "meeting_generated": True,
                "media_ref": {"file_path": str(source)},
            }]

            materialized, linked = service.materialize_tree_files(
                nodes,
                folder,
                generated_roots=[],
            )

            cached = folder / CACHE_DIR_NAME / "official-image.jpg"
            self.assertTrue(source.exists())
            self.assertFalse((folder / "official-image.jpg").exists())
            self.assertTrue(cached.exists())
            self.assertEqual(materialized[0]["media_ref"]["file_path"], str(cached))
            self.assertEqual(linked[str(cached)], "official")

    def test_delete_sync_metadata_keeps_root_files(self):
        service = MeetingLinkedFolderSync(_Prefs())
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "2026-05-25 MW"
            folder.mkdir()
            root_file = folder / "talk.mp4"
            root_file.write_bytes(b"video")
            (folder / MANIFEST_FILE).write_text("{}", encoding="utf-8")
            cache = folder / CACHE_DIR_NAME
            cache.mkdir()
            (cache / "page.jpg").write_bytes(b"image")

            service.delete_sync_metadata(folder)

            self.assertTrue(root_file.exists())
            self.assertFalse((folder / MANIFEST_FILE).exists())
            self.assertFalse(cache.exists())

    def test_detach_cache_references_keeps_local_tree_independent_from_deleted_cache(self):
        service = MeetingLinkedFolderSync(_Prefs())
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / "2026-05-25 MW"
            cache = folder / CACHE_DIR_NAME
            durable = root / "durable"
            cache.mkdir(parents=True)
            cached_media = cache / "official-image.jpg"
            cached_media.write_bytes(b"image")
            manual_root = folder / "talk.mp4"
            manual_root.write_bytes(b"video")
            nodes = [
                {
                    "id": "official",
                    "type": "media",
                    "linked_folder_source": str(folder),
                    "children": [],
                    "media_ref": {"file_path": str(cached_media)},
                },
                {
                    "id": "manual",
                    "type": "media",
                    "linked_folder_source": str(folder),
                    "children": [],
                    "media_ref": {"file_path": str(manual_root)},
                },
            ]

            detached = service.detach_cache_references(nodes, folder, durable)
            service.delete_sync_metadata(folder)

            official_path = Path(detached[0]["media_ref"]["file_path"])
            manual_path = Path(detached[1]["media_ref"]["file_path"])
            self.assertTrue(official_path.is_file())
            self.assertTrue(official_path.is_relative_to(durable))
            self.assertEqual(manual_path, manual_root)
            self.assertTrue(manual_root.exists())
            self.assertFalse(cache.exists())
            self.assertNotIn("linked_folder_source", detached[0])
            self.assertNotIn("linked_folder_source", detached[1])


class MeetingTreeControllerSyncTests(unittest.TestCase):
    def test_prepare_nodes_for_sync_uses_copied_file(self):
        class FakeController:
            pass

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.mp4"
            source.write_bytes(b"video")
            folder = root / "2026-05-25 MW"
            folder.mkdir()
            controller = FakeController()
            controller._sync_enabled = True
            controller._sync_folder = str(folder)
            controller._sync_service = MeetingLinkedFolderSync(_Prefs())
            controller._linked_folder_files = {}
            controller._generated_asset_roots = lambda: ()

            nodes = [{
                "id": "media",
                "type": "media",
                "children": [],
                "media_ref": {"file_path": str(source)},
            }]

            prepared = MeetingTreeController._prepare_nodes_for_sync(controller, nodes)

            copied = folder / "source.mp4"
            self.assertTrue(source.exists())
            self.assertTrue(copied.exists())
            self.assertEqual(prepared[0]["media_ref"]["file_path"], str(copied))
            self.assertEqual(controller._linked_folder_files[str(copied)], "media")


if __name__ == "__main__":
    unittest.main()
