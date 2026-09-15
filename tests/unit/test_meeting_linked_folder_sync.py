from __future__ import annotations

import json
import tempfile
import unittest
import uuid
from collections.abc import Callable
from concurrent.futures import Future
from datetime import date
from pathlib import Path
from types import SimpleNamespace

from solin.core.foundation.thread_workers import CancellationFlag
from solin.core.foundation.constants import QSETTINGS_PREFS_APP
from solin.core.foundation.settings_keys import SettingsKey
from solin.core.foundation.settings_store import SettingsStore
from solin.core.ingest.manifest import (
    CACHE_DIR_NAME,
    MANIFEST_FILE,
    ManifestWriteError,
)
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


class _Timer:
    def __init__(self):
        self.interval = None

    def start(self, interval=0):
        self.interval = interval

    def stop(self):
        self.interval = None


class _ImmediateExecutor:
    def submit(self, function, *args, **kwargs):
        future = Future()
        try:
            future.set_result(function(*args, **kwargs))
        except BaseException as exc:  # noqa: BLE001 - test executor boundary
            future.set_exception(exc)
        return future


class _ForwardSignal:
    def __init__(self, callback):
        self._callback = callback

    def emit(self, *args):
        self._callback(*args)


def _configure_sync_save_queue(controller) -> None:
    controller._pending_sync_saves = {}
    controller._sync_save_inflight = None
    controller._sync_save_future = None
    controller._sync_save_executor = _ImmediateExecutor()
    controller._media_tree_runtime = SimpleNamespace(
        resource_lanes=SimpleNamespace(run=lambda _key, action: action())
    )
    controller._sync_save_timer = _Timer()
    controller._schedule_sync_manifest_save = (
        lambda **kwargs: MeetingTreeController._schedule_sync_manifest_save(
            controller,
            **kwargs,
        )
    )
    controller._arm_sync_save_timer = (
        lambda: MeetingTreeController._arm_sync_save_timer(controller)
    )
    controller._drain_sync_manifest_saves = (
        lambda: MeetingTreeController._drain_sync_manifest_saves(controller)
    )
    controller._on_sync_save_completed = (
        lambda *args: MeetingTreeController._on_sync_save_completed(controller, *args)
    )
    controller._emit_sync_save_completed = (
        lambda *args: MeetingTreeController._emit_sync_save_completed(controller, *args)
    )
    controller._notify_sync_save_failure = (
        lambda *args: MeetingTreeController._notify_sync_save_failure(controller, *args)
    )
    controller._syncSaveCompleted = _ForwardSignal(controller._on_sync_save_completed)


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
                "image_framing": {
                    "version": 1,
                    "zoom": 1.4,
                    "norm_x": 0.12,
                    "norm_y": -0.08,
                },
            }]

            record = service.save_tree(
                folder,
                _identity("mwb"),
                nodes=nodes,
                deleted_source_keys={"official"},
                linked_folder_files={str(media): "media"},
                meeting_folder_imports={},
                expected_revision=0,
                canonical_reset_generation=3,
                hidden_canonical_media={
                    "media:hidden": {
                        "id": "hidden",
                        "type": "media",
                        "title": "Resolved hidden title",
                        "children": [],
                        "meeting_generated": True,
                        "meeting_source_key": "media:hidden",
                        "resolved_url": "https://cdn.example.invalid/hidden.mp4",
                        "media_ref": {"file_path": "", "mime_type": "video/mp4"},
                    }
                },
            )
            raw = json.loads((folder / MANIFEST_FILE).read_text(encoding="utf-8"))
            saved_node = raw["meeting_tree"]["nodes"][0]

            self.assertEqual(record.revision, 1)
            self.assertEqual(record.canonical_reset_generation, 3)
            self.assertEqual(raw["meeting_tree"]["schema_version"], 3)
            self.assertEqual(saved_node["media_ref"]["file_path"], "talk.mp4")
            self.assertEqual(
                raw["meeting_tree"]["hidden_canonical_media"]["media:hidden"]["title"],
                "Resolved hidden title",
            )
            self.assertNotIn("thumbnail_local_path", saved_node)
            self.assertEqual(saved_node["image_framing"], nodes[0]["image_framing"])
            loaded = service.load_tree(str(root), _identity("mwb"))
            self.assertIsNotNone(loaded)
            assert loaded is not None
            self.assertEqual(
                loaded.nodes[0]["media_ref"]["file_path"],
                str(media),
            )
            self.assertEqual(
                loaded.nodes[0]["image_framing"],
                nodes[0]["image_framing"],
            )
            self.assertEqual(loaded.deleted_source_keys, {"official"})
            self.assertEqual(
                loaded.hidden_canonical_media["media:hidden"]["resolved_url"],
                "https://cdn.example.invalid/hidden.mp4",
            )

    def test_save_tree_ignores_canonical_hash_only_changes(self):
        service = _linked_folder_sync()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / "2026-05-25 MW"
            folder.mkdir()
            media = folder / "talk.mp4"
            media.write_bytes(b"video")
            nodes = [{
                "id": "media",
                "type": "media",
                "title": "Talk",
                "children": [],
                "media_ref": {"file_path": str(media), "mime_type": "video/mp4"},
            }]
            first_identity = MeetingSyncIdentity(
                tree_key="mwb:2026-05-25:T:issue",
                pub_type="mwb",
                monday=date(2026, 5, 25),
                canonical_hash="hash-from-terminal-a",
            )
            second_identity = MeetingSyncIdentity(
                tree_key=first_identity.tree_key,
                pub_type=first_identity.pub_type,
                monday=first_identity.monday,
                canonical_hash="hash-from-terminal-b",
            )

            first = service.save_tree(
                folder,
                first_identity,
                nodes=nodes,
                deleted_source_keys=set(),
                linked_folder_files={str(media): "media"},
                meeting_folder_imports={},
                expected_revision=0,
            )
            manifest_path = folder / MANIFEST_FILE
            before = manifest_path.read_text(encoding="utf-8")

            second = service.save_tree(
                folder,
                second_identity,
                nodes=nodes,
                deleted_source_keys=set(),
                linked_folder_files={str(media): "media"},
                meeting_folder_imports={},
                expected_revision=first.revision,
            )

            self.assertEqual(second.revision, first.revision)
            self.assertEqual(second.canonical_hash, "hash-from-terminal-a")
            self.assertEqual(manifest_path.read_text(encoding="utf-8"), before)

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

    def test_newer_restore_generation_wins_stale_tombstones_and_keeps_manual_nodes(self):
        service = _linked_folder_sync()
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "2026-05-25 MW"
            folder.mkdir()
            official = {
                "id": "official",
                "type": "media",
                "title": "Official",
                "children": [],
                "media_ref": {"file_path": "", "mime_type": "video/mp4"},
                "meeting_generated": True,
                "meeting_source_key": "media:official",
            }
            remote_manual = {
                "id": "remote-manual",
                "type": "media",
                "children": [],
                "media_ref": {"file_path": ""},
                "meeting_generated": False,
            }
            local_manual = {
                "id": "local-manual",
                "type": "media",
                "children": [],
                "media_ref": {"file_path": ""},
                "meeting_generated": False,
            }
            stale_official_container = {
                "id": "stale-section",
                "type": "section",
                "title": "Stale",
                "children": [local_manual],
                "meeting_generated": True,
                "meeting_source_key": "section:stale",
            }
            restored = service.save_tree(
                folder,
                _identity("mwb"),
                nodes=[official, remote_manual],
                deleted_source_keys=set(),
                linked_folder_files={},
                meeting_folder_imports={},
                expected_revision=0,
                canonical_reset_generation=2,
            )

            stale = service.save_tree(
                folder,
                _identity("mwb"),
                nodes=[stale_official_container],
                deleted_source_keys={"media:official"},
                linked_folder_files={},
                meeting_folder_imports={},
                expected_revision=restored.revision - 1,
                canonical_reset_generation=1,
                hidden_canonical_media={"media:official": official},
            )

            self.assertEqual(stale.canonical_reset_generation, 2)
            self.assertEqual(stale.deleted_source_keys, set())
            self.assertEqual(stale.hidden_canonical_media, {})
            self.assertEqual(
                {node["id"] for node in stale.nodes},
                {"official", "remote-manual", "local-manual"},
            )

    def test_higher_incoming_generation_invalidates_old_tombstones(self):
        service = _linked_folder_sync()
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "2026-05-25 MW"
            folder.mkdir()
            old_manual = {
                "id": "old-manual",
                "type": "media",
                "children": [],
                "media_ref": {"file_path": ""},
                "meeting_generated": False,
            }
            previous = service.save_tree(
                folder,
                _identity("mwb"),
                nodes=[old_manual],
                deleted_source_keys={"media:official"},
                linked_folder_files={},
                meeting_folder_imports={},
                expected_revision=0,
                canonical_reset_generation=1,
                hidden_canonical_media={
                    "media:official": {
                        "id": "old-hidden",
                        "type": "media",
                        "children": [],
                        "meeting_generated": True,
                        "meeting_source_key": "media:official",
                        "media_ref": {"file_path": ""},
                    }
                },
            )
            restored_official = {
                "id": "official",
                "type": "media",
                "title": "Official",
                "children": [],
                "media_ref": {"file_path": "", "mime_type": "video/mp4"},
                "meeting_generated": True,
                "meeting_source_key": "media:official",
            }

            restored = service.save_tree(
                folder,
                _identity("mwb"),
                nodes=[restored_official],
                deleted_source_keys=set(),
                linked_folder_files={},
                meeting_folder_imports={},
                expected_revision=previous.revision - 1,
                canonical_reset_generation=2,
            )

            self.assertEqual(restored.canonical_reset_generation, 2)
            self.assertEqual(restored.deleted_source_keys, set())
            self.assertEqual(restored.hidden_canonical_media, {})
            self.assertEqual(
                {node["id"] for node in restored.nodes},
                {"official", "old-manual"},
            )

    def test_same_generation_unions_tombstones_and_manual_nodes(self):
        service = _linked_folder_sync()
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "2026-05-25 MW"
            folder.mkdir()
            first = service.save_tree(
                folder,
                _identity("mwb"),
                nodes=[{
                    "id": "remote-manual",
                    "type": "media",
                    "children": [],
                    "media_ref": {"file_path": ""},
                    "meeting_generated": False,
                }],
                deleted_source_keys={"media:a"},
                linked_folder_files={},
                meeting_folder_imports={},
                expected_revision=0,
                canonical_reset_generation=4,
                hidden_canonical_media={
                    "media:a": {
                        "id": "hidden-a",
                        "type": "media",
                        "children": [],
                        "meeting_generated": True,
                        "meeting_source_key": "media:a",
                        "media_ref": {"file_path": ""},
                    }
                },
            )

            merged = service.save_tree(
                folder,
                _identity("mwb"),
                nodes=[{
                    "id": "local-manual",
                    "type": "media",
                    "children": [],
                    "media_ref": {"file_path": ""},
                    "meeting_generated": False,
                }],
                deleted_source_keys={"media:b"},
                linked_folder_files={},
                meeting_folder_imports={},
                expected_revision=first.revision - 1,
                canonical_reset_generation=4,
                hidden_canonical_media={
                    "media:b": {
                        "id": "hidden-b",
                        "type": "media",
                        "children": [],
                        "meeting_generated": True,
                        "meeting_source_key": "media:b",
                        "media_ref": {"file_path": ""},
                    }
                },
            )

            self.assertEqual(merged.canonical_reset_generation, 4)
            self.assertEqual(merged.deleted_source_keys, {"media:a", "media:b"})
            self.assertEqual(
                set(merged.hidden_canonical_media),
                {"media:a", "media:b"},
            )
            self.assertEqual(
                {node["id"] for node in merged.nodes},
                {"remote-manual", "local-manual"},
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

    def test_materialize_same_named_content_reuses_file_and_first_node_mapping(self):
        service = _linked_folder_sync()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "first" / "clip.mp4"
            second = root / "second" / "clip.mp4"
            first.parent.mkdir()
            second.parent.mkdir()
            first.write_bytes(b"same-video")
            second.write_bytes(b"same-video")
            folder = root / "2026-05-25 MW"
            folder.mkdir()
            nodes = [
                {
                    "id": node_id,
                    "type": "media",
                    "children": [],
                    "media_ref": {"file_path": str(path)},
                }
                for node_id, path in (("first", first), ("second", second))
            ]

            materialized, linked = service.materialize_tree_files(
                nodes,
                folder,
                generated_roots=[],
            )

            copied = folder / "clip.mp4"
            self.assertEqual(
                [node["media_ref"]["file_path"] for node in materialized],
                [str(copied), str(copied)],
            )
            self.assertEqual(linked, {str(copied): "first"})

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

    def test_cancelled_materialization_rolls_back_files_created_by_transaction(self):
        service = _linked_folder_sync()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "first.mp4"
            second = root / "second.mp4"
            first.write_bytes(b"first")
            second.write_bytes(b"second")
            folder = root / "2026-05-25 MW"
            folder.mkdir()
            nodes = [
                {
                    "id": path.stem,
                    "type": "media",
                    "children": [],
                    "media_ref": {"file_path": str(path)},
                }
                for path in (first, second)
            ]
            cancellation = CancellationFlag()
            copy_file = service._copy_into_directory

            def cancel_after_first(*args, **kwargs):
                result = copy_file(*args, **kwargs)
                cancellation.set()
                return result

            service._copy_into_directory = cancel_after_first

            with self.assertRaisesRegex(Exception, "cancelled"):
                service.materialize_tree_files(
                    nodes,
                    folder,
                    generated_roots=[],
                    cancellation=cancellation,
                )

            self.assertFalse((folder / first.name).exists())
            self.assertFalse((folder / second.name).exists())

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
                    r"C:\Users\TestUser\AppData\Local\Solin\cache\page_001.jpg": "page",
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
                nodes=[
                    {
                        "id": "remote",
                        "type": "media",
                        "children": [],
                        "image_framing": {"scale": 1.0},
                    }
                ],
                deleted_source_keys={"remote-source"},
                linked_folder_files={"remote.mp4": "remote"},
                meeting_folder_imports={"remote.mp4": {"status": "processed"}},
                revision=7,
            )
            controller = FakeController()
            controller._sync_identity = _identity("mwb")
            controller._sync_folder = str(folder)
            controller._sync_revision = 3
            controller._nodes = [
                {
                    "id": "remote",
                    "type": "media",
                    "children": [],
                    "image_framing": {"scale": 1.4},
                }
            ]
            controller._image_framing_save_pending = True
            controller._deleted_source_keys = set()
            controller._linked_folder_files = {}
            controller._meeting_folder_imports = {}
            controller._sync_service = FakeService()
            controller.syncStateChanged = _Signal()
            local_saves = []
            _configure_sync_save_queue(controller)
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
            controller._save_local_cache = lambda: local_saves.append(True)

            MeetingTreeController._save_sync_manifest(controller)

            self.assertEqual(controller._nodes[0]["id"], "remote")
            self.assertEqual(
                controller._nodes[0]["image_framing"],
                {"scale": 1.4},
            )
            self.assertEqual(controller._deleted_source_keys, {"remote-source"})
            self.assertEqual(controller._linked_folder_files, {"remote.mp4": "remote"})
            self.assertEqual(controller._sync_revision, 7)
            self.assertEqual(local_saves, [True])

    def test_sync_discovery_publishes_canonical_reorder(self):
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
            controller._meeting_folder_scan_generation = 0
            controller._meeting_folder_scan_operation_id = ""
            controller._canonical_reset_generation = 0
            controller._hidden_canonical_media = {}
            controller._save_local_cache = lambda: True
            controller._schedule_sync_manifest_save = lambda: None
            controller._start_media_requests = lambda: None
            controller._emit_section_counts = (
                lambda: MeetingTreeController._emit_section_counts(controller)
            )
            controller.sectionCountsChanged = _Signal()
            controller.chromeChanged = _Signal()
            controller.syncStateChanged = _Signal()
            controller.stateChanged = _Signal()

            MeetingTreeController._apply_sync_discovery(
                controller,
                type(
                    "Discovery",
                    (),
                    {"available": True, "folder": folder, "record": record},
                )(),
            )

            self.assertEqual(len(controller.stateChanged.calls), 1)
            self.assertEqual(controller._nodes, record.nodes)
            self.assertEqual(controller._sync_revision, 2)

    def test_sync_discovery_publishes_section_state_without_manual_patch(self):
        class FakeController:
            pass

        controller = FakeController()
        controller._nodes = [
            {
                "id": "section",
                "type": "section",
                "title": "Before",
                "color_hue": 100,
                "collapsed": False,
                "children": [],
            },
        ]
        record = MeetingSyncRecord(
            folder=Path("C:/tmp/2026-05-25 MW"),
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
                    "title": "After",
                    "color_hue": 220,
                    "collapsed": True,
                    "children": [],
                },
            ],
            deleted_source_keys=set(),
            linked_folder_files={},
            meeting_folder_imports={},
            revision=2,
        )
        controller._sync_enabled = True
        controller._sync_revision = 1
        controller._deleted_source_keys = set()
        controller._linked_folder_files = {}
        controller._meeting_folder_imports = {}
        controller._meeting_folder_pending_sources = set()
        controller._canonical_reset_generation = 0
        controller._hidden_canonical_media = {}
        controller._save_local_cache = lambda: True
        controller._schedule_sync_manifest_save = lambda: None
        controller._start_media_requests = lambda: None
        controller._find_node = (
            lambda node_id: MeetingTreeController._find_node(controller, node_id)
        )
        controller._emit_section_changed = (
            lambda node: MeetingTreeController._emit_section_changed(controller, node)
        )
        controller._section_patch = (
            lambda node: MeetingTreeController._section_patch(controller, node)
        )
        controller._emit_section_counts = (
            lambda: MeetingTreeController._emit_section_counts(controller)
        )
        controller.sectionCountsChanged = _Signal()
        controller.chromeChanged = _Signal()
        controller.syncStateChanged = _Signal()
        controller.stateChanged = _Signal()

        MeetingTreeController._apply_sync_discovery(
            controller,
            type(
                "Discovery",
                (),
                {
                    "available": True,
                    "folder": record.folder,
                    "record": record,
                },
            )(),
        )

        self.assertEqual(len(controller.stateChanged.calls), 1)
        self.assertEqual(controller._nodes, record.nodes)
        self.assertEqual(controller._sync_revision, 2)

    def test_save_sync_manifest_permanent_failure_keeps_sync_enabled_and_warns(self):
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
        _configure_sync_save_queue(controller)
        warnings = []
        controller._warn_sync_failed = lambda message, **_kwargs: warnings.append(message)
        controller._pause_sync_after_save_failure = (
            lambda message: MeetingTreeController._pause_sync_after_save_failure(
                controller,
                message,
            )
        )

        MeetingTreeController._save_sync_manifest(controller)

        self.assertTrue(controller._sync_enabled)
        self.assertEqual(controller._sync_revision, 4)
        self.assertEqual(warnings, ["manifest is unavailable"])
        self.assertFalse(controller._pending_sync_saves)

    def test_save_sync_manifest_retries_transient_lock_without_warning(self):
        class FakeController:
            pass

        class FlakyService:
            def __init__(self):
                self.calls = 0

            def save_tree(self, folder, identity, **_kwargs):
                self.calls += 1
                if self.calls == 1:
                    cause = PermissionError("busy")
                    cause.winerror = 5
                    raise ManifestWriteError(
                        folder / MANIFEST_FILE,
                        operation="replace",
                        retryable=True,
                        cause=cause,
                    )
                return MeetingSyncRecord(
                    folder=folder,
                    tree_key=identity.tree_key,
                    pub_type=identity.pub_type,
                    monday=identity.monday,
                    meeting_tag=identity.meeting_tag,
                    folder_date=identity.monday,
                    canonical_hash=identity.canonical_hash,
                    nodes=[],
                    deleted_source_keys=set(),
                    linked_folder_files={},
                    meeting_folder_imports={},
                    revision=5,
                )

        controller = FakeController()
        controller._sync_identity = _identity("mwb")
        controller._sync_folder = "C:/tmp/2026-05-25 MW"
        controller._sync_enabled = True
        controller._sync_revision = 4
        controller._nodes = []
        controller._deleted_source_keys = set()
        controller._linked_folder_files = {}
        controller._meeting_folder_imports = {}
        controller._sync_service = FlakyService()
        controller.syncStateChanged = _Signal()
        warnings = []
        controller._warn_sync_failed = lambda message, **_kwargs: warnings.append(message)
        controller._apply_sync_record = (
            lambda record: MeetingTreeController._apply_sync_record(controller, record)
        )
        local_saves = []
        controller._save_local_cache = lambda: local_saves.append(True)
        _configure_sync_save_queue(controller)

        MeetingTreeController._save_sync_manifest(controller)

        self.assertEqual(controller._sync_service.calls, 1)
        self.assertTrue(controller._pending_sync_saves)
        self.assertEqual(warnings, [])
        request = next(iter(controller._pending_sync_saves.values()))
        request.next_attempt_at = 0

        MeetingTreeController._drain_sync_manifest_saves(controller)

        self.assertEqual(controller._sync_service.calls, 2)
        self.assertFalse(controller._pending_sync_saves)
        self.assertEqual(controller._sync_revision, 5)
        self.assertEqual(warnings, [])
        self.assertEqual(local_saves, [True])

    def test_scheduled_manifest_saves_coalesce_latest_tree(self):
        class FakeController:
            pass

        controller = FakeController()
        controller._sync_identity = _identity("mwb")
        controller._sync_folder = "C:/tmp/2026-05-25 MW"
        controller._sync_revision = 1
        controller._nodes = [{"id": "first", "type": "media", "children": []}]
        controller._deleted_source_keys = set()
        controller._linked_folder_files = {}
        controller._meeting_folder_imports = {}
        controller.syncStateChanged = _Signal()
        _configure_sync_save_queue(controller)

        MeetingTreeController._schedule_sync_manifest_save(controller)
        controller._nodes = [{"id": "latest", "type": "media", "children": []}]
        MeetingTreeController._schedule_sync_manifest_save(controller)

        self.assertEqual(len(controller._pending_sync_saves), 1)
        request = next(iter(controller._pending_sync_saves.values()))
        self.assertEqual(request.nodes[0]["id"], "latest")

    def test_save_sync_manifest_io_failure_keeps_sync_enabled_and_warns(self):
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
        _configure_sync_save_queue(controller)
        warnings = []
        controller._warn_sync_failed = lambda message, **_kwargs: warnings.append(message)
        controller._pause_sync_after_save_failure = (
            lambda message: MeetingTreeController._pause_sync_after_save_failure(
                controller,
                message,
            )
        )

        MeetingTreeController._save_sync_manifest(controller)

        self.assertTrue(controller._sync_enabled)
        self.assertEqual(controller._sync_revision, 4)
        self.assertEqual(warnings, ["[Errno 28] No space left on device"])
        self.assertFalse(controller._pending_sync_saves)

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
            controller._tree_key = controller._sync_identity.tree_key
            controller._sync_root = str(folder.parent)
            controller._sync_folder = ""
            controller._sync_enabled = False
            controller._sync_busy = False
            controller._sync_revision = 0
            controller._nodes = [{"id": "local", "type": "media", "children": []}]
            controller._deleted_source_keys = set()
            controller._linked_folder_files = {}
            controller._meeting_folder_imports = {}
            controller._canonical_reset_generation = 0
            controller._hidden_canonical_media = {}
            controller._sync_service = service
            controller._linked_folder_availability = ()
            controller.chromeChanged = _Signal()
            controller.stateChanged = _Signal()
            controller.syncStateChanged = _Signal()
            controller.saved = False
            controller._generated_asset_roots = lambda: ()
            controller._cancel_sync_refresh = lambda: None
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
            controller._start_media_requests = lambda: None
            controller._warn_sync_failed = lambda _message: None

            def submit_modal_operation(**options):
                try:
                    value = options["runner"](
                        lambda _progress: None,
                        CancellationFlag(),
                    )
                    options["commit"](value)
                except Exception as exc:  # noqa: BLE001 - test operation boundary
                    options["failed"](str(exc))
                return True

            controller._submit_modal_operation = submit_modal_operation

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
            controller._meeting_folder_scan_generation = 0
            controller._meeting_folder_scan_operation_id = ""
            controller._sync_refresh_generation = 0
            controller._sync_refresh_operation_id = ""
            controller._pending_sync_saves = {}
            controller._canonical_reset_generation = 0
            controller._hidden_canonical_media = {}
            controller._sync_service = service
            controller.sectionCountsChanged = _Signal()
            controller.chromeChanged = _Signal()
            controller.stateChanged = _Signal()
            controller.syncStateChanged = _Signal()
            controller.saved = False
            controller.set_sync_root = (
                lambda path: MeetingTreeController.set_sync_root(controller, path)
            )
            controller._flush_image_framing_save = lambda: None
            controller._refresh_sync_availability = (
                lambda: MeetingTreeController._refresh_sync_availability(controller)
            )
            controller._pending_sync_save_for_identity = (
                lambda sync_identity: (
                    MeetingTreeController._pending_sync_save_for_identity(
                        controller,
                        sync_identity,
                    )
                )
            )
            controller._request_sync_refresh = (
                lambda: MeetingTreeController._request_sync_refresh(controller)
            )
            controller._apply_sync_discovery = (
                lambda discovery: MeetingTreeController._apply_sync_discovery(
                    controller,
                    discovery,
                )
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
            controller._schedule_sync_manifest_save = lambda: None
            controller._start_media_requests = lambda *_args: None
            controller._emit_section_counts = (
                lambda: MeetingTreeController._emit_section_counts(controller)
            )
            controller._watched_folder_file_store = WatchedFolderFileStore()
            controller._apply_meeting_folder_scan = (
                lambda folders, monday, pub_type: (
                    MeetingTreeController._apply_meeting_folder_scan(
                        controller,
                        folders,
                        monday,
                        pub_type,
                    )
                )
            )

            class ImmediateOperations:
                @staticmethod
                def submit(spec):
                    value = spec.runner(
                        lambda _progress: None,
                        CancellationFlag(),
                    )
                    spec.commit(value)
                    return True

                @staticmethod
                def cancel(_operation_id):
                    return None

            controller._media_tree_runtime = type(
                "Runtime",
                (),
                {"operations": ImmediateOperations()},
            )()
            controller._adopt_existing_meeting_folder_source = (
                lambda _source, _folder: []
            )
            controller._remove_previous_meeting_folder_nodes = lambda _record: None
            controller._import_meeting_folder_source = (
                lambda *_args: (_ for _ in ()).throw(
                    AssertionError("manifest adoption should not need import")
                )
            )
            controller._start_media_requests = lambda: None

            MeetingTreeController.inject_linked_folder_media(controller, str(root))

            self.assertTrue(controller._sync_enabled)
            self.assertEqual(controller._sync_revision, record.revision)
            self.assertEqual(controller._nodes, record.nodes)
            self.assertEqual(controller._deleted_source_keys, {"remote-source"})
            self.assertTrue(controller.saved)
            self.assertEqual(len(controller.stateChanged.calls), 1)


if __name__ == "__main__":
    unittest.main()
