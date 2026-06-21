from __future__ import annotations

import json
import tempfile
import unittest
import uuid
from collections.abc import Callable
from datetime import date
from pathlib import Path

from solin.core.foundation.constants import QSETTINGS_PREFS_APP
from solin.core.foundation.settings_keys import SettingsKey
from solin.core.foundation.settings_store import SettingsStore
from solin.core.ingest.manifest import CACHE_DIR_NAME, MANIFEST_FILE
from solin.core.ingest.watched_folder_files import WatchedFolderFileStore
from solin.core.meetings.linked_folder_sync import (
    MeetingLinkedFolderSync,
    MEETING_TREE_KEY,
    MeetingSyncIdentity,
    MeetingSyncError,
    MeetingSyncRecord,
)
from solin.core.meetings.schedule import UNCONFIGURED_WEEKDAY
from solin.core.meetings.schedule_settings import MeetingScheduleSettingsStore
from solin.core.profiles.settings import ProfileSettings
from solin.widgets.meetings.tree_controller import MeetingTreeController


def _schedule_settings(
    values: dict[str, object] | None = None,
) -> MeetingScheduleSettingsStore:
    profile_settings = ProfileSettings.for_profile_id(
        f"meeting_linked_folder_{uuid.uuid4().hex}"
    )
    settings = SettingsStore.for_namespace(
        profile_settings.organization,
        QSETTINGS_PREFS_APP,
    )
    settings.clear(sync=False)
    for key, value in (values or {}).items():
        settings.set_value(key, value, sync=False)
    settings.sync()
    return MeetingScheduleSettingsStore(settings)


def _weekday_resolver(values: dict[str, object] | None = None) -> Callable[[str], int]:
    schedule_settings = _schedule_settings(values)

    def weekday_for_pub_type(pub_type: str) -> int:
        schedule = schedule_settings.load()
        if pub_type == "mwb":
            weekday = schedule.midweek.weekday
        elif pub_type == "wt":
            weekday = schedule.weekend.weekday
        else:
            return UNCONFIGURED_WEEKDAY
        if 0 <= weekday <= 6:
            return weekday
        return UNCONFIGURED_WEEKDAY

    return weekday_for_pub_type


def _linked_folder_sync(
    values: dict[str, object] | None = None,
) -> MeetingLinkedFolderSync:
    return MeetingLinkedFolderSync(_weekday_resolver(values))


class _Signal:
    def __init__(self):
        self.calls = []

    def emit(self, *args):
        self.calls.append(args)


def _identity(pub_type: str = "mwb") -> MeetingSyncIdentity:
    return MeetingSyncIdentity(
        tree_key=f"{pub_type}:2026-05-25:T:issue",
        pub_type=pub_type,
        monday=date(2026, 5, 25),
        canonical_hash="hash",
    )


class MeetingLinkedFolderSyncTests(unittest.TestCase):
    def test_folder_name_uses_configured_meeting_day(self):
        service = _linked_folder_sync({
            SettingsKey.MEETING_MIDWEEK_DAY: 2,
            SettingsKey.MEETING_WEEKEND_DAY: 5,
        })

        self.assertEqual(service.folder_name_for(_identity("mwb")), "2026-05-27 MW")
        self.assertEqual(service.folder_name_for(_identity("wt")), "2026-05-30 WE")

    def test_folder_name_falls_back_to_monday_when_day_is_unconfigured(self):
        service = _linked_folder_sync()

        self.assertEqual(service.folder_name_for(_identity("mwb")), "2026-05-25 MW")

    def test_locate_reuses_existing_folder_before_creating(self):
        service = _linked_folder_sync({
            SettingsKey.MEETING_MIDWEEK_DAY: 2,
        })
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
        service = _linked_folder_sync({
            SettingsKey.MEETING_MIDWEEK_DAY: 2,
        })
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

            record = service.save_tree(
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

            self.assertEqual(record.revision, 1)
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
        service = _linked_folder_sync()
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

    def test_stale_save_returns_merged_record_for_next_save(self):
        service = _linked_folder_sync()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / "2026-05-25 MW"
            folder.mkdir()
            first = service.save_tree(
                folder,
                _identity("mwb"),
                nodes=[{
                    "id": "remote",
                    "type": "media",
                    "children": [],
                    "media_ref": {"file_path": ""},
                }],
                deleted_source_keys={"remote-source"},
                linked_folder_files={},
                meeting_folder_imports={},
                expected_revision=0,
            )

            merged = service.save_tree(
                folder,
                _identity("mwb"),
                nodes=[{
                    "id": "local",
                    "type": "media",
                    "children": [],
                    "media_ref": {"file_path": ""},
                }],
                deleted_source_keys=set(),
                linked_folder_files={},
                meeting_folder_imports={},
                expected_revision=first.revision - 1,
            )
            self.assertEqual(
                {node["id"] for node in merged.nodes},
                {"local", "remote"},
            )
            self.assertEqual(merged.deleted_source_keys, {"remote-source"})

            service.save_tree(
                folder,
                _identity("mwb"),
                nodes=merged.nodes,
                deleted_source_keys=merged.deleted_source_keys,
                linked_folder_files=merged.linked_folder_files,
                meeting_folder_imports=merged.meeting_folder_imports,
                expected_revision=merged.revision,
            )
            loaded = service.load_tree(str(root), _identity("mwb"))
            self.assertIsNotNone(loaded)
            assert loaded is not None
            self.assertEqual(
                {node["id"] for node in loaded.nodes},
                {"local", "remote"},
            )

    def test_materialize_copies_manual_physical_file_to_root_without_deleting_origin(self):
        service = _linked_folder_sync()
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
        service = _linked_folder_sync()
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

    def test_save_manifest_drops_nonportable_stale_linked_file_entries(self):
        service = _linked_folder_sync()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / "2026-05-25 MW"
            cache = folder / CACHE_DIR_NAME
            profile_cache = root / "profile-cache" / "pdf_pages" / "report"
            cache.mkdir(parents=True)
            profile_cache.mkdir(parents=True)
            cached_page = cache / "page_001.jpg"
            stale_profile_page = profile_cache / "page_001.jpg"
            cached_page.write_bytes(b"page")
            stale_profile_page.write_bytes(b"page")

            service.save_tree(
                folder,
                _identity("mwb"),
                nodes=[],
                deleted_source_keys=set(),
                linked_folder_files={
                    str(cached_page): "page",
                    str(stale_profile_page): "page",
                    r"C:\Users\Someone\AppData\Local\Solin\cache\page_001.jpg": "page",
                },
                meeting_folder_imports={},
                expected_revision=0,
            )

            raw = json.loads((folder / MANIFEST_FILE).read_text(encoding="utf-8"))
            self.assertEqual(
                raw["meeting_tree"]["linked_folder_files"],
                {f"{CACHE_DIR_NAME}/page_001.jpg": "page"},
            )

    def test_delete_sync_metadata_keeps_root_files(self):
        service = _linked_folder_sync()
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

    def test_delete_sync_metadata_removes_meeting_manifest_and_cache_with_processed_state(self):
        service = _linked_folder_sync()
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "2026-05-25 MW"
            folder.mkdir()
            root_file = folder / "report.pdf"
            root_file.write_bytes(b"pdf")
            manifest = {
                "version": 1,
                "processed": {
                    "report.pdf": {
                        "url": ".solin_cache/report-page_001.jpg",
                    }
                },
                MEETING_TREE_KEY: {
                    "tree_key": _identity("mwb").tree_key,
                    "pub_type": "mwb",
                    "monday": "2026-05-25",
                    "meeting_tag": "MW",
                    "nodes": [],
                },
            }
            (folder / MANIFEST_FILE).write_text(
                json.dumps(manifest),
                encoding="utf-8",
            )
            cache = folder / CACHE_DIR_NAME
            cache.mkdir()
            cached_output = cache / "report-page_001.jpg"
            cached_output.write_bytes(b"image")

            service.delete_sync_metadata(folder)

            self.assertTrue(root_file.exists())
            self.assertFalse((folder / MANIFEST_FILE).exists())
            self.assertFalse(cached_output.exists())
            self.assertFalse(cache.exists())

    def test_detach_cache_references_keeps_local_tree_independent_from_deleted_cache(self):
        service = _linked_folder_sync()
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
            self.assertEqual(detached[1]["linked_folder_source"], str(folder))


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
            controller._sync_service = _linked_folder_sync()
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

    def test_materialize_current_nodes_replaces_stale_processed_file_links(self):
        class FakeController:
            pass

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile_cache = root / "profile-cache" / "pdf_pages" / "report"
            profile_cache.mkdir(parents=True)
            source_page = profile_cache / "page_001.jpg"
            source_page.write_bytes(b"page")
            folder = root / "2026-05-25 MW"
            folder.mkdir()
            controller = FakeController()
            controller._sync_folder = str(folder)
            controller._sync_service = _linked_folder_sync()
            controller._linked_folder_files = {
                str(source_page): "page",
                r"C:\Users\Someone\AppData\Local\Solin\cache\page_001.jpg": "page",
            }
            controller._generated_asset_roots = lambda: (str(profile_cache.parent),)
            controller._nodes = [{
                "id": "page",
                "type": "media",
                "linked_folder_source": str(folder),
                "children": [],
                "media_ref": {"file_path": str(source_page)},
            }]

            MeetingTreeController._materialize_current_nodes_for_sync(controller)

            cached_page = folder / CACHE_DIR_NAME / "page_001.jpg"
            self.assertTrue(cached_page.exists())
            self.assertEqual(
                controller._nodes[0]["media_ref"]["file_path"],
                str(cached_page),
            )
            self.assertEqual(controller._linked_folder_files, {str(cached_page): "page"})

    def test_save_sync_manifest_adopts_saved_merged_record(self):
        class FakeController:
            pass

        class FakeService:
            def save_tree(self, *_args, **_kwargs):
                return saved_record

        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "2026-05-25 MW"
            folder.mkdir()
            saved_record = MeetingSyncRecord(
                folder=folder,
                tree_key=_identity("mwb").tree_key,
                pub_type="mwb",
                monday=date(2026, 5, 25),
                meeting_tag="MW",
                folder_date=date(2026, 5, 25),
                canonical_hash="hash",
                nodes=[{"id": "remote", "type": "media", "children": []}],
                deleted_source_keys={"remote-source"},
                linked_folder_files={"remote.mp4": "remote"},
                meeting_folder_imports={"remote.mp4": {"status": "processed"}},
                revision=7,
            )
            controller = FakeController()
            controller._sync_identity = _identity("mwb")
            controller._sync_folder = str(folder)
            controller._sync_revision = 3
            controller._nodes = [{"id": "local", "type": "media", "children": []}]
            controller._deleted_source_keys = set()
            controller._linked_folder_files = {}
            controller._meeting_folder_imports = {}
            controller._sync_service = FakeService()
            controller._apply_sync_record = (
                lambda record: MeetingTreeController._apply_sync_record(
                    controller,
                    record,
                )
            )
            controller._pause_sync_after_save_failure = (
                lambda message: MeetingTreeController._pause_sync_after_save_failure(
                    controller,
                    message,
                )
            )

            MeetingTreeController._save_sync_manifest(controller)

            self.assertEqual(controller._nodes, saved_record.nodes)
            self.assertEqual(controller._deleted_source_keys, {"remote-source"})
            self.assertEqual(controller._linked_folder_files, {"remote.mp4": "remote"})
            self.assertEqual(controller._sync_revision, 7)

    def test_refresh_sync_from_manifest_emits_incremental_reorder(self):
        class FakeController:
            pass

        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "2026-05-25 MW"
            folder.mkdir()
            record = MeetingSyncRecord(
                folder=folder,
                tree_key=_identity("mwb").tree_key,
                pub_type="mwb",
                monday=date(2026, 5, 25),
                meeting_tag="MW",
                folder_date=date(2026, 5, 25),
                canonical_hash="hash",
                nodes=[
                    {
                        "id": "section",
                        "type": "section",
                        "title": "Section",
                        "children": [
                            {
                                "id": "media",
                                "type": "media",
                                "title": "Media",
                                "children": [],
                            },
                        ],
                    },
                ],
                deleted_source_keys=set(),
                linked_folder_files={},
                meeting_folder_imports={},
                revision=2,
            )
            controller = FakeController()
            controller._nodes = [
                {
                    "id": "media",
                    "type": "media",
                    "title": "Media",
                    "children": [],
                },
                {
                    "id": "section",
                    "type": "section",
                    "title": "Section",
                    "children": [],
                },
            ]
            controller._sync_enabled = True
            controller._sync_revision = 1
            controller._deleted_source_keys = set()
            controller._linked_folder_files = {}
            controller._meeting_folder_imports = {}
            controller._meeting_folder_pending_sources = set()
            controller._load_sync_record = lambda: record
            controller._apply_sync_record = (
                lambda sync_record: MeetingTreeController._apply_sync_record(
                    controller,
                    sync_record,
                )
            )
            controller._linked_folder_availability = ()
            controller._linked_folder_availability_signature = lambda: ()
            controller._save_local_cache = lambda: True
            controller._start_media_requests = lambda: None
            controller._emit_section_counts = (
                lambda: MeetingTreeController._emit_section_counts(controller)
            )
            controller.nodeMoved = _Signal()
            controller.sectionCountsChanged = _Signal()
            controller.chromeChanged = _Signal()
            controller.syncStateChanged = _Signal()
            controller.stateChanged = _Signal()

            self.assertTrue(MeetingTreeController._refresh_sync_from_manifest(controller))

            self.assertEqual(
                controller.nodeMoved.calls,
                [
                    ("section", "root", 0),
                    ("media", "section:section", 0),
                ],
            )
            self.assertEqual(controller.stateChanged.calls, [])
            self.assertEqual(controller._nodes, record.nodes)
            self.assertEqual(controller._sync_revision, 2)

    def test_save_sync_manifest_failure_pauses_sync_and_warns(self):
        class FakeController:
            pass

        class FailingService:
            def save_tree(self, *_args, **_kwargs):
                raise MeetingSyncError("manifest is unavailable")

        controller = FakeController()
        controller._sync_identity = _identity("mwb")
        controller._sync_folder = "C:/tmp/2026-05-25 MW"
        controller._sync_enabled = True
        controller._sync_revision = 4
        controller._nodes = []
        controller._deleted_source_keys = set()
        controller._linked_folder_files = {}
        controller._meeting_folder_imports = {}
        controller._sync_service = FailingService()
        controller.syncStateChanged = _Signal()
        warnings = []
        controller._warn_sync_failed = warnings.append
        controller._pause_sync_after_save_failure = (
            lambda message: MeetingTreeController._pause_sync_after_save_failure(
                controller,
                message,
            )
        )

        MeetingTreeController._save_sync_manifest(controller)

        self.assertFalse(controller._sync_enabled)
        self.assertEqual(controller._sync_revision, 0)
        self.assertEqual(warnings, ["manifest is unavailable"])
        self.assertEqual(controller.syncStateChanged.calls, [()])

    def test_save_sync_manifest_io_failure_pauses_sync_and_warns(self):
        class FakeController:
            pass

        class FailingService:
            def save_tree(self, *_args, **_kwargs):
                raise OSError(28, "No space left on device")

        controller = FakeController()
        controller._sync_identity = _identity("mwb")
        controller._sync_folder = "C:/tmp/2026-05-25 MW"
        controller._sync_enabled = True
        controller._sync_revision = 4
        controller._nodes = []
        controller._deleted_source_keys = set()
        controller._linked_folder_files = {}
        controller._meeting_folder_imports = {}
        controller._sync_service = FailingService()
        controller.syncStateChanged = _Signal()
        warnings = []
        controller._warn_sync_failed = warnings.append
        controller._pause_sync_after_save_failure = (
            lambda message: MeetingTreeController._pause_sync_after_save_failure(
                controller,
                message,
            )
        )

        MeetingTreeController._save_sync_manifest(controller)

        self.assertFalse(controller._sync_enabled)
        self.assertEqual(controller._sync_revision, 0)
        self.assertEqual(warnings, ["[Errno 28] No space left on device"])
        self.assertEqual(controller.syncStateChanged.calls, [()])

    def test_enable_sync_adopts_existing_manifest_without_overwrite(self):
        class FakeController:
            pass

        class FakeService:
            def __init__(self):
                self.save_calls = 0

            def locate_folder(self, *_args, **_kwargs):
                return folder

            def load_tree(self, *_args, **_kwargs):
                return existing_record

            def save_tree(self, *_args, **_kwargs):
                self.save_calls += 1
                raise AssertionError("existing sync must be adopted, not overwritten")

        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "2026-05-25 MW"
            folder.mkdir()
            existing_record = MeetingSyncRecord(
                folder=folder,
                tree_key=_identity("mwb").tree_key,
                pub_type="mwb",
                monday=date(2026, 5, 25),
                meeting_tag="MW",
                folder_date=date(2026, 5, 25),
                canonical_hash="hash",
                nodes=[{"id": "remote", "type": "media", "children": []}],
                deleted_source_keys={"remote-source"},
                linked_folder_files={"remote.mp4": "remote"},
                meeting_folder_imports={"remote.mp4": {"status": "processed"}},
                revision=9,
            )
            service = FakeService()
            controller = FakeController()
            controller._sync_available = True
            controller._sync_identity = _identity("mwb")
            controller._sync_root = str(folder.parent)
            controller._sync_folder = ""
            controller._sync_enabled = False
            controller._sync_busy = False
            controller._sync_revision = 0
            controller._nodes = [{"id": "local", "type": "media", "children": []}]
            controller._deleted_source_keys = set()
            controller._linked_folder_files = {}
            controller._meeting_folder_imports = {}
            controller._sync_service = service
            controller._linked_folder_availability = ()
            controller.chromeChanged = _Signal()
            controller.stateChanged = _Signal()
            controller.syncStateChanged = _Signal()
            controller.saved = False
            controller._sync_snapshot = (
                lambda: MeetingTreeController._sync_snapshot(controller)
            )
            controller._restore_sync_snapshot = (
                lambda snapshot: MeetingTreeController._restore_sync_snapshot(
                    controller,
                    snapshot,
                )
            )
            controller._set_sync_busy = (
                lambda value: MeetingTreeController._set_sync_busy(controller, value)
            )
            controller._apply_sync_record = (
                lambda record: MeetingTreeController._apply_sync_record(
                    controller,
                    record,
                )
            )
            controller._save_local_cache = (
                lambda: setattr(controller, "saved", True)
            )
            controller._linked_folder_availability_signature = lambda: ()
            controller._warn_sync_failed = lambda _message: None

            MeetingTreeController._enable_sync(controller)

            self.assertEqual(service.save_calls, 0)
            self.assertEqual(controller._nodes, existing_record.nodes)
            self.assertEqual(controller._deleted_source_keys, {"remote-source"})
            self.assertEqual(controller._sync_folder, str(folder))
            self.assertTrue(controller._sync_enabled)
            self.assertEqual(controller._sync_revision, 9)
            self.assertTrue(controller.saved)

    def test_folder_refresh_auto_adopts_manifest_created_elsewhere(self):
        class FakeController:
            pass

        service = _linked_folder_sync()
        identity = _identity("mwb")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / "2026-05-25 MW"
            folder.mkdir()
            record = service.save_tree(
                folder,
                identity,
                nodes=[{"id": "remote", "type": "media", "children": []}],
                deleted_source_keys={"remote-source"},
                linked_folder_files={},
                meeting_folder_imports={},
                expected_revision=0,
            )
            controller = FakeController()
            controller._tree_key = identity.tree_key
            controller._sync_root = ""
            controller._sync_identity = identity
            controller._sync_available = False
            controller._sync_enabled = False
            controller._sync_busy = False
            controller._sync_folder = ""
            controller._sync_revision = 0
            controller._nodes = []
            controller._deleted_source_keys = set()
            controller._linked_folder_files = {}
            controller._meeting_folder_imports = {}
            controller._meeting_folder_pending_sources = set()
            controller._linked_folder_availability = ()
            controller._sync_service = service
            controller.chromeChanged = _Signal()
            controller.stateChanged = _Signal()
            controller.syncStateChanged = _Signal()
            controller.saved = False
            controller.set_sync_root = (
                lambda path: MeetingTreeController.set_sync_root(controller, path)
            )
            controller._refresh_sync_availability = (
                lambda: MeetingTreeController._refresh_sync_availability(controller)
            )
            controller._refresh_sync_from_manifest = (
                lambda: MeetingTreeController._refresh_sync_from_manifest(controller)
            )
            controller._load_sync_record = (
                lambda: MeetingTreeController._load_sync_record(controller)
            )
            controller._candidate_sync_folder = (
                lambda: MeetingTreeController._candidate_sync_folder(controller)
            )
            controller._apply_sync_record = (
                lambda sync_record: MeetingTreeController._apply_sync_record(
                    controller,
                    sync_record,
                )
            )
            controller._save_local_cache = (
                lambda: setattr(controller, "saved", True)
            )
            controller._start_media_requests = lambda *_args: None
            controller._linked_folder_availability_signature = lambda: ()
            controller._watched_folder_file_store = WatchedFolderFileStore()
            controller._adopt_existing_meeting_folder_source = (
                lambda _source, _folder: []
            )
            controller._remove_previous_meeting_folder_nodes = lambda _record: None
            controller._import_meeting_folder_source = (
                lambda *_args: (_ for _ in ()).throw(
                    AssertionError("manifest adoption should not need import")
                )
            )
            controller._emit_linked_folder_availability_if_changed = lambda: None

            MeetingTreeController.inject_linked_folder_media(controller, str(root))

            self.assertTrue(controller._sync_enabled)
            self.assertEqual(controller._sync_revision, record.revision)
            self.assertEqual(controller._nodes, record.nodes)
            self.assertEqual(controller._deleted_source_keys, {"remote-source"})
            self.assertTrue(controller.saved)


if __name__ == "__main__":
    unittest.main()
