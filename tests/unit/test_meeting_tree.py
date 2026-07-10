from __future__ import annotations

import copy
import inspect
import json
import sqlite3
import tempfile
import unittest
import zipfile
from collections import deque
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import solin.core.meetings.tree_store as tree_store_module
import solin.core.meetings.memorial as memorial_module
import solin.core.meetings.publications as publications_module
from solin.core.ingest.watched_folder_files import WatchedFolderFileStore
from solin.core.meetings.meeting_folder_imports import (
    find_meeting_folder_import_record,
)
from solin.core.meetings.models import MeetingMedia, MeetingPublicationRef, WeekData
from solin.core.meetings.publication_content import (
    dedup_multimedia_rows,
    find_mwb_document_id,
    get_cbs_reference,
    get_mwb_publication_refs,
    make_media_item,
    open_publication_database,
)
from solin.core.meetings.tree_builder import MeetingTreeBuilder
from solin.core.meetings.tree_store import (
    MeetingTreeOverview,
    MeetingTreeStore,
    make_meeting_tree_key,
)
from solin.core.meetings.tree_store import flush_meeting_thumbs_dir
from solin.core.meetings.tree_merger import (
    MeetingTreeMerger,
    media_identity_signature,
    merge_persisted_meeting_trees,
)
from solin.core.meetings.tree_types import iter_nodes
from tests._paths import FIXTURES_DIR
from solin.widgets.meetings.tree_controller import (
    MeetingTreeController,
    _display_section_title,
)


def test_jwpub_scheduler_dispatches_interactive_before_pending_background() -> None:
    emitted = []
    background = publications_module._WeekLoadRequest(
        date(2026, 6, 1),
        False,
        "T",
        False,
        1,
        0,
        0,
    )
    interactive = publications_module._WeekLoadRequest(
        date(2026, 6, 8),
        False,
        "T",
        False,
        2,
        1,
        1,
    )
    background_key = ("2026-06-01", "T", False, 1)
    interactive_key = ("2026-06-08", "T", False, 2)
    service = SimpleNamespace(
        _active_load_key=None,
        _pending_loads={
            background_key: background,
            interactive_key: interactive,
        },
        _sig_load_week=SimpleNamespace(
            emit=lambda *args: emitted.append(args),
        ),
    )

    publications_module.JwpubService._dispatch_week_load(service)

    self_request = emitted[0]
    assert service._active_load_key == interactive_key
    assert self_request[0] == interactive.monday
    assert background_key in service._pending_loads


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
    def test_jwpub_svg_multimedia_is_ignored(self):
        rows = [
            multimedia_row(
                MultimediaId=1,
                MimeType="image/svg+xml; charset=utf-8",
                FilePath="internal-graphic",
            ),
            multimedia_row(
                MultimediaId=2,
                MimeType="image/png",
                FilePath="assets/INTERNAL.SVG?revision=1",
            ),
            multimedia_row(
                MultimediaId=3,
                MimeType="image/png",
                FilePath="meeting-image.png",
            ),
        ]

        filtered = dedup_multimedia_rows(rows)

        self.assertEqual([row["MultimediaId"] for row in filtered], [3])

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


class MeetingSectionTitleTests(unittest.TestCase):
    def test_generated_section_uses_its_structural_translation_key(self):
        node = {
            "type": "section",
            "title": "LIVING AS CHRISTIANS",
            "meeting_generated": True,
            "meeting_source_key": "section:mwb:lac",
        }

        with patch(
            "solin.widgets.meetings.tree_controller._translate_section_title",
            return_value="Nossa Vida Cristã",
        ) as translate:
            title = _display_section_title(node)

        self.assertEqual(title, "Nossa Vida Cristã")
        translate.assert_called_once_with("LIVING AS CHRISTIANS")

    def test_renamed_section_keeps_its_user_title(self):
        node = {
            "type": "section",
            "title": "Minha seção",
            "meeting_generated": True,
            "meeting_source_key": "section:mwb:lac",
            "user_title_override": True,
        }

        self.assertEqual(_display_section_title(node), "Minha seção")


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

    def test_canonical_resolution_wins_without_discarding_saved_user_state(self):
        canonical = self.canonical()
        saved = self.canonical()
        canonical_media = self.wt_section(canonical)["children"][0]
        saved_media = self.wt_section(saved)["children"][0]
        canonical_media["resolved_url"] = "https://cdn.example/fresh.mp4"
        canonical_media["thumbnail_url"] = "https://cdn.example/fresh.jpg"
        saved_media["resolved_url"] = "https://cdn.example/stale.mp4"
        saved_media["thumbnail_url"] = "https://cdn.example/stale.jpg"
        saved_media["start_trim_ticks"] = 10
        saved_media["image_framing"] = {"scale": 1.2}

        merged = MeetingTreeMerger(canonical).merge(saved)
        media_node = self.wt_section(merged)["children"][0]

        self.assertEqual(media_node["resolved_url"], canonical_media["resolved_url"])
        self.assertEqual(media_node["thumbnail_url"], canonical_media["thumbnail_url"])
        self.assertEqual(media_node["start_trim_ticks"], 10)
        self.assertEqual(media_node["image_framing"], {"scale": 1.2})

    def test_persisted_union_keeps_manual_nodes_from_local_and_portable_state(self):
        prepared = self.canonical()
        portable = self.canonical()
        self.wt_section(prepared)["children"].append(
            {
                "id": "local-manual",
                "type": "media",
                "title": "Local",
                "children": [],
                "meeting_generated": False,
            }
        )
        self.wt_section(portable)["children"].append(
            {
                "id": "portable-manual",
                "type": "media",
                "title": "Portable",
                "children": [],
                "meeting_generated": False,
            }
        )

        merged = merge_persisted_meeting_trees(prepared, portable)
        node_ids = {
            node["id"] for node in self.wt_section(merged)["children"]
        }

        self.assertIn("local-manual", node_ids)
        self.assertIn("portable-manual", node_ids)

    def test_persisted_union_does_not_duplicate_official_nodes_nested_locally(self):
        prepared = self.canonical()
        portable = self.canonical()
        official = copy.deepcopy(self.wt_section(prepared)["children"][0])
        prepared.append(
            {
                "id": "manual-section",
                "type": "section",
                "title": "Manual section",
                "children": [
                    official,
                    {
                        "id": "nested-manual",
                        "type": "media",
                        "title": "Nested",
                        "children": [],
                        "meeting_generated": False,
                    },
                ],
                "meeting_generated": False,
            }
        )

        merged = merge_persisted_meeting_trees(prepared, portable)
        source_key = official["meeting_source_key"]
        matching_official = [
            node
            for node in iter_nodes(merged)
            if node.get("meeting_source_key") == source_key
        ]

        self.assertEqual(len(matching_official), 1)
        self.assertTrue(
            any(node.get("id") == "nested-manual" for node in iter_nodes(merged))
        )

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
    class _FakeTimer:
        def __init__(self):
            self.started: list[int] = []
            self.stopped = 0

        def start(self, delay_ms=0):
            self.started.append(delay_ms)

        def stop(self):
            self.stopped += 1

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

    def test_local_image_thumb_source_uses_file_until_generated_thumb_exists(self):
        class FakeController:
            pass

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "photo.jpg"
            source.write_bytes(b"image")
            node = {
                "id": "image-node",
                "type": "media",
                "media_type": "image",
                "media_ref": {"file_path": str(source)},
            }
            controller = FakeController()
            controller._resolved_urls = {}
            controller._thumb_source_for = lambda _item_id: ""
            controller._media_type_from_ref = lambda _ref: "image"
            controller._url_for_node = (
                lambda target: MeetingTreeController._url_for_node(controller, target)
            )

            source_url = MeetingTreeController._display_thumb_source_for(
                controller,
                node,
                "image-node",
            )

            self.assertTrue(source_url.startswith("file:///"))
            self.assertTrue(source_url.endswith("photo.jpg"))

    def test_generated_thumb_source_wins_over_local_image_file_source(self):
        class FakeController:
            pass

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "photo.jpg"
            source.write_bytes(b"image")
            node = {
                "id": "image-node",
                "type": "media",
                "media_type": "image",
                "media_ref": {"file_path": str(source)},
            }
            controller = FakeController()
            controller._thumb_source_for = (
                lambda _item_id: "image://playlistthumbs/image-node/1"
            )

            self.assertEqual(
                MeetingTreeController._display_thumb_source_for(
                    controller,
                    node,
                    "image-node",
                ),
                "image://playlistthumbs/image-node/1",
            )

    def test_local_image_thumbnail_retry_when_file_is_not_decodable_yet(self):
        class FakeController:
            pass

        class NullPixmap:
            def __init__(self, _path):
                pass

            def isNull(self):
                return True

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "photo.jpg"
            source.write_bytes(b"not-ready-yet")
            node = {
                "id": "image-node",
                "type": "media",
                "media_type": "image",
                "media_ref": {
                    "file_path": str(source),
                    "mime_type": "image/jpeg",
                },
            }
            controller = FakeController()
            controller._resolved_urls = {}
            controller._thumb_cache = {}
            controller._thumb_versions = {}
            controller._local_image_thumb_retry_due = {}
            controller._local_image_thumb_retry_attempts = {}
            controller._local_image_thumb_retry_timer = self._FakeTimer()
            controller._schedule_local_image_thumb_retry = (
                lambda item_id: MeetingTreeController._schedule_local_image_thumb_retry(
                    controller,
                    item_id,
                )
            )
            controller._arm_local_image_thumb_retry_timer = (
                lambda: MeetingTreeController._arm_local_image_thumb_retry_timer(
                    controller
                )
            )
            controller._url_for_node = (
                lambda target: MeetingTreeController._url_for_node(controller, target)
            )
            controller._has_local_thumbnail = lambda _node: False
            controller._duration_ticks = lambda _node: 0
            controller._media_type_from_ref = lambda _ref: "image"
            controller._save_thumbnail_for_node = (
                lambda *_args: self.fail("thumbnail should not be saved")
            )
            controller._save = lambda: self.fail("tree should not be saved")
            controller._emit_media_changed = (
                lambda _item_id: self.fail("media should not be emitted")
            )

            with patch("solin.widgets.meetings.tree_controller.QPixmap", NullPixmap):
                MeetingTreeController._start_media_request(controller, node)

            self.assertEqual(
                controller._local_image_thumb_retry_attempts,
                {"image-node": 1},
            )
            self.assertIn("image-node", controller._local_image_thumb_retry_due)
            self.assertTrue(controller._local_image_thumb_retry_timer.started)

    def test_local_image_thumbnail_retry_recovers_and_notifies_qml(self):
        class FakeController:
            pass

        class ReadyPixmap:
            def __init__(self, _path):
                pass

            def isNull(self):
                return False

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "photo.jpg"
            source.write_bytes(b"ready")
            node = {
                "id": "image-node",
                "type": "media",
                "media_type": "image",
                "media_ref": {
                    "file_path": str(source),
                    "mime_type": "image/jpeg",
                },
            }
            controller = FakeController()
            controller._nodes = [node]
            controller._resolved_urls = {}
            controller._thumb_cache = {}
            controller._thumb_versions = {}
            controller._local_image_thumb_retry_due = {"image-node": 0.0}
            controller._local_image_thumb_retry_attempts = {"image-node": 1}
            controller._local_image_thumb_retry_timer = self._FakeTimer()
            saved: list[str] = []
            emitted: list[str] = []
            controller._start_media_request = (
                lambda target: MeetingTreeController._start_media_request(
                    controller,
                    target,
                )
            )
            controller._clear_local_image_thumb_retry = (
                lambda item_id: MeetingTreeController._clear_local_image_thumb_retry(
                    controller,
                    item_id,
                )
            )
            controller._arm_local_image_thumb_retry_timer = (
                lambda: MeetingTreeController._arm_local_image_thumb_retry_timer(
                    controller
                )
            )
            controller._find_node = (
                lambda item_id, nodes=None: MeetingTreeController._find_node(
                    controller,
                    item_id,
                    nodes,
                )
            )
            controller._url_for_node = (
                lambda target: MeetingTreeController._url_for_node(controller, target)
            )
            controller._has_local_thumbnail = lambda _node: False
            controller._duration_ticks = lambda _node: 0
            controller._media_type_from_ref = lambda _ref: "image"
            controller._save_thumbnail_for_node = (
                lambda target, _pixmap: target.__setitem__(
                    "thumbnail_local_path",
                    str(Path(tmp) / "thumb.jpg"),
                )
                or saved.append(str(target["id"]))
                or str(Path(tmp) / "thumb.jpg")
            )
            controller._save = lambda: saved.append("tree")
            controller._emit_media_changed = lambda item_id: emitted.append(item_id)

            with patch("solin.widgets.meetings.tree_controller.QPixmap", ReadyPixmap):
                MeetingTreeController._drain_local_image_thumb_retries(controller)

            self.assertEqual(emitted, ["image-node"])
            self.assertIn("image-node", saved)
            self.assertIn("tree", saved)
            self.assertEqual(controller._local_image_thumb_retry_due, {})
            self.assertEqual(controller._local_image_thumb_retry_attempts, {})


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
        controller._tree_data_cache = [{"id": "stale"}]
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

        self.assertEqual(
            result,
            {
                "items": [
                    {
                        "id": "media",
                        "url": "",
                        "key_symbol": "",
                        "track": 0,
                        "issue_tag": 0,
                        "doc_id": 0,
                        "meps_language": 0,
                        "language": "",
                        "jw_media_id": "",
                    }
                ],
                "sections": [],
            },
        )

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
        self.assertIsNone(controller._tree_data_cache)

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
        self.assertEqual(controller._tree_data_cache, [{"id": "stale"}])

    def test_media_requests_are_queued_for_batched_drain(self):
        class FakeTimer:
            def __init__(self):
                self.started = 0
                self.stopped = 0
                self.active = False

            def isActive(self):
                return self.active

            def start(self):
                self.started += 1
                self.active = True

            def stop(self):
                self.stopped += 1
                self.active = False

        controller = self.controller([
            {"id": "media-1", "type": "media", "children": []},
            {
                "id": "section",
                "type": "section",
                "children": [
                    {"id": "media-2", "type": "media", "children": []},
                ],
            },
        ])
        requested = []
        controller._media_request_queue = deque()
        controller._media_request_timer = FakeTimer()
        controller._start_media_request = lambda node: requested.append(node["id"])

        MeetingTreeController._start_media_requests(controller)

        self.assertEqual(requested, [])
        self.assertEqual(
            [node["id"] for node in controller._media_request_queue],
            ["media-1", "media-2"],
        )
        self.assertEqual(controller._media_request_timer.started, 1)

        MeetingTreeController._drain_media_request_queue(controller)

        self.assertEqual(requested, ["media-1", "media-2"])
        self.assertEqual(controller._media_request_queue, deque())
        self.assertEqual(controller._media_request_timer.stopped, 1)


class MeetingTreeControllerStorageFeedbackTests(unittest.TestCase):
    class _Signal:
        def __init__(self):
            self.calls = []

        def emit(self, *args):
            self.calls.append(args)

    def test_local_cache_save_failure_reports_without_raising(self):
        class FakeStore:
            def save(self, *_args, **_kwargs):
                raise OSError(28, "No space left on device")

        class FakeController:
            pass

        controller = FakeController()
        controller._tree_key = "mwb:2026-05-25:T:20260500"
        controller._nodes = [{"id": "n1", "type": "section", "children": []}]
        controller._canonical_hash = "hash"
        controller._deleted_source_keys = set()
        controller._linked_folder_files = {}
        controller._meeting_folder_imports = {}
        controller._store = FakeStore()
        controller._current_overview = lambda: None
        controller.storageSaveFailed = self._Signal()

        saved = MeetingTreeController._save_local_cache(controller)

        self.assertFalse(saved)
        self.assertEqual(
            controller.storageSaveFailed.calls,
            [("mwb:2026-05-25:T:20260500", "[Errno 28] No space left on device")],
        )

    def test_local_cache_save_success_reports_saved_tree_key(self):
        class FakeStore:
            def __init__(self):
                self.calls = []

            def save(self, *args, **kwargs):
                self.calls.append((args, kwargs))

        class FakeController:
            pass

        store = FakeStore()
        controller = FakeController()
        controller._tree_key = "mwb:2026-05-25:T:20260500"
        controller._nodes = [{"id": "n1", "type": "section", "children": []}]
        controller._canonical_hash = "hash"
        controller._deleted_source_keys = set()
        controller._linked_folder_files = {}
        controller._meeting_folder_imports = {}
        controller._store = store
        controller._current_overview = lambda: None
        controller.storageSaved = self._Signal()
        controller.storageSaveFailed = self._Signal()

        saved = MeetingTreeController._save_local_cache(controller)

        self.assertTrue(saved)
        self.assertEqual(controller.storageSaved.calls, [(controller._tree_key,)])
        self.assertEqual(controller.storageSaveFailed.calls, [])
        self.assertEqual(len(store.calls), 1)


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
            nodes = [{
                "id": "n1",
                "type": "media",
                "children": [],
                "image_framing": {
                    "version": 1,
                    "zoom": 1.4,
                    "norm_x": 0.12,
                    "norm_y": -0.08,
                },
            }]
            store.save("mwb:2026-05-25:T:20260500", nodes, "hash")
            loaded, digest = store.load("mwb:2026-05-25:T:20260500")
            self.assertEqual(loaded, nodes)
            self.assertEqual(digest, "hash")

    def test_find_snapshot_returns_persisted_overview(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = MeetingTreeStore(Path(tmp) / "meeting_trees.json")
            nodes = [{"id": "media", "type": "media", "children": []}]

            store.save(
                "mwb:2026-05-25:T:20260500",
                nodes,
                "hash",
                overview=MeetingTreeOverview(
                    title="May 25-31",
                    media_count=1,
                    cover_bytes=b"cover",
                ),
            )

            snapshot = store.find_snapshot("mwb", date(2026, 5, 25), "T")

            self.assertIsNotNone(snapshot)
            assert snapshot is not None
            self.assertEqual(snapshot.tree_key, "mwb:2026-05-25:T:20260500")
            self.assertEqual(snapshot.overview.title, "May 25-31")
            self.assertEqual(snapshot.overview.media_count, 1)
            self.assertEqual(snapshot.overview.cover_bytes, b"cover")
            self.assertEqual(snapshot.media_count, 1)

    def test_find_snapshot_supports_legacy_tree_without_overview(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = MeetingTreeStore(Path(tmp) / "meeting_trees.json")
            nodes = [{"id": "media", "type": "media", "children": []}]

            store.save("mwb:2026-05-25:T:20260500", nodes, "hash")

            snapshot = store.find_snapshot("mwb", date(2026, 5, 25), "T")

            self.assertIsNotNone(snapshot)
            assert snapshot is not None
            self.assertEqual(snapshot.overview.title, "")
            self.assertEqual(snapshot.overview.media_count, 1)

    def test_find_snapshot_is_language_scoped_and_chooses_newest_issue(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = MeetingTreeStore(Path(tmp) / "meeting_trees.json")
            older = [{"id": "older", "type": "media", "children": []}]
            newer = [{"id": "newer", "type": "media", "children": []}]

            store.save("wt:2026-05-25:T:20260400", older, "old")
            store.save("wt:2026-05-25:T:20260500", newer, "new")

            snapshot = store.find_snapshot("wt", date(2026, 5, 25), "T")

            self.assertIsNotNone(snapshot)
            assert snapshot is not None
            self.assertEqual(snapshot.tree_key, "wt:2026-05-25:T:20260500")
            self.assertEqual(snapshot.nodes, newer)
            self.assertIsNone(store.find_snapshot("wt", date(2026, 5, 25), "E"))

    def test_find_snapshot_accepts_structural_tree_without_media(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = MeetingTreeStore(Path(tmp) / "meeting_trees.json")
            store.save(
                "wt:2026-05-25:T:20260500",
                [{"id": "section", "type": "section", "children": []}],
                "hash",
                overview=MeetingTreeOverview(title="Study", media_count=0),
            )

            snapshot = store.find_snapshot("wt", date(2026, 5, 25), "T")

            self.assertIsNotNone(snapshot)
            assert snapshot is not None
            self.assertEqual(snapshot.media_count, 0)
            self.assertEqual(snapshot.overview.title, "Study")

    def test_store_revisions_increment_and_migrate_v1_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "meeting_trees.json"
            tree_key = "mwb:2026-05-25:T:20260500"
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "trees": {
                            tree_key: {
                                "nodes": [],
                                "last_canonical_hash": "old",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            store = MeetingTreeStore(path)

            migrated = store.find_snapshot("mwb", date(2026, 5, 25), "T")
            assert migrated is not None
            self.assertEqual(migrated.revision, 0)
            saved = store.save(tree_key, [], "new")

            self.assertEqual(saved.revision, 1)
            raw = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(raw["version"], 2)
            self.assertEqual(raw["trees"][tree_key]["revision"], 1)

    def test_spoken_and_sign_variants_have_distinct_persisted_identities(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = MeetingTreeStore(Path(tmp) / "meeting_trees.json")
            monday = date(2026, 5, 25)
            spoken_key = make_meeting_tree_key(
                "mwb",
                monday,
                "T",
                "20260500",
            )
            sign_key = make_meeting_tree_key(
                "mwb",
                monday,
                "T",
                "20260500",
                is_sign_language=True,
            )
            store.save(spoken_key, [], "spoken")
            store.save(sign_key, [], "sign")

            spoken = store.find_snapshot("mwb", monday, "T", False)
            sign = store.find_snapshot("mwb", monday, "T", True)

            assert spoken is not None
            assert sign is not None
            self.assertEqual(spoken.tree_key, spoken_key)
            self.assertEqual(sign.tree_key, sign_key)
            self.assertFalse(spoken.is_sign_language)
            self.assertTrue(sign.is_sign_language)

    def test_corrupt_store_write_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "meeting_trees.json"
            path.write_text("{broken", encoding="utf-8")
            store = MeetingTreeStore(path)

            with self.assertRaises(json.JSONDecodeError):
                store.save("mwb:2026-05-25:T:20260500", [], "hash")

            self.assertEqual(path.read_text(encoding="utf-8"), "{broken")

    def test_resolution_patch_preserves_user_title_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = MeetingTreeStore(Path(tmp) / "meeting_trees.json")
            tree_key = "mwb:2026-05-25:T:20260500"
            node = {
                "id": "official",
                "type": "media",
                "title": "My title",
                "user_title_override": True,
                "auto_title": False,
                "children": [],
                "media_ref": {
                    "key_symbol": "mwbv",
                    "track": 1,
                    "label": "My title",
                },
            }
            store.save(tree_key, [node], "hash")

            snapshot = store.patch_media_batch(
                tree_key,
                {
                    "official": (
                        media_identity_signature(node),
                        {
                            "resolved_url": "https://cdn.example/fresh.mp4",
                            "title": "JW title",
                            "auto_title": False,
                            "media_ref_label": "JW title",
                        },
                    )
                },
            )

            assert snapshot is not None
            updated = snapshot.nodes[0]
            self.assertEqual(updated["resolved_url"], "https://cdn.example/fresh.mp4")
            self.assertEqual(updated["title"], "My title")
            self.assertEqual(updated["media_ref"]["label"], "My title")

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


    def test_external_items_use_normal_target_and_insert_path_in_order(self):
        captured = []

        class FakeController:
            _parse_list_id = staticmethod(lambda list_id: (list_id, ""))
            _children_for_target = staticmethod(lambda *_target: [])
            _media_identity_records = staticmethod(lambda: [])
            _node_from_playlist_item = staticmethod(
                lambda item, title: {"title": title, "url": item["url"]}
            )
            _insert_nodes = staticmethod(
                lambda list_id, index, nodes: (
                    captured.append((list_id, index, nodes)) or True
                )
            )

        added = MeetingTreeController.add_external_media_items(
            FakeController(),
            [
                {"title": "First", "url": "first.mp4"},
                {"title": "Second", "url": "second.mp4"},
                {"title": "Missing", "url": ""},
            ],
            list_id="section:talk",
            insert_index=3,
        )

        self.assertEqual(added.added_count, 2)
        self.assertEqual(captured[0][0:2], ("section:talk", 3))
        self.assertEqual(
            [node["title"] for node in captured[0][2]],
            ["First", "Second"],
        )

    def test_jw_duplicate_is_blocked_across_nested_meeting_tree(self):
        class FakeController:
            pass

        existing_ref = {
            "file_path": "https://akamd1.jw-cdn.org/x/sjjm_T_002_r480P.mp4",
            "key_symbol": "sjjm",
            "track": 2,
            "meps_language": 5,
        }
        controller = FakeController()
        controller._nodes = [
            {
                "id": "section",
                "type": "section",
                "children": [
                    {
                        "id": "existing",
                        "type": "media",
                        "media_ref": existing_ref,
                        "children": [],
                    }
                ],
            }
        ]
        controller._media_identity_records = lambda: (
            MeetingTreeController._media_identity_records(controller)
        )
        controller._meeting_thumbnail_store = SimpleNamespace(
            copy_from=lambda *_args: self.fail("duplicate must not copy thumbnail")
        )
        controller._insert_nodes = lambda *_args: self.fail(
            "duplicate must not mutate the tree"
        )

        result = MeetingTreeController.add_from_jw_catalog(
            controller,
            {
                "title": "2. Song",
                "download_url": "https://akamd1.jw-cdn.org/y/sjjm_T_002_r720P.mp4",
                "pub": "sjjm",
                "track": 2,
                "meps_language": 5,
            },
            "section:other",
            0,
        )

        self.assertEqual(result.added_count, 0)
        self.assertEqual(result.duplicate_count, 1)
        self.assertEqual(controller._nodes[0]["children"][0]["media_ref"], existing_ref)

    def test_external_batch_adds_only_unique_items_in_order(self):
        captured = []

        class FakeController:
            _parse_list_id = staticmethod(lambda list_id: (list_id, ""))
            _children_for_target = staticmethod(lambda *_target: [])
            _media_identity_records = staticmethod(
                lambda: [{"file_path": "existing.mp4"}]
            )
            _node_from_playlist_item = staticmethod(
                lambda item, title: {"title": title, "url": item["url"]}
            )
            _insert_nodes = staticmethod(
                lambda list_id, index, nodes: (
                    captured.append((list_id, index, nodes)) or True
                )
            )

        result = MeetingTreeController.add_external_media_items(
            FakeController(),
            [
                {"id": "old", "title": "Old", "url": "existing.mp4"},
                {"id": "first", "title": "First", "url": "first.mp4"},
                {"id": "repeat", "title": "Repeat", "url": "first.mp4"},
                {"id": "second", "title": "Second", "url": "second.mp4"},
            ],
            list_id="root",
            insert_index=0,
        )

        self.assertEqual(result.added_count, 2)
        self.assertEqual(result.duplicate_count, 2)
        self.assertEqual(
            [node["title"] for node in captured[0][2]],
            ["First", "Second"],
        )

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
