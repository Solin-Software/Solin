from __future__ import annotations

import copy
import inspect
import json
import sqlite3
import tempfile
import unittest
import zipfile
from datetime import date
from pathlib import Path

import solin.core.meetings.tree_store as tree_store_module
import solin.core.meetings.memorial as memorial_module
import solin.core.meetings.publications as publications_module
from solin.core.ingest.watched_folder_files import WatchedFolderFileStore
from solin.core.meetings.meeting_folder_imports import (
    find_meeting_folder_import_record,
)
from solin.core.meetings.models import MeetingMedia, MeetingPublicationRef, WeekData
from solin.core.meetings.publication_content import (
    find_mwb_document_id,
    get_cbs_reference,
    get_mwb_publication_refs,
    make_media_item,
    open_publication_database,
)
from solin.core.meetings.tree_builder import MeetingTreeBuilder
from solin.core.meetings.tree_store import MeetingTreeStore
from solin.core.meetings.tree_store import flush_meeting_thumbs_dir
from solin.core.meetings.tree_merger import MeetingTreeMerger
from tests._paths import FIXTURES_DIR
from solin.widgets.meetings.tree_controller import MeetingTreeController


class PublicationSqlErrorBoundaryTests(unittest.TestCase):
    class _Connection:
        def __init__(self, error):
            self._error = error

        def execute(self, *_args, **_kwargs):
            raise self._error

    def test_expected_sqlite_error_uses_absence_fallback(self):
        conn = self._Connection(sqlite3.OperationalError("missing table"))

        self.assertIsNone(find_mwb_document_id(conn, date(2026, 5, 25)))

    def test_unexpected_query_error_is_not_silenced(self):
        conn = self._Connection(RuntimeError("programming error"))

        with self.assertRaisesRegex(RuntimeError, "programming error"):
            find_mwb_document_id(conn, date(2026, 5, 25))


def media(**kwargs) -> MeetingMedia:
    base = {
        "multimedia_id": 1,
        "mime_type": "image/jpeg",
        "file_path": "",
        "label": "Media",
        "caption": "",
        "begin_ordinal": 1,
        "key_symbol": "",
        "track": 0,
        "issue_tag": 0,
        "meps_doc_id": 0,
        "section": "tgw",
        "is_song": False,
        "cbs_article_title": "",
    }
    base.update(kwargs)
    return MeetingMedia(**base)


def multimedia_row(**kwargs) -> dict:
    base = {
        "MultimediaId": 1,
        "MimeType": "video/mp4",
        "FilePath": "",
        "Label": "Media",
        "Caption": "",
        "par": 1,
        "KeySymbol": "",
        "Track": 0,
        "IssueTagNumber": 0,
        "MepsDocumentId": 0,
    }
    base.update(kwargs)
    return base


class MeetingMediaPathTests(unittest.TestCase):
    def test_jwpub_image_file_path_becomes_existing_absolute_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            pub_dir = Path(tmp)
            image_path = pub_dir / "image.jpg"
            image_path.write_bytes(b"jpg")

            item = make_media_item(
                multimedia_row(MimeType="image/jpeg", FilePath="image.jpg"),
                pub_dir,
                "wt",
                False,
            )

            self.assertEqual(item.file_path, str(image_path))

    def test_jwpub_video_file_path_is_empty_when_file_is_not_extracted(self):
        with tempfile.TemporaryDirectory() as tmp:
            item = make_media_item(
                multimedia_row(
                    FilePath="w_LGP_202604_02_r720P.mp4",
                    KeySymbol="w",
                    Track=2,
                    IssueTagNumber=20260400,
                    MepsDocumentId=2026365,
                ),
                Path(tmp),
                "wt",
                False,
            )

            self.assertEqual(item.file_path, "")
            self.assertEqual(item.key_symbol, "w")
            self.assertEqual(item.track, 2)

    def test_jwpub_video_file_path_becomes_absolute_when_file_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            pub_dir = Path(tmp)
            video_path = pub_dir / "local.mp4"
            video_path.write_bytes(b"mp4")

            item = make_media_item(
                multimedia_row(FilePath="local.mp4"),
                pub_dir,
                "wt",
                False,
            )

            self.assertEqual(item.file_path, str(video_path))


class MeetingTreeBuilderTests(unittest.TestCase):
    def test_meeting_models_are_not_reexported_by_service_modules(self):
        self.assertFalse(hasattr(publications_module, "MeetingMedia"))
        self.assertFalse(hasattr(publications_module, "MeetingPublicationRef"))
        self.assertFalse(hasattr(publications_module, "WeekData"))
        self.assertFalse(hasattr(memorial_module, "MeetingMedia"))
        self.assertFalse(hasattr(memorial_module, "MemorialData"))

    def test_builder_uses_injected_presentation_text(self):
        builder = MeetingTreeBuilder(
            section_title=lambda source: f"translated:{source}",
            media_fallback_title=lambda: "Translated media",
        )
        tree = builder.build_weekend(
            WeekData(wt_all_media=[media(label="", caption="")])
        )

        self.assertEqual(tree[0]["title"], "translated:PUBLIC TALK")
        self.assertEqual(tree[1]["title"], "translated:Watchtower Study")
        self.assertEqual(tree[1]["children"][0]["title"], "Translated media")

    def test_midweek_builds_sections_and_cbs_markers(self):
        wd = WeekData(
            monday=date(2026, 5, 25),
            mwb_issue="20260500",
            mwb_all_media=[
                media(multimedia_id=10, label="Treasure", section="tgw", begin_ordinal=4),
                media(multimedia_id=20, label="Ministry", section="ayfm", begin_ordinal=20),
                media(multimedia_id=30, label="Life", section="lac", begin_ordinal=32),
                media(multimedia_id=70, label="", caption="", section="lac", begin_ordinal=34),
            ],
            mwb_publication_refs=[
                MeetingPublicationRef(
                    section="tgw",
                    begin_ordinal=8,
                    pub="cl",
                    publication_title="Synthetic Reference Manual  (cl)",
                    caption="cl p. 10 Synthetic reference topic",
                    meps_doc_id=2001,
                    items=[
                        media(
                            multimedia_id=60,
                            label="External image",
                            section="tgw",
                            begin_ordinal=1,
                        ),
                    ],
                ),
                MeetingPublicationRef(
                    section="lac",
                    begin_ordinal=38,
                    pub="lfb",
                    publication_title="Synthetic Study Guide  (lfb)",
                    caption="lfb p. 20 Synthetic study - Part 1",
                    meps_doc_id=3001,
                    is_cbs=True,
                    items=[
                        media(
                            multimedia_id=40,
                            label="Synthetic study image 1",
                            section="cbs",
                        ),
                    ],
                ),
                MeetingPublicationRef(
                    section="lac",
                    begin_ordinal=39,
                    pub="lfb",
                    publication_title="Synthetic Study Guide  (lfb)",
                    caption="lfb p. 21 Synthetic study - Part 2",
                    meps_doc_id=3002,
                    is_cbs=True,
                    items=[
                        media(
                            multimedia_id=41,
                            label="Synthetic study image 2",
                            section="cbs",
                        ),
                    ],
                ),
            ],
            cbs_ref={
                "pub": "lfb",
                "publication_title": "Synthetic Study Guide  (lfb)",
                "cbs_start": 38,
                "doc_titles": {
                    3001: "lfb p. 20 Synthetic study - Part 1",
                    3002: "lfb p. 21 Synthetic study - Part 2",
                },
            },
        )

        tree = MeetingTreeBuilder().build_midweek(wd)

        self.assertEqual([node["meeting_source_key"] for node in tree], [
            "section:mwb:tgw",
            "section:mwb:ayfm",
            "section:mwb:lac",
        ])
        tgw = tree[0]
        ext = next(node for node in tgw["children"] if node["type"] == "subsection")
        self.assertEqual(ext["title"], "Synthetic Reference Manual (cl)")
        self.assertTrue(ext["collapsed"])
        self.assertIn("bondade", ext["children"][0]["text"])
        lac = tree[2]
        placeholder = next(
            node for node in lac["children"]
            if node["type"] == "media" and node["title"] == "Media"
        )
        self.assertTrue(placeholder["auto_title"])
        cbs = next(node for node in lac["children"] if node["type"] == "subsection")
        self.assertEqual(cbs["meeting_source_key"], "subsection:cbs:lfb")
        self.assertEqual(cbs["title"], "Synthetic Study Guide (lfb)")
        self.assertFalse(cbs["collapsed"])
        markers = [node for node in cbs["children"] if node["type"] == "marker"]
        self.assertEqual(len(markers), 2)
        self.assertEqual(markers[0]["meeting_source_key"], "marker:cbs:3001")
        self.assertIn("Synthetic study - Part 1", markers[0]["text"])

    def test_does_not_create_empty_subsections(self):
        wd = WeekData(
            monday=date(2026, 5, 25),
            mwb_issue="20260500",
            mwb_all_media=[media(multimedia_id=10, label="Life", section="lac")],
            cbs_ref={
                "pub": "lfb",
                "publication_title": "Synthetic Study Guide (lfb)",
                "cbs_start": 38,
            },
            cbs_items=[],
            mwb_publication_refs=[
                MeetingPublicationRef(
                    section="tgw",
                    pub="cl",
                    publication_title="Synthetic Reference Manual (cl)",
                    caption="cl p. 10 Synthetic reference topic",
                    meps_doc_id=2001,
                    items=[],
                ),
            ],
        )
        tree = MeetingTreeBuilder().build_midweek(wd)
        subsections = [
            child
            for section in tree
            for child in section["children"]
            if child["type"] == "subsection"
        ]
        self.assertEqual(subsections, [])

    def test_cbs_detection_ignores_publication_after_final_song(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE Multimedia (
                MultimediaId INTEGER,
                KeySymbol TEXT
            );
            CREATE TABLE DocumentMultimedia (
                DocumentId INTEGER,
                MultimediaId INTEGER,
                BeginParagraphOrdinal INTEGER
            );
            CREATE TABLE RefPublication (
                RefPublicationId INTEGER,
                UndatedSymbol TEXT,
                IssueTagNumber INTEGER,
                PublicationType TEXT,
                DisplayTitle TEXT,
                ReferenceTitle TEXT,
                ShortTitle TEXT,
                Title TEXT,
                UndatedReferenceTitle TEXT
            );
            CREATE TABLE Extract (
                ExtractId INTEGER,
                Caption TEXT,
                RefMepsDocumentId INTEGER,
                RefPublicationId INTEGER
            );
            CREATE TABLE DocumentExtract (
                DocumentId INTEGER,
                ExtractId INTEGER,
                BeginParagraphOrdinal INTEGER
            );
        """)
        conn.executemany(
            "INSERT INTO Multimedia VALUES (?, ?)",
            [(1, "sjj"), (2, "sjj"), (3, "sjj")],
        )
        conn.executemany(
            "INSERT INTO DocumentMultimedia VALUES (?, ?, ?)",
            [(1, 1, 2), (1, 2, 32), (1, 3, 70)],
        )
        conn.executemany(
            "INSERT INTO RefPublication VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (1, "studyguide", 0, "Book", "Synthetic Study Guide", None, None, None, None),
                (2, "postfinal", 0, "Book", "Post-song reference", None, None, None, None),
            ],
        )
        conn.executemany(
            "INSERT INTO Extract VALUES (?, ?, ?, ?)",
            [
                (1, "Synthetic study - Part 1", 101, 1),
                (2, "Synthetic study - Part 2", 102, 1),
                (3, "Synthetic post-song reference", 999, 2),
            ],
        )
        conn.executemany(
            "INSERT INTO DocumentExtract VALUES (?, ?, ?)",
            [(1, 1, 50), (1, 2, 52), (1, 3, 80)],
        )

        try:
            cbs_ref = get_cbs_reference(conn, 1)
            assert cbs_ref is not None
            refs = get_mwb_publication_refs(conn, 1, cbs_ref)
        finally:
            conn.close()

        self.assertEqual(cbs_ref["pub"], "studyguide")
        self.assertEqual(cbs_ref["cbs_start"], 50)
        self.assertEqual(cbs_ref["meps_doc_ids"], [101, 102])
        self.assertTrue(any(ref.pub == "studyguide" and ref.is_cbs for ref in refs))
        self.assertTrue(any(ref.pub == "postfinal" and not ref.is_cbs for ref in refs))

    def test_weekend_has_public_talk_and_watchtower_sections(self):
        wd = WeekData(
            monday=date(2026, 5, 25),
            wt_issue="20260400",
            wt_all_media=[media(multimedia_id=50, section="wt", label="Study image")],
        )
        tree = MeetingTreeBuilder().build_weekend(wd)
        self.assertEqual(len(tree), 2)
        self.assertEqual(tree[0]["meeting_source_key"], "section:wt:public_talk")
        self.assertFalse(tree[0]["collapsed"])
        self.assertEqual(tree[1]["meeting_source_key"], "section:wt:wt")
        self.assertEqual(tree[1]["children"][0]["type"], "media")


class MeetingTreeMergerTests(unittest.TestCase):
    def canonical(self):
        return MeetingTreeBuilder().build_weekend(
            WeekData(
                monday=date(2026, 5, 25),
                wt_all_media=[
                    media(multimedia_id=1, section="wt", label="One"),
                    media(multimedia_id=2, section="wt", label="Two"),
                ],
            )
        )

    def wt_section(self, tree):
        return next(
            node for node in tree
            if node.get("meeting_source_key") == "section:wt:wt"
        )

    def test_first_load_uses_canonical(self):
        canonical = self.canonical()
        merged = MeetingTreeMerger(canonical).merge(None)
        self.assertEqual(
            [node["meeting_source_key"] for node in merged],
            ["section:wt:public_talk", "section:wt:wt"],
        )

    def test_preserves_reordered_official_children(self):
        canonical = self.canonical()
        section = self.wt_section(canonical)
        saved = [dict(section, children=list(reversed(section["children"])))]
        merged = MeetingTreeMerger(canonical).merge(saved)
        titles = [child["title"] for child in self.wt_section(merged)["children"]]
        self.assertEqual(titles, ["Two", "One"])

    def test_replaces_changed_official_in_user_position(self):
        canonical = self.canonical()
        saved = self.canonical()
        saved_section = self.wt_section(saved)
        saved_section["children"][0]["title"] = "Old"
        saved_section["children"][0]["meeting_source_hash"] = "old-hash"
        merged = MeetingTreeMerger(canonical).merge(saved)
        self.assertEqual(self.wt_section(merged)["children"][0]["title"], "One")

    def test_preserves_resolved_auto_title_for_placeholder_media(self):
        canonical = MeetingTreeBuilder().build_weekend(
            WeekData(
                monday=date(2026, 5, 25),
                wt_all_media=[media(multimedia_id=1, section="wt", label="", caption="")],
            )
        )
        saved = MeetingTreeBuilder().build_weekend(
            WeekData(
                monday=date(2026, 5, 25),
                wt_all_media=[media(multimedia_id=1, section="wt", label="", caption="")],
            )
        )
        saved_media = self.wt_section(saved)["children"][0]
        saved_media["title"] = "Resolved JW Title"
        saved_media["auto_title"] = False
        merged = MeetingTreeMerger(canonical).merge(saved)
        self.assertEqual(
            self.wt_section(merged)["children"][0]["title"],
            "Resolved JW Title",
        )

    def test_preserves_manual_and_removes_stale_generated(self):
        canonical = self.canonical()
        stale = {
            "id": "stale",
            "type": "media",
            "title": "Stale",
            "children": [],
            "meeting_generated": True,
            "meeting_source_key": "media:wt:missing",
        }
        manual = {
            "id": "manual",
            "type": "media",
            "title": "Manual",
            "children": [],
            "meeting_generated": False,
        }
        saved = [dict(self.wt_section(canonical), children=[stale, manual])]
        merged = MeetingTreeMerger(canonical).merge(saved)
        titles = [child["title"] for child in self.wt_section(merged)["children"]]
        self.assertNotIn("Stale", titles)
        self.assertIn("Manual", titles)

    def test_promotes_manual_children_from_stale_section(self):
        canonical = self.canonical()
        saved = [{
            "id": "old-section",
            "type": "section",
            "title": "Old section",
            "children": [{
                "id": "manual",
                "type": "media",
                "title": "Manual",
                "children": [],
                "meeting_generated": False,
            }],
            "meeting_generated": True,
            "meeting_source_key": "section:mwb:gone",
        }]
        merged = MeetingTreeMerger(canonical).merge(saved)
        self.assertEqual(merged[0]["id"], "manual")

    def test_deleted_official_source_is_not_reinserted(self):
        canonical = self.canonical()
        section = self.wt_section(canonical)
        deleted_key = section["children"][0]["meeting_source_key"]
        saved = [dict(section, children=[section["children"][1]])]
        merged = MeetingTreeMerger(canonical, {deleted_key}).merge(saved)
        titles = [child["title"] for child in self.wt_section(merged)["children"]]
        self.assertEqual(titles, ["Two"])

    def test_preserves_durable_media_metadata_for_same_identity(self):
        canonical = self.canonical()
        saved = self.canonical()
        saved_media = self.wt_section(saved)["children"][0]
        saved_media["resolved_url"] = "https://cdn.example/video.mp4"
        saved_media["thumbnail_url"] = "https://cdn.example/thumb.jpg"
        saved_media["thumbnail_local_path"] = r"C:\cache\meeting_thumbs\one.jpg"
        saved_media["thumbnail_cache_key"] = "one.jpg"
        saved_media["base_duration_ticks"] = 123_000_000

        merged = MeetingTreeMerger(canonical).merge(saved)
        media_node = self.wt_section(merged)["children"][0]

        self.assertEqual(media_node["resolved_url"], saved_media["resolved_url"])
        self.assertEqual(media_node["thumbnail_url"], saved_media["thumbnail_url"])
        self.assertEqual(media_node["thumbnail_local_path"], saved_media["thumbnail_local_path"])
        self.assertEqual(media_node["thumbnail_cache_key"], saved_media["thumbnail_cache_key"])
        self.assertEqual(media_node["base_duration_ticks"], 123_000_000)

    def test_discards_durable_media_metadata_when_identity_changes(self):
        canonical = self.canonical()
        saved = self.canonical()
        saved_media = self.wt_section(saved)["children"][0]
        saved_media["media_ref"]["track"] = 99
        saved_media["resolved_url"] = "https://cdn.example/stale.mp4"
        saved_media["base_duration_ticks"] = 123_000_000

        merged = MeetingTreeMerger(canonical).merge(saved)
        media_node = self.wt_section(merged)["children"][0]

        self.assertNotIn("resolved_url", media_node)
        self.assertNotIn("base_duration_ticks", media_node)

    def test_preserves_durable_metadata_when_jw_file_path_was_normalized(self):
        canonical = MeetingTreeBuilder().build_weekend(
            WeekData(
                monday=date(2026, 6, 8),
                wt_all_media=[
                    media(
                        multimedia_id=15,
                        mime_type="video/mp4",
                        file_path="",
                        label="O “Deus da verdade” cumpre sempre o que promete",
                        section="wt",
                        key_symbol="w",
                        track=2,
                        issue_tag=20260400,
                        meps_doc_id=2026365,
                    ),
                ],
            )
        )
        saved = MeetingTreeBuilder().build_weekend(
            WeekData(
                monday=date(2026, 6, 8),
                wt_all_media=[
                    media(
                        multimedia_id=15,
                        mime_type="video/mp4",
                        file_path="w_LGP_202604_02_r720P.mp4",
                        label="O “Deus da verdade” cumpre sempre o que promete",
                        section="wt",
                        key_symbol="w",
                        track=2,
                        issue_tag=20260400,
                        meps_doc_id=2026365,
                    ),
                ],
            )
        )
        saved_media = self.wt_section(saved)["children"][0]
        saved_media["resolved_url"] = "https://cdn.example/w_LGP_202604_02.mp4"
        saved_media["thumbnail_url"] = "https://cdn.example/thumb.jpg"

        merged = MeetingTreeMerger(canonical).merge(saved)
        media_node = self.wt_section(merged)["children"][0]

        self.assertEqual(media_node["media_ref"]["file_path"], "")
        self.assertEqual(media_node["resolved_url"], saved_media["resolved_url"])
        self.assertEqual(media_node["thumbnail_url"], saved_media["thumbnail_url"])


class MeetingTreeControllerMediaResolutionTests(unittest.TestCase):
    def test_stale_jwpub_video_file_path_falls_back_to_jw_resolution(self):
        calls: list[tuple[str, MeetingMedia]] = []

        class FakeService:
            def resolve_video_async(self, request_id: str, item: MeetingMedia) -> None:
                calls.append((request_id, item))

        class FakeController:
            pass

        controller = FakeController()
        controller._resolved_urls = {}
        controller._resolve_to_node_id = {}
        controller._svc = FakeService()
        controller._url_for_node = (
            lambda node: MeetingTreeController._url_for_node(controller, node)
        )
        controller._has_local_thumbnail = lambda node: False
        controller._duration_ticks = lambda node: 0
        controller._media_type_from_ref = lambda ref: "video"
        controller._queue_info = lambda *args, **kwargs: None
        controller._emit_cloud_for_node = lambda item_id: None

        node = {
            "id": "video-node",
            "type": "media",
            "media_type": "video",
            "media_ref": {
                "file_path": "w_LGP_202604_02_r720P.mp4",
                "key_symbol": "w",
                "track": 2,
                "issue_tag": 20260400,
                "meps_doc_id": 2026365,
                "mime_type": "video/mp4",
            },
        }

        self.assertEqual(MeetingTreeController._url_for_node(controller, node), "")

        MeetingTreeController._start_media_request(controller, node)

        self.assertEqual(len(calls), 1)
        request_id, item = calls[0]
        self.assertIn(request_id, controller._resolve_to_node_id)
        self.assertEqual(controller._resolve_to_node_id[request_id], "video-node")
        self.assertEqual(item.key_symbol, "w")
        self.assertEqual(item.track, 2)

    def test_missing_plain_local_file_still_reports_missing_url(self):
        class FakeController:
            pass

        controller = FakeController()
        controller._resolved_urls = {}
        with tempfile.TemporaryDirectory() as tmp:
            missing_path = str(Path(tmp) / "missing-local-video.mp4")
            node = {
                "id": "local-node",
                "type": "media",
                "media_ref": {"file_path": missing_path},
            }

            self.assertEqual(
                MeetingTreeController._url_for_node(controller, node),
                missing_path,
            )


class MeetingTreeControllerEditingTests(unittest.TestCase):
    class _Signal:
        def __init__(self):
            self.calls = []

        def emit(self, *args):
            self.calls.append(args)

    def controller(self, nodes):
        class FakeController:
            pass

        controller = FakeController()
        controller._nodes = nodes
        controller.saved = 0
        controller.count_updates = 0
        controller.chromeChanged = self._Signal()
        controller._save = lambda: setattr(
            controller,
            "saved",
            controller.saved + 1,
        )
        controller._emit_section_counts = lambda: setattr(
            controller,
            "count_updates",
            controller.count_updates + 1,
        )
        return controller

    def test_placement_playlist_ref_uses_current_tree_snapshot(self):
        controller = self.controller([
            {"id": "media", "type": "media", "children": []},
        ])

        result = MeetingTreeController.placement_playlist_ref(controller)

        self.assertEqual(result, {"items": [{"id": "media"}], "sections": []})

    def test_move_node_reparents_media_and_emits_persistence_updates(self):
        nodes = [
            {"id": "media", "type": "media", "children": []},
            {
                "id": "section",
                "type": "section",
                "children": [],
            },
        ]
        controller = self.controller(nodes)

        moved = MeetingTreeController.moveNode(
            controller,
            "media",
            "section:section",
            0,
        )

        self.assertTrue(moved)
        self.assertEqual([node["id"] for node in controller._nodes], ["section"])
        self.assertEqual(
            controller._nodes[0]["children"][0]["id"],
            "media",
        )
        self.assertEqual(controller.saved, 1)
        self.assertEqual(controller.count_updates, 1)
        self.assertEqual(controller.chromeChanged.calls, [()])

    def test_invalid_move_into_descendant_preserves_tree(self):
        nodes = [
            {
                "id": "section",
                "type": "section",
                "children": [
                    {
                        "id": "subsection",
                        "type": "subsection",
                        "children": [],
                    },
                ],
            },
            {"id": "media", "type": "media", "children": []},
        ]
        before = copy.deepcopy(nodes)
        controller = self.controller(nodes)

        moved = MeetingTreeController.moveNode(
            controller,
            "section",
            "subsection:subsection",
            0,
        )

        self.assertFalse(moved)
        self.assertEqual(controller._nodes, before)
        self.assertEqual(controller.saved, 0)
        self.assertEqual(controller.count_updates, 0)


class MeetingTreeStoreTests(unittest.TestCase):
    def test_store_requires_an_explicit_path(self):
        path_param = inspect.signature(MeetingTreeStore).parameters["path"]

        self.assertIs(path_param.default, inspect.Parameter.empty)
        source = tree_store_module.__file__
        self.assertIsNotNone(source)
        self.assertNotIn(
            "ProfileManager",
            Path(source).read_text(encoding="utf-8"),
        )

    def test_can_use_explicit_store_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "custom_tree_store.json"
            store = MeetingTreeStore(path=path)
            nodes = [{"id": "n1", "type": "section", "children": []}]

            store.save("mwb:2026-05-25:T:20260500", nodes, "hash")

            self.assertEqual(store.path, path)
            self.assertTrue(path.exists())
            self.assertEqual(store.load("mwb:2026-05-25:T:20260500"), (nodes, "hash"))

    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = MeetingTreeStore(Path(tmp) / "meeting_trees.json")
            nodes = [{"id": "n1", "type": "section", "children": []}]
            store.save("mwb:2026-05-25:T:20260500", nodes, "hash")
            loaded, digest = store.load("mwb:2026-05-25:T:20260500")
            self.assertEqual(loaded, nodes)
            self.assertEqual(digest, "hash")

    def test_round_trip_meeting_folder_imports(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = MeetingTreeStore(Path(tmp) / "meeting_trees.json")
            imports = {
                "source-key": {
                    "source_key": "source-key",
                    "path": str(Path(tmp) / "2026-05-26 MW" / "slides.pdf"),
                    "name": "slides.pdf",
                    "kind": "pdf",
                    "signature": {"size": 100, "mtime_ns": 123456},
                    "status": "processed",
                    "node_ids": ["page-1", "page-2"],
                }
            }
            store.save(
                "mwb:2026-05-25:T:20260500",
                [{"id": "n1", "type": "section", "children": []}],
                "hash",
                meeting_folder_imports=imports,
            )

            self.assertEqual(
                store.load_meeting_folder_imports("mwb:2026-05-25:T:20260500"),
                imports,
            )

    def test_flush_meeting_thumbs_keeps_referenced_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            thumb_dir = Path(tmp) / "meeting_thumbs"
            thumb_dir.mkdir()
            keep = thumb_dir / "keep.jpg"
            stale = thumb_dir / "stale.jpg"
            keep.write_bytes(b"keep")
            stale.write_bytes(b"stale")
            store = MeetingTreeStore(Path(tmp) / "meeting_trees.json")
            store.save(
                "wt:2026-05-25:T:20260400",
                [{
                    "id": "keep",
                    "type": "media",
                    "children": [],
                    "thumbnail_local_path": str(keep),
                }],
                "hash",
            )

            flush_meeting_thumbs_dir(store=store, thumb_dir=thumb_dir)

            self.assertTrue(keep.exists())
            self.assertFalse(stale.exists())

    def test_flush_meeting_thumbs_fails_closed_when_store_is_corrupt(self):
        with tempfile.TemporaryDirectory() as tmp:
            thumb_dir = Path(tmp) / "meeting_thumbs"
            thumb_dir.mkdir()
            stale = thumb_dir / "stale.jpg"
            stale.write_bytes(b"stale")
            store = MeetingTreeStore(Path(tmp) / "meeting_trees.json")
            store.path.write_text("{broken", encoding="utf-8")

            flush_meeting_thumbs_dir(store=store, thumb_dir=thumb_dir)

            self.assertTrue(stale.exists())

    def test_flush_meeting_thumbs_fails_closed_when_tree_shape_is_invalid(self):
        with tempfile.TemporaryDirectory() as tmp:
            thumb_dir = Path(tmp) / "meeting_thumbs"
            thumb_dir.mkdir()
            stale = thumb_dir / "stale.jpg"
            stale.write_bytes(b"stale")
            store = MeetingTreeStore(Path(tmp) / "meeting_trees.json")
            store.path.write_text(
                json.dumps({"trees": {"week": {"nodes": "not-a-list"}}}),
                encoding="utf-8",
            )

            flush_meeting_thumbs_dir(store=store, thumb_dir=thumb_dir)

            self.assertTrue(stale.exists())

    def test_synthetic_jwpub_fixture_identifies_study_references(self):
        fixture = FIXTURES_DIR / "synthetic_meeting_workbook.jwpub"
        if not fixture.exists():
            self.skipTest("synthetic jwpub fixture not present")
        with tempfile.TemporaryDirectory() as tmp:
            outer = Path(tmp) / "outer"
            inner = Path(tmp) / "inner"
            outer.mkdir()
            inner.mkdir()
            with zipfile.ZipFile(fixture) as zf:
                zf.extractall(outer)
            with zipfile.ZipFile(outer / "contents") as zf:
                zf.extractall(inner)
            db_path = next(inner.rglob("*.db"))
            conn = open_publication_database(db_path)
            doc_id = find_mwb_document_id(conn, date(2026, 5, 25))
            assert doc_id is not None
            cbs_ref = get_cbs_reference(conn, doc_id)
            assert cbs_ref is not None
            refs = get_mwb_publication_refs(conn, doc_id, cbs_ref)
            conn.close()
        self.assertEqual(cbs_ref["pub"], "studyguide")
        self.assertEqual(cbs_ref.get("publication_title", ""), "Synthetic Study Guide")
        self.assertTrue(any(ref.pub == "reference" for ref in refs))
        self.assertTrue(
            any("Synthetic Reference Manual" in ref.publication_title for ref in refs)
        )
        cbs_refs = [ref for ref in refs if ref.is_cbs]
        self.assertTrue(cbs_refs)
        self.assertTrue(all(ref.pub == "studyguide" for ref in cbs_refs))
        self.assertFalse(any(ref.pub == "th" for ref in refs))
        titles = list(cbs_ref.get("doc_titles", {}).values())
        self.assertTrue(any("Synthetic study - Part 1" in title for title in titles))
        self.assertTrue(any("Synthetic study - Part 2" in title for title in titles))


class JwpubImportFactoryWiringTests(unittest.TestCase):
    class _Signal:
        def __init__(self):
            self.callbacks = []

        def connect(self, callback):
            self.callbacks.append(callback)

    class _Thread:
        def __init__(self):
            self.items_ready = JwpubImportFactoryWiringTests._Signal()
            self.failed = JwpubImportFactoryWiringTests._Signal()
            self.finished = JwpubImportFactoryWiringTests._Signal()
            self.started = False

        def start(self):
            self.started = True

    class _Factory:
        def __init__(self):
            self.calls = []
            self.threads = []

        def create(self, path, **kwargs):
            thread = JwpubImportFactoryWiringTests._Thread()
            self.calls.append((path, kwargs))
            self.threads.append(thread)
            return thread

    def controller(self):
        factory = self._Factory()

        class FakeController:
            pass

        controller = FakeController()
        controller._language_code = "T"
        controller._jwpub_threads = []
        controller._jwpub_import_thread_factory = factory
        controller._jwpub_image_dir = lambda: "linked-folder-cache"
        controller._on_playlist_items_ready = lambda *_args: None
        controller._warn_import_failed = lambda *_args: None
        controller._insert_meeting_folder_nodes = lambda *_args: None
        controller._node_from_playlist_item = lambda *_args: {}
        controller._record_meeting_folder_failure = lambda *_args: None
        return controller, factory

    def test_manual_import_uses_dynamic_linked_folder_destination(self):
        controller, factory = self.controller()

        MeetingTreeController._import_jwpubs(
            controller,
            ["publication.jwpub"],
            "root",
            4,
        )

        self.assertEqual(
            factory.calls,
            [
                (
                    "publication.jwpub",
                    {
                        "lang": "T",
                        "dest_images_dir": "linked-folder-cache",
                        "parent": controller,
                    },
                )
            ],
        )
        self.assertTrue(factory.threads[0].started)

    def test_meeting_folder_import_uses_factory_default_destination(self):
        controller, factory = self.controller()

        MeetingTreeController._import_meeting_folder_jwpub(
            controller,
            {"path": "meeting-folder/publication.jwpub"},
            "root",
            2,
        )

        self.assertEqual(
            factory.calls,
            [
                (
                    "meeting-folder/publication.jwpub",
                    {"lang": "T", "parent": controller},
                )
            ],
        )
        self.assertTrue(factory.threads[0].started)


class MeetingTreeControllerMeetingFolderImportTests(unittest.TestCase):
    class _Signal:
        def __init__(self):
            self.calls = []

        def emit(self, *args):
            self.calls.append(args)

    def _wire_remove_item_controller(self, controller) -> None:
        controller._watched_folder_file_store = WatchedFolderFileStore()
        controller._find_node = (
            lambda node_id, nodes=None: MeetingTreeController._find_node(
                controller,
                node_id,
                nodes,
            )
        )
        controller._url_for_node = (
            lambda node: MeetingTreeController._url_for_node(controller, node)
        )
        controller._remember_deleted_sources = (
            lambda node, *, include_media: MeetingTreeController._remember_deleted_sources(
                controller,
                node,
                include_media=include_media,
            )
        )
        controller._replace_node = (
            lambda node_id, replacement: MeetingTreeController._replace_node(
                controller,
                node_id,
                replacement,
            )
        )
        controller._cleanup_meeting_folder_import_for_removed_node = (
            lambda node_id, file_path, *, source_removed: MeetingTreeController._cleanup_meeting_folder_import_for_removed_node(
                controller,
                node_id,
                file_path,
                source_removed=source_removed,
            )
        )
        controller.saved = False
        controller._save_and_emit_replace = (
            lambda _node_id, _replacement: setattr(controller, "saved", True)
        )

    def test_meeting_folder_imported_media_keeps_linked_folder_semantics(self):
        class FakeController:
            pass

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "talk.mp4"
            source.write_bytes(b"video")
            controller = FakeController()
            controller._resolved_urls = {}
            controller._linked_folder_files = {}
            controller._url_for_node = (
                lambda node: MeetingTreeController._url_for_node(controller, node)
            )
            captured = {}

            def fake_insert(list_id, insert_index, nodes):
                captured["list_id"] = list_id
                captured["insert_index"] = insert_index
                captured["nodes"] = nodes

            def fake_record(source_data, node_ids):
                captured["record_source"] = source_data
                captured["record_node_ids"] = node_ids

            controller._insert_nodes = fake_insert
            controller._record_meeting_folder_import = fake_record

            node = {
                "id": "auto-node",
                "type": "media",
                "media_ref": {"file_path": str(source)},
                "children": [],
                "meeting_generated": False,
            }
            source_data = {
                "source_key": "source-key",
                "folder_path": str(Path(tmp)),
            }

            MeetingTreeController._insert_meeting_folder_nodes(
                controller,
                source_data,
                [node],
                "root",
                0,
            )

            self.assertEqual(node["linked_folder_source"], str(Path(tmp)))
            self.assertEqual(controller._linked_folder_files[str(source)], "auto-node")
            self.assertEqual(captured["nodes"], [node])
            self.assertEqual(captured["record_node_ids"], ["auto-node"])

    def test_sync_active_scan_imports_new_root_file_into_midweek_target(self):
        class FakeController:
            pass

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            meeting = root / "2026-05-27 MW"
            meeting.mkdir()
            source = meeting / "manual.mp4"
            source.write_bytes(b"video")
            controller = FakeController()
            controller._tree_key = "mwb:2026-05-25:T:20260500"
            controller._sync_enabled = True
            controller._meeting_folder_pending_sources = set()
            controller._meeting_folder_imports = {}
            controller.chromeChanged = self._Signal()
            controller._watched_folder_file_store = WatchedFolderFileStore()
            captured = {}

            controller.set_sync_root = lambda _path: None
            controller._meeting_folder_target_list_id = lambda _pub: "section:lac"
            controller._adopt_existing_meeting_folder_source = (
                lambda _source_data, _folder_path: []
            )
            controller._remove_previous_meeting_folder_nodes = lambda _record: None
            controller._emit_linked_folder_availability_if_changed = lambda: None

            def fake_import(source_data, list_id, insert_index):
                captured["source"] = source_data
                captured["list_id"] = list_id
                captured["insert_index"] = insert_index

            controller._import_meeting_folder_source = fake_import

            MeetingTreeController.inject_linked_folder_media(controller, str(root))

            self.assertEqual(captured["source"]["path"], str(source))
            self.assertEqual(captured["list_id"], "section:lac")
            self.assertEqual(captured["insert_index"], 0)
            self.assertEqual(controller.chromeChanged.calls, [()])

    def test_meeting_folder_record_lookup_matches_same_path_across_source_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "2026-05-27 MW" / "manual.mp4")
            records = {
                "old-machine-key": {
                    "path": path,
                    "status": "processed",
                    "signature": {"size": 5, "mtime_ns": 123},
                }
            }

            record = find_meeting_folder_import_record(
                {"source_key": "new-machine-key", "path": path},
                records,
            )

            self.assertIs(record, records["old-machine-key"])

    def test_remove_linked_root_media_deletes_import_record(self):
        class FakeController:
            pass

        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "2026-05-27 MW"
            folder.mkdir()
            source = folder / "manual.mp4"
            source.write_bytes(b"video")
            controller = FakeController()
            controller._nodes = [
                {
                    "id": "manual",
                    "type": "media",
                    "linked_folder_source": str(folder),
                    "children": [],
                    "media_ref": {"file_path": str(source)},
                    "meeting_generated": False,
                }
            ]
            controller._resolved_urls = {}
            controller._linked_folder_files = {str(source): "manual"}
            controller._meeting_folder_imports = {
                "source-key": {
                    "source_key": "source-key",
                    "path": str(source),
                    "name": "manual.mp4",
                    "kind": "media",
                    "signature": {"size": 5, "mtime_ns": 123},
                    "status": "processed",
                    "node_ids": ["manual"],
                }
            }
            controller._deleted_source_keys = set()
            self._wire_remove_item_controller(controller)

            MeetingTreeController.removeItem(controller, "manual")

            self.assertFalse(source.exists())
            self.assertEqual(controller._nodes, [])
            self.assertEqual(controller._linked_folder_files, {})
            self.assertEqual(controller._meeting_folder_imports, {})
            self.assertTrue(controller.saved)

    def test_remove_linked_derived_output_keeps_source_record(self):
        class FakeController:
            pass

        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "2026-05-27 MW"
            cache = folder / ".solin_cache"
            cache.mkdir(parents=True)
            source = folder / "slides.pdf"
            source.write_bytes(b"pdf")
            page = cache / "slides-page_001.jpg"
            page.write_bytes(b"image")
            controller = FakeController()
            controller._nodes = [
                {
                    "id": "page-1",
                    "type": "media",
                    "linked_folder_source": str(folder),
                    "children": [],
                    "media_ref": {"file_path": str(page)},
                    "meeting_generated": False,
                }
            ]
            controller._resolved_urls = {}
            controller._linked_folder_files = {str(page): "page-1"}
            controller._meeting_folder_imports = {
                "source-key": {
                    "source_key": "source-key",
                    "path": str(source),
                    "name": "slides.pdf",
                    "kind": "pdf",
                    "signature": {"size": 3, "mtime_ns": 123},
                    "status": "processed",
                    "node_ids": ["page-1", "page-2"],
                }
            }
            controller._deleted_source_keys = set()
            self._wire_remove_item_controller(controller)

            MeetingTreeController.removeItem(controller, "page-1")

            self.assertFalse(page.exists())
            self.assertTrue(source.exists())
            self.assertEqual(controller._nodes, [])
            self.assertEqual(
                controller._meeting_folder_imports["source-key"]["node_ids"],
                ["page-2"],
            )
            self.assertTrue(controller.saved)


if __name__ == "__main__":
    unittest.main()
