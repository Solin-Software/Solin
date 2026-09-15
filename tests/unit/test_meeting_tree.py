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
from types import SimpleNamespace
from unittest.mock import patch

from PySide6.QtWidgets import QDialog, QMessageBox

import solin.core.meetings.tree_store as tree_store_module
import solin.core.meetings.memorial as memorial_module
import solin.core.meetings.publications as publications_module
from solin.core.ingest.watched_folder_files import WatchedFolderFileStore
from solin.core.meetings.meeting_folder_imports import (
    find_meeting_folder_import_record,
)
from solin.core.meetings.linked_folder_sync import MeetingLinkedFolderSync
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
from solin.core.meetings.tree_types import iter_nodes, publication_subsection_key
from solin.core.media.thumbnail_identity import thumbnail_source_fingerprint
from solin.core.i18n.meeting_sections import display_meeting_section_title
from solin.ui.qml.media_tree.media_presenter import MediaRoleInput, media_roles
from solin.ui.qml.media_tree.state import MediaAvailability, MediaPresentationState
from tests._jwpub_fixture import build_synthetic_jwpub
from solin.widgets.meetings.tree_controller import (
    MeetingTreeController,
)


def test_memorial_service_stays_alive_until_slow_thread_finishes() -> None:
    callbacks = []

    class _FinishedSignal:
        def connect(self, callback) -> None:
            callbacks.append(callback)

    class _Thread:
        finished = _FinishedSignal()

        @staticmethod
        def isRunning() -> bool:
            return True

        @staticmethod
        def quit() -> None:
            pass

        @staticmethod
        def wait(_wait_ms: int) -> None:
            pass

    class _Service:
        _thread = _Thread()

        def __init__(self) -> None:
            self.parent = object()
            self.deleted = False

        def setParent(self, parent) -> None:
            self.parent = parent

        def deleteLater(self) -> None:
            self.deleted = True

    service = _Service()

    memorial_module.MemorialService.shutdown(
        service,
        wait_ms=1,
        delete_when_stopped=True,
    )

    assert service in memorial_module._STOPPING_MEMORIAL_SERVICES
    assert service.parent is None
    assert service.deleted is False

    callbacks[0]()

    assert service not in memorial_module._STOPPING_MEMORIAL_SERVICES
    assert service.deleted is True


def test_media_info_request_replaces_pending_work_when_source_changes() -> None:
    class _Queue:
        def __init__(self) -> None:
            self.invalidated = []
            self.requests = []

        def invalidate(self, token) -> None:
            self.invalidated.append(token)

        def request(self, *args, **kwargs) -> None:
            self.requests.append((args, kwargs))

    queue = _Queue()
    controller = SimpleNamespace(
        _info_queue=queue,
        _info_request_by_token={},
        _active_info_requests=set(),
        _next_token=1,
        _media_info_source_identity=(MeetingTreeController._media_info_source_identity),
    )

    MeetingTreeController._queue_info(
        controller,
        "media-1",
        "C:/media/old.mp4",
        "video",
    )
    MeetingTreeController._queue_info(
        controller,
        "media-1",
        "C:/media/new.mp4",
        "video",
    )

    assert queue.invalidated == [1]
    assert [args[1] for args, _kwargs in queue.requests] == [
        "C:/media/old.mp4",
        "C:/media/new.mp4",
    ]
    assert set(controller._info_request_by_token) == {2}


def test_media_info_request_replaces_same_path_when_source_revision_changes() -> None:
    class _Queue:
        def __init__(self) -> None:
            self.invalidated = []
            self.requests = []

        def invalidate(self, token) -> None:
            self.invalidated.append(token)

        def request(self, *args, **kwargs) -> None:
            self.requests.append((args, kwargs))

    queue = _Queue()
    controller = SimpleNamespace(
        _info_queue=queue,
        _info_request_by_token={},
        _active_info_requests=set(),
        _next_token=1,
        _media_info_source_identity=(MeetingTreeController._media_info_source_identity),
    )

    MeetingTreeController._queue_info(
        controller,
        "media-1",
        "C:/media/video.mp4",
        "video",
        source_signature="100:1",
    )
    MeetingTreeController._queue_info(
        controller,
        "media-1",
        "C:/media/video.mp4",
        "video",
        source_signature="100:2",
    )

    assert queue.invalidated == [1]
    assert set(controller._info_request_by_token) == {2}


def test_removed_meeting_item_cancels_active_media_work() -> None:
    invalidated = []
    removed_key = ("removed", "metadata", "source-a", "100:1")
    kept_key = ("kept", "thumb", "source-b", "")
    controller = SimpleNamespace(
        _info_request_by_token={3: removed_key, 4: kept_key},
        _active_info_requests={removed_key, kept_key},
        _info_queue=SimpleNamespace(invalidate=invalidated.append),
    )

    MeetingTreeController._cancel_media_info_requests_for_item(
        controller,
        "removed",
    )

    assert invalidated == [3]
    assert controller._info_request_by_token == {4: kept_key}
    assert controller._active_info_requests == {kept_key}


def test_meeting_source_change_failure_reprobes_before_retry() -> None:
    request_key = ("media-1", "metadata", "source-a", "100:1")
    requested = []
    controller = SimpleNamespace(
        _info_request_by_token={3: request_key},
        _active_info_requests={request_key},
        _tree_session=SimpleNamespace(request_nodes=requested.append),
    )

    MeetingTreeController._on_info_failed(
        controller,
        3,
        SimpleNamespace(code="source-changed"),
    )

    assert controller._info_request_by_token == {}
    assert controller._active_info_requests == set()
    assert requested == [{"media-1"}]


def test_jwpub_scheduler_dispatches_interactive_before_pending_background() -> None:
    emitted = []
    background = publications_module._WeekLoadRequest(
        date(2026, 6, 1),
        False,
        "T",
        False,
        1,
        0,
        frozenset({"mwb", "wt"}),
        "",
        {},
        0,
    )
    interactive = publications_module._WeekLoadRequest(
        date(2026, 6, 8),
        False,
        "T",
        False,
        2,
        1,
        frozenset({"mwb", "wt"}),
        "",
        {},
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
            "solin.core.i18n.meeting_sections.translate_meeting_section_title",
            return_value="Nossa Vida Cristã",
        ) as translate:
            title = display_meeting_section_title(node)

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

        self.assertEqual(display_meeting_section_title(node), "Minha seção")


class MeetingTreeBuilderTests(unittest.TestCase):
    def test_meeting_models_are_not_reexported_by_service_modules(self):
        self.assertFalse(hasattr(publications_module, "MeetingMedia"))
        self.assertFalse(hasattr(publications_module, "MeetingPublicationRef"))
        self.assertFalse(hasattr(publications_module, "WeekData"))
        self.assertFalse(hasattr(memorial_module, "MeetingMedia"))
        self.assertFalse(hasattr(memorial_module, "MemorialData"))

    def test_builder_keeps_canonical_section_titles_presentation_neutral(self):
        builder = MeetingTreeBuilder(
            media_fallback_title=lambda: "Translated media",
        )
        tree = builder.build_weekend(WeekData(wt_all_media=[media(label="", caption="")]))

        self.assertEqual(tree[0]["title"], "PUBLIC TALK")
        self.assertEqual(tree[1]["title"], "Watchtower Study")
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

        self.assertEqual(
            [node["meeting_source_key"] for node in tree],
            [
                "section:mwb:tgw",
                "section:mwb:ayfm",
                "section:mwb:lac",
            ],
        )
        tgw = tree[0]
        ext = next(node for node in tgw["children"] if node["type"] == "subsection")
        self.assertEqual(ext["title"], "Synthetic Reference Manual (cl)")
        self.assertTrue(ext["collapsed"])
        self.assertIn("Synthetic reference topic", ext["children"][0]["text"])
        lac = tree[2]
        placeholder = next(
            node for node in lac["children"] if node["type"] == "media" and node["title"] == "Media"
        )
        self.assertTrue(placeholder["auto_title"])
        cbs = next(node for node in lac["children"] if node["type"] == "subsection")
        self.assertEqual(cbs["meeting_source_key"], publication_subsection_key(
            "subsection:cbs:lfb", "Synthetic Study Guide (lfb)",
        ))
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
                (
                    1,
                    "studyguide",
                    0,
                    "Book",
                    "Synthetic Study Guide",
                    None,
                    None,
                    None,
                    None,
                ),
                (
                    2,
                    "postfinal",
                    0,
                    "Book",
                    "Post-song reference",
                    None,
                    None,
                    None,
                    None,
                ),
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
        return next(node for node in tree if node.get("meeting_source_key") == "section:wt:wt")

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
        saved = [
            {
                "id": "old-section",
                "type": "section",
                "title": "Old section",
                "children": [
                    {
                        "id": "manual",
                        "type": "media",
                        "title": "Manual",
                        "children": [],
                        "meeting_generated": False,
                    }
                ],
                "meeting_generated": True,
                "meeting_source_key": "section:mwb:gone",
            }
        ]
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
        node_ids = {node["id"] for node in self.wt_section(merged)["children"]}

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
            node for node in iter_nodes(merged) if node.get("meeting_source_key") == source_key
        ]

        self.assertEqual(len(matching_official), 1)
        self.assertTrue(any(node.get("id") == "nested-manual" for node in iter_nodes(merged)))

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

    def test_partial_resolution_preserves_duration_and_does_not_reenter_resolver(self):
        class FakeController:
            pass

        node = {
            "id": "video-node",
            "type": "media",
            "media_type": "video",
            "resolved_url": "https://cdn.example/old.mp4",
            "base_duration_ticks": 123_000_000,
            "media_ref": {
                "key_symbol": "mwbv",
                "track": 2,
                "mime_type": "video/mp4",
            },
        }
        controller = FakeController()
        controller._resolve_to_node_id = {"request": "video-node"}
        controller._resolve_to_identity = {}
        controller._resolved_urls = {}
        controller._find_node = lambda _item_id: node
        controller._save = lambda: True
        controller._emit_media_changed = lambda _item_id: None
        controller._emit_cloud_for_node = lambda _item_id: None
        media_starts: list[bool] = []
        controller._start_media_requests = lambda _nodes, *, resolve_jw_metadata=True: (
            media_starts.append(resolve_jw_metadata)
        )

        MeetingTreeController._on_media_resolved(
            controller,
            "request",
            {"url": "https://cdn.example/new.mp4"},
        )

        self.assertEqual(node["resolved_url"], "https://cdn.example/new.mp4")
        self.assertEqual(node["base_duration_ticks"], 123_000_000)
        self.assertEqual(media_starts, [False])

    def test_canonical_image_is_not_sent_to_publication_media_resolver(self):
        node = {
            "id": "image-node",
            "type": "media",
            "media_type": "image",
            "meeting_generated": True,
            "media_ref": {
                "key_symbol": "mwb",
                "track": 2,
                "mime_type": "image/jpeg",
            },
        }

        self.assertFalse(MeetingTreeController._node_needs_jw_metadata(object(), node))

    def test_jwpub_file_path_is_deferred_to_the_async_probe(self):
        class FakeController:
            pass

        controller = FakeController()
        controller._resolved_urls = {}
        controller._url_for_node = lambda node: MeetingTreeController._url_for_node(
            controller, node
        )
        requested: list[set[str]] = []
        controller._tree_session = SimpleNamespace(request_nodes=requested.append)

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

        self.assertEqual(
            MeetingTreeController._url_for_node(controller, node),
            "w_LGP_202604_02_r720P.mp4",
        )

        MeetingTreeController._start_media_requests(controller, [node])

        self.assertEqual(requested, [{"video-node"}])

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
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "photo.jpg"
            source.write_bytes(b"image")
            roles = media_roles(
                MediaRoleInput("image-node", "Photo", "image", "Image", str(source)),
                MediaPresentationState(
                    availability=MediaAvailability.AVAILABLE,
                    local_path=str(source),
                ),
                None,
            )

            source_url = str(roles["thumbSource"])
            self.assertTrue(source_url.startswith("file:///"))
            self.assertTrue(source_url.endswith("photo.jpg"))

    def test_generated_thumb_source_wins_over_local_image_file_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "photo.jpg"
            source.write_bytes(b"image")
            roles = media_roles(
                MediaRoleInput("image-node", "Photo", "image", "Image", str(source)),
                MediaPresentationState(
                    availability=MediaAvailability.AVAILABLE,
                    local_path=str(source),
                    thumbnail_source="image://playlistthumbs/image-node/1",
                ),
                None,
            )

            self.assertEqual(
                roles["thumbSource"],
                "image://playlistthumbs/image-node/1",
            )

    def test_local_image_probe_is_scheduled_without_synchronous_metadata_work(self):
        class FakeController:
            pass

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
            controller._url_for_node = lambda _node: str(source)
            requested: list[set[str]] = []
            controller._tree_session = SimpleNamespace(request_nodes=requested.append)
            controller._queue_info = lambda *_args, **_kwargs: self.fail(
                "media discovery must stay in the asynchronous probe"
            )

            MeetingTreeController._start_media_requests(controller, [node])

            self.assertEqual(requested, [{"image-node"}])

    def test_remote_uncached_media_is_scheduled_through_the_async_probe(self):
        class FakeController:
            pass

        node = {
            "id": "video-node",
            "type": "media",
            "media_type": "video",
            "resolved_url": "https://cdn.example/video.mp4",
            "media_ref": {"mime_type": "video/mp4"},
        }
        controller = FakeController()
        controller._url_for_node = lambda target: target["resolved_url"]
        requested: list[set[str]] = []
        controller._tree_session = SimpleNamespace(request_nodes=requested.append)
        controller._queue_info = lambda *_args, **_kwargs: self.fail(
            "uncached remote media must remain behind the cloud action"
        )

        MeetingTreeController._start_media_requests(controller, [node])

        self.assertEqual(requested, [{"video-node"}])

    def test_remote_uncached_media_fetches_known_thumbnail_after_probe(self):
        class FakeController:
            pass

        thumbnail_url = "https://cdn.example/video.jpg"
        node = {
            "id": "video-node",
            "type": "media",
            "media_type": "video",
            "resolved_url": "https://cdn.example/video.mp4",
            "thumbnail_url": thumbnail_url,
            "media_ref": {"mime_type": "video/mp4"},
        }
        controller = FakeController()
        controller._nodes = [node]
        controller._find_node = lambda item_id: node if item_id == "video-node" else None
        controller._duration_ticks = lambda _node: 123_000_000
        controller._media_type_from_ref = lambda _ref: "video"
        controller._tree_session = SimpleNamespace(owner_id="meeting:test")
        controller._media_tree_runtime = SimpleNamespace(
            registry=SimpleNamespace(
                state=lambda _owner, _item: MediaPresentationState(
                    availability=MediaAvailability.AVAILABLE,
                )
            )
        )
        controller._request_jw_resolution = lambda *_args, **_kwargs: self.fail(
            "available remote media must not be resolved again"
        )
        requests: list[tuple[tuple, dict]] = []
        controller._queue_info = lambda *args, **kwargs: requests.append((args, kwargs))

        MeetingTreeController._on_presentation_state_changed(
            controller,
            "meeting:test",
            "video-node",
        )

        self.assertEqual(
            requests,
            [
                (
                    ("video-node", thumbnail_url, "image"),
                    {
                        "purpose": "thumb",
                        "source_signature": thumbnail_source_fingerprint(thumbnail_url),
                    },
                )
            ],
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
        controller.stateChanged = self._Signal()
        controller._tree_session = SimpleNamespace(
            tree_id="meeting:test",
            structure_revision=1,
        )
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
        controller = self.controller(
            [
                {"id": "media", "type": "media", "children": []},
            ]
        )

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

    def test_add_to_destination_emits_a_playlist_shaped_copy(self):
        node = {
            "id": "meeting-media",
            "type": "media",
            "title": "Meeting video",
            "media_type": "video",
            "start_trim_ticks": 20_000,
            "image_framing": {"zoom": 1.1, "norm_x": 0.0, "norm_y": 0.0},
            "thumbnail_url": "https://cdn.example/thumb.jpg",
            "media_ref": {
                "file_path": "C:/meeting/video.mp4",
                "mime_type": "video/mp4",
                "key_symbol": "mwbv",
                "track": 4,
                "issue_tag": 20260900,
                "meps_doc_id": 1234,
                "meps_language": 5,
                "language": "T",
                "jw_media_id": "jw-media-1",
            },
            "children": [],
        }
        controller = self.controller([node])
        controller._tree_session.owner_id = "meeting:2026-09-07:mwb"
        controller._media_tree_runtime = SimpleNamespace(
            registry=SimpleNamespace(
                state=lambda _owner, _node: MediaPresentationState(
                    availability=MediaAvailability.AVAILABLE,
                )
            )
        )
        controller._resolved_urls = {}
        controller._find_node = lambda item_id: node if item_id == node["id"] else None
        controller._url_for_node = lambda current: MeetingTreeController._url_for_node(
            controller, current
        )
        controller._flush_image_framing_save = lambda: True
        controller.addToDestinationRequested = self._Signal()

        MeetingTreeController.addToDestination(controller, "meeting-media")

        [(request,)] = controller.addToDestinationRequested.calls
        self.assertEqual(request.title, "Meeting video")
        self.assertFalse(request.can_play)
        [asset] = request.assets
        self.assertEqual(asset.source_id, "C:/meeting/video.mp4")
        self.assertNotEqual(asset.item["id"], "meeting-media")
        self.assertEqual(asset.item["doc_id"], 1234)
        self.assertEqual(asset.item["start_trim_ticks"], 20_000)
        self.assertEqual(asset.item["image_framing"], node["image_framing"])
        self.assertEqual(asset.item["thumbnail_url"], node["thumbnail_url"])

    def test_set_as_idle_emits_an_existing_local_meeting_item(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "idle.mp4"
            thumbnail = Path(tmp) / "idle.jpg"
            path.write_bytes(b"video")
            thumbnail.write_bytes(b"thumbnail")
            node = {
                "id": "meeting-media",
                "type": "media",
                "title": "Idle video",
                "media_type": "video",
                "media_ref": {
                    "file_path": str(path),
                    "mime_type": "video/mp4",
                },
                "children": [],
            }
            controller = self.controller([node])
            controller._tree_session.owner_id = "meeting:2026-09-07:mwb"
            controller._media_tree_runtime = SimpleNamespace(
                registry=SimpleNamespace(
                    state=lambda _owner, _node: MediaPresentationState(
                        availability=MediaAvailability.AVAILABLE,
                        local_path=str(path),
                    )
                )
            )
            controller._resolved_urls = {}
            controller._find_node = (
                lambda item_id: node if item_id == node["id"] else None
            )
            controller._url_for_node = (
                lambda current: MeetingTreeController._url_for_node(
                    controller,
                    current,
                )
            )
            controller._flush_image_framing_save = lambda: True
            controller._thumbnail_local_path = (
                lambda _node, _item_id, _source: str(thumbnail)
            )
            controller.setAsIdleRequested = self._Signal()

            MeetingTreeController.setAsIdle(controller, "meeting-media")

            [(request,)] = controller.setAsIdleRequested.calls
            self.assertEqual(request.title, "Idle video")
            self.assertEqual(request.path, str(path))
            self.assertEqual(request.media_type, "video")
            self.assertEqual(request.thumbnail_path, str(thumbnail))

    def test_placement_playlist_ref_uses_localized_section_titles(self):
        controller = self.controller(
            [
                {
                    "id": "section",
                    "type": "section",
                    "title": "LIVING AS CHRISTIANS",
                    "meeting_generated": True,
                    "meeting_source_key": "section:mwb:lac",
                    "children": [],
                },
            ]
        )

        with patch(
            "solin.widgets.meetings.tree_controller.display_meeting_section_title",
            return_value="Nossa Vida Cristã",
        ):
            result = MeetingTreeController.placement_playlist_ref(controller)

        self.assertEqual(result["sections"][0]["name"], "Nossa Vida Cristã")

    @staticmethod
    def _section_display_title(node):
        if node.get("user_title_override"):
            return str(node.get("title") or "")
        return "NOSSA VIDA CRISTÃ"

    def _rename_section_controller(self, node, canonical):
        controller = self.controller([node])
        controller._canonical_nodes = [canonical]
        controller._find_node = lambda node_id: node if node_id == node["id"] else None
        controller.parent = lambda: None
        controller.section_updates = []
        controller._emit_section_changed = controller.section_updates.append
        return controller

    def test_rename_official_section_edits_its_localized_display_title(self):
        canonical = {
            "id": "section",
            "type": "section",
            "title": "LIVING AS CHRISTIANS",
            "meeting_generated": True,
            "meeting_source_key": "section:mwb:lac",
            "children": [],
        }
        node = copy.deepcopy(canonical)
        controller = self._rename_section_controller(node, canonical)
        dialog = SimpleNamespace(
            setWindowTitle=lambda _title: None,
            exec=lambda: QDialog.DialogCode.Accepted,
            get_name=lambda: "Minha seção",
        )

        with (
            patch(
                "solin.widgets.meetings.tree_controller.display_meeting_section_title",
                side_effect=self._section_display_title,
            ),
            patch(
                "solin.widgets.meetings.tree_controller.NameDialog",
                return_value=dialog,
            ) as dialog_type,
        ):
            MeetingTreeController.renameSection(controller, "section")

        self.assertEqual(dialog_type.call_args.args[0], "NOSSA VIDA CRISTÃ")
        self.assertEqual(node["title"], "Minha seção")
        self.assertTrue(node["user_title_override"])
        self.assertEqual(controller.saved, 1)
        self.assertEqual(controller.section_updates, [node])

    def test_rename_official_section_to_canonical_display_clears_override(self):
        canonical = {
            "id": "section",
            "type": "section",
            "title": "LIVING AS CHRISTIANS",
            "meeting_generated": True,
            "meeting_source_key": "section:mwb:lac",
            "children": [],
        }
        node = canonical | {
            "title": "Minha seção",
            "user_title_override": True,
        }
        controller = self._rename_section_controller(node, canonical)
        dialog = SimpleNamespace(
            setWindowTitle=lambda _title: None,
            exec=lambda: QDialog.DialogCode.Accepted,
            get_name=lambda: "NOSSA VIDA CRISTÃ",
        )

        with (
            patch(
                "solin.widgets.meetings.tree_controller.display_meeting_section_title",
                side_effect=self._section_display_title,
            ),
            patch(
                "solin.widgets.meetings.tree_controller.NameDialog",
                return_value=dialog,
            ),
        ):
            MeetingTreeController.renameSection(controller, "section")

        self.assertEqual(node["title"], "LIVING AS CHRISTIANS")
        self.assertNotIn("user_title_override", node)
        self.assertEqual(controller.saved, 1)
        self.assertEqual(controller.section_updates, [node])

    def test_accepting_unchanged_canonical_display_does_not_create_override(self):
        canonical = {
            "id": "section",
            "type": "section",
            "title": "LIVING AS CHRISTIANS",
            "meeting_generated": True,
            "meeting_source_key": "section:mwb:lac",
            "children": [],
        }
        node = copy.deepcopy(canonical)
        controller = self._rename_section_controller(node, canonical)
        dialog = SimpleNamespace(
            setWindowTitle=lambda _title: None,
            exec=lambda: QDialog.DialogCode.Accepted,
            get_name=lambda: "NOSSA VIDA CRISTÃ",
        )

        with (
            patch(
                "solin.widgets.meetings.tree_controller.display_meeting_section_title",
                side_effect=self._section_display_title,
            ),
            patch(
                "solin.widgets.meetings.tree_controller.NameDialog",
                return_value=dialog,
            ),
        ):
            MeetingTreeController.renameSection(controller, "section")

        self.assertEqual(node["title"], "LIVING AS CHRISTIANS")
        self.assertNotIn("user_title_override", node)
        self.assertEqual(controller.saved, 0)
        self.assertEqual(controller.section_updates, [])

    def test_delete_official_section_confirmation_uses_display_title(self):
        node = {
            "id": "section",
            "type": "section",
            "title": "LIVING AS CHRISTIANS",
            "meeting_generated": True,
            "meeting_source_key": "section:mwb:lac",
            "children": [],
        }
        controller = self.controller([node])
        controller._find_node = lambda node_id: node if node_id == "section" else None
        controller.parent = lambda: None

        with (
            patch(
                "solin.widgets.meetings.tree_controller.display_meeting_section_title",
                return_value="NOSSA VIDA CRISTÃ",
            ),
            patch.object(
                QMessageBox,
                "question",
                return_value=QMessageBox.StandardButton.No,
            ) as question,
        ):
            MeetingTreeController.deleteSection(controller, "section")

        self.assertIn("NOSSA VIDA CRISTÃ", question.call_args.args[2])
        self.assertNotIn("LIVING AS CHRISTIANS", question.call_args.args[2])

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
            "meeting:test",
            1,
        )

        self.assertTrue(moved)
        self.assertEqual([node["id"] for node in controller._nodes], ["section"])
        self.assertEqual(
            controller._nodes[0]["children"][0]["id"],
            "media",
        )
        self.assertEqual(controller.saved, 1)
        self.assertEqual(controller.count_updates, 1)
        self.assertEqual(controller.stateChanged.calls, [()])

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
            "meeting:test",
            1,
        )

        self.assertFalse(moved)
        self.assertEqual(controller._nodes, before)
        self.assertEqual(controller.saved, 0)
        self.assertEqual(controller.count_updates, 0)

    def test_move_from_another_tree_is_rejected(self):
        nodes = [
            {"id": "first", "type": "media", "children": []},
            {"id": "second", "type": "media", "children": []},
        ]
        controller = self.controller(nodes)

        moved = MeetingTreeController.moveNode(
            controller,
            "first",
            "root",
            1,
            "meeting:another-tree",
            1,
        )

        self.assertFalse(moved)
        self.assertEqual([node["id"] for node in controller._nodes], ["first", "second"])
        self.assertEqual(controller.saved, 0)

    def test_derived_durations_are_coalesced_into_one_complete_snapshot_save(self):
        class FakeTimer:
            def __init__(self):
                self.starts = 0
                self.stops = 0

            def start(self):
                self.starts += 1

            def stop(self):
                self.stops += 1

        nodes = [
            {
                "id": "media-1",
                "type": "media",
                "media_ref": {"file_path": "one.mp4"},
            },
            {
                "id": "media-2",
                "type": "media",
                "media_ref": {"file_path": "two.mp4"},
            },
        ]
        timer = FakeTimer()
        controller = self.controller(nodes)
        controller._tree_key = "mwb:2026-05-25:T:20260500"
        controller._derived_media_patches = {}
        controller._derived_media_save_timer = timer
        controller._save = lambda: setattr(controller, "saved", controller.saved + 1) or True
        controller._info_request_by_token = {
            1: (
                "media-1",
                "metadata",
                MeetingTreeController._media_info_source_identity("one.mp4"),
                "",
            ),
            2: (
                "media-2",
                "metadata",
                MeetingTreeController._media_info_source_identity("two.mp4"),
                "",
            ),
        }
        controller._duration_ticks = MeetingTreeController._duration_ticks.__get__(
            controller,
            type(controller),
        )
        controller._media_info_request_is_current = lambda *_args: True
        controller._find_node = lambda item_id: next(
            (node for node in nodes if node["id"] == item_id),
            None,
        )
        controller._queue_derived_media_patch = lambda node, patch: (
            MeetingTreeController._queue_derived_media_patch(
                controller,
                node,
                patch,
            )
        )
        emitted: list[str] = []
        controller._emit_media_changed = emitted.append
        MeetingTreeController._on_duration_ready(controller, 1, 1_000)
        MeetingTreeController._on_duration_ready(controller, 2, 2_000)

        self.assertEqual(controller.saved, 0)
        self.assertEqual(emitted, ["media-1", "media-2"])
        self.assertEqual(timer.starts, 2)

        self.assertTrue(MeetingTreeController._flush_derived_media_patches(controller))
        self.assertEqual(controller.saved, 1)
        self.assertEqual(controller._derived_media_patches, {})
        self.assertEqual(nodes[0]["base_duration_ticks"], 10_000_000)
        self.assertEqual(nodes[1]["base_duration_ticks"], 20_000_000)

    def test_cache_completion_reprobes_the_remote_source(self):
        url = "https://cdn.example/video.mp4"
        nodes = [{"id": "media", "type": "media", "resolved_url": url}]
        controller = self.controller(nodes)
        controller._cloud_progress_by_url = {url: 0.8}
        requested: list[str] = []
        controller._tree_session = SimpleNamespace(request_source=requested.append)

        MeetingTreeController._on_cache_changed(controller, url)

        self.assertEqual(requested, [url])
        self.assertNotIn(url, controller._cloud_progress_by_url)

    def test_manual_cloud_download_remains_priority_for_excluded_media(self):
        class FakeCacheManager:
            def __init__(self):
                self.calls = []

            def is_cached(self, _url):
                return False

            def is_prefetching(self, _url):
                return False

            def prefetch(self, url, priority=False):
                self.calls.append((url, priority))

        url = "https://cdn.example/subsection-video.mp4"
        manager = FakeCacheManager()
        controller = self.controller([])
        controller._media_cache_manager = manager
        controller._tree_session = SimpleNamespace(owner_id="meeting:test")
        controller._media_tree_runtime = SimpleNamespace(
            registry=SimpleNamespace(state=lambda _owner, _item: SimpleNamespace(cached=False))
        )
        controller._url_for_node_id = lambda _item_id: url
        cloud_updates: list[str] = []
        controller._emit_cloud_for_node = cloud_updates.append

        MeetingTreeController.downloadItem(controller, "nested-official")

        self.assertEqual(manager.calls, [(url, True)])
        self.assertEqual(cloud_updates, ["nested-official"])


class MeetingTreeControllerStorageFeedbackTests(unittest.TestCase):
    class _Signal:
        def __init__(self):
            self.calls = []

        def emit(self, *args):
            self.calls.append(args)

    def test_local_cache_save_reports_rejected_submission(self):
        class FakeController:
            pass

        controller = FakeController()
        controller._tree_key = "mwb:2026-05-25:T:20260500"
        controller._nodes = [{"id": "n1", "type": "section", "children": []}]
        controller._canonical_hash = "hash"
        controller._deleted_source_keys = set()
        controller._linked_folder_files = {}
        controller._meeting_folder_imports = {}
        controller._store = SimpleNamespace(path=Path("meeting-trees.json"))
        controller._current_overview = lambda: None
        controller._snapshot_storage_key = lambda tree_key=None: (
            MeetingTreeController._snapshot_storage_key(
                controller,
                tree_key,
            )
        )
        controller._media_tree_runtime = SimpleNamespace(
            snapshots=SimpleNamespace(request=lambda _key, _writer: 0)
        )

        saved = MeetingTreeController._save_local_cache(controller)

        self.assertFalse(saved)

    def test_local_cache_save_enqueues_immutable_snapshot(self):
        class FakeStore:
            def __init__(self):
                self.calls = []
                self.path = Path("meeting-trees.json")

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
        controller._snapshot_storage_key = lambda tree_key=None: (
            MeetingTreeController._snapshot_storage_key(
                controller,
                tree_key,
            )
        )
        queued: list[tuple[str, object]] = []
        controller._media_tree_runtime = SimpleNamespace(
            snapshots=SimpleNamespace(request=lambda key, writer: queued.append((key, writer)) or 1)
        )
        controller.storageSaved = self._Signal()
        controller.storageSaveFailed = self._Signal()

        saved = MeetingTreeController._save_local_cache(controller)

        self.assertTrue(saved)
        self.assertEqual(controller.storageSaved.calls, [])
        self.assertEqual(controller.storageSaveFailed.calls, [])
        self.assertEqual(len(queued), 1)
        controller._nodes[0]["id"] = "mutated-after-submit"
        queued[0][1]()
        self.assertEqual(len(store.calls), 1)
        self.assertEqual(store.calls[0][0][1][0]["id"], "n1")

        MeetingTreeController._on_snapshot_write_completed(
            controller,
            queued[0][0],
            1,
        )
        self.assertEqual(controller.storageSaved.calls, [(controller._tree_key,)])


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

    def test_cached_snapshot_never_initiates_a_store_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "meeting_trees.json"
            tree_key = "mwb:2026-05-25:T:20260500"
            writer = MeetingTreeStore(path)
            writer.save(tree_key, [], "hash")
            store = MeetingTreeStore(path)

            self.assertFalse(store.loaded)
            self.assertIsNone(store.cached_snapshot(tree_key))
            self.assertFalse(store.loaded)

            snapshot = store.snapshot(tree_key)

            self.assertIsNotNone(snapshot)
            self.assertTrue(store.loaded)
            self.assertEqual(store.cached_snapshot(tree_key), snapshot)

    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = MeetingTreeStore(Path(tmp) / "meeting_trees.json")
            nodes = [
                {
                    "id": "n1",
                    "type": "media",
                    "children": [],
                    "image_framing": {
                        "version": 1,
                        "zoom": 1.4,
                        "norm_x": 0.12,
                        "norm_y": -0.08,
                    },
                }
            ]
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
            self.assertEqual(raw["version"], 6)
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

    def test_prune_before_only_removes_parseable_older_meeting_trees(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "meeting_trees.json"
            untouched_cache = root / "meeting-cache.bin"
            untouched_cache.write_bytes(b"keep")
            old_spoken = "mwb:2026-05-25:T:20260500"
            old_sign = "wt:2026-05-25:T:sign:20260400"
            boundary = "mwb:2026-06-01:T:20260500"
            current = "wt:2026-06-15:T:20260500"
            far_future = "mwb:2027-01-04:T:20270100"
            legacy_unknown = "upgrade-week"
            invalid_date = "wt:not-a-date:T:20260500"
            writer = MeetingTreeStore(path)
            for tree_key in (
                old_spoken,
                old_sign,
                boundary,
                current,
                far_future,
            ):
                writer.save(tree_key, [], tree_key)
            persisted = json.loads(path.read_text(encoding="utf-8"))
            legacy_record = {
                "nodes": [],
                "last_canonical_hash": "legacy",
                "revision": 1,
            }
            persisted["trees"][legacy_unknown] = legacy_record
            persisted["trees"][invalid_date] = legacy_record
            path.write_text(json.dumps(persisted), encoding="utf-8")
            store = MeetingTreeStore(path)
            changes: list[str] = []
            store.subscribe(lambda: changes.append("changed"))

            removed = store.prune_before(date(2026, 6, 1))

            self.assertEqual(removed, 2)
            self.assertEqual(changes, ["changed"])
            self.assertEqual(untouched_cache.read_bytes(), b"keep")
            persisted = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(
                set(persisted["trees"]),
                {
                    boundary,
                    current,
                    far_future,
                    legacy_unknown,
                    invalid_date,
                },
            )

            self.assertEqual(store.prune_before(date(2026, 6, 1)), 0)
            self.assertEqual(changes, ["changed"])

    def test_prune_before_fails_closed_when_store_is_corrupt(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "meeting_trees.json"
            path.write_text("{broken", encoding="utf-8")
            store = MeetingTreeStore(path)

            with self.assertRaises(json.JSONDecodeError):
                store.prune_before(date(2026, 6, 1))

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
                [
                    {
                        "id": "keep",
                        "type": "media",
                        "children": [],
                        "thumbnail_local_path": str(keep),
                    }
                ],
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
        with tempfile.TemporaryDirectory() as tmp:
            fixture = build_synthetic_jwpub(Path(tmp) / "synthetic_meeting_workbook.jwpub")
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
        self.assertEqual(cbs_ref["publication_title"], "Synthetic Study Guide")
        self.assertTrue(any(ref.pub == "reference" for ref in refs))
        cbs_refs = [ref for ref in refs if ref.is_cbs]
        self.assertTrue(cbs_refs)
        self.assertTrue(all(ref.pub == "studyguide" for ref in cbs_refs))
        self.assertFalse(any(ref.pub == "th" for ref in refs))
        titles = list(cbs_ref.get("doc_titles", {}).values())
        self.assertEqual(
            titles,
            ["Synthetic study - Part 1", "Synthetic study - Part 2"],
        )


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
        class ImmediateOperations:
            @staticmethod
            def submit(spec):
                value = spec.runner(
                    lambda _progress: None,
                    SimpleNamespace(is_set=lambda: False),
                )
                spec.commit(value)
                return True

        controller._watched_folder_file_store = WatchedFolderFileStore()
        controller._tree_session = SimpleNamespace(
            owner_id="meeting:test",
            remove_pending_node=lambda _item_id: False,
        )
        controller._media_tree_runtime = SimpleNamespace(operations=ImmediateOperations())
        controller._queue_linked_media_removal = lambda node, file_path, linked_source: (
            MeetingTreeController._queue_linked_media_removal(
                controller,
                node,
                file_path,
                linked_source,
            )
        )
        controller._cancel_media_info_requests_for_item = lambda _item_id: None
        controller._find_node = lambda node_id, nodes=None: MeetingTreeController._find_node(
            controller,
            node_id,
            nodes,
        )
        controller._url_for_node = lambda node: MeetingTreeController._url_for_node(
            controller, node
        )
        controller._remember_deleted_sources = lambda node, *, include_media: (
            MeetingTreeController._remember_deleted_sources(
                controller,
                node,
                include_media=include_media,
            )
        )
        controller._replace_node = lambda node_id, replacement: MeetingTreeController._replace_node(
            controller,
            node_id,
            replacement,
        )
        controller._cleanup_meeting_folder_import_for_removed_node = (
            lambda node_id, file_path, *, source_removed: (
                MeetingTreeController._cleanup_meeting_folder_import_for_removed_node(
                    controller,
                    node_id,
                    file_path,
                    source_removed=source_removed,
                )
            )
        )
        controller.saved = False
        controller._save_and_emit_replace = lambda _node_id, _replacement: setattr(
            controller, "saved", True
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
            controller._url_for_node = lambda node: MeetingTreeController._url_for_node(
                controller, node
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
                lambda list_id, index, nodes: captured.append((list_id, index, nodes)) or True
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
        controller._media_identity_records = lambda: MeetingTreeController._media_identity_records(
            controller
        )
        controller._meeting_thumbnail_store = SimpleNamespace(
            copy_from=lambda *_args: self.fail("duplicate must not copy thumbnail")
        )
        controller._insert_nodes = lambda *_args: self.fail("duplicate must not mutate the tree")

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

    def test_jw_insert_preserves_duration_and_official_thumbnail(self):
        inserted = []
        controller = SimpleNamespace(
            _nodes=[],
            _media_identity_records=lambda: [],
            _insert_nodes=lambda list_id, index, nodes: (
                inserted.append((list_id, index, nodes)) or True
            ),
        )

        result = MeetingTreeController.add_from_jw_catalog(
            controller,
            {
                "title": "Video",
                "download_url": "https://cdn.example/video.mp4",
                "media_type": "video",
                "duration_seconds": 12.5,
                "thumbnail_url": "https://cdn.example/video.jpg",
                "pub": "mwb",
                "track": 1,
                "language": "T",
            },
            "root",
            0,
        )

        self.assertEqual(result.added_count, 1)
        [node] = inserted[0][2]
        self.assertEqual(node["base_duration_ticks"], 125_000_000)
        self.assertEqual(node["thumbnail_url"], "https://cdn.example/video.jpg")
        self.assertEqual(node["thumbnail_binding"], "jw_artwork")

    def test_external_batch_adds_only_unique_items_in_order(self):
        captured = []

        class FakeController:
            _parse_list_id = staticmethod(lambda list_id: (list_id, ""))
            _children_for_target = staticmethod(lambda *_target: [])
            _media_identity_records = staticmethod(lambda: [{"file_path": "existing.mp4"}])
            _node_from_playlist_item = staticmethod(
                lambda item, title: {"title": title, "url": item["url"]}
            )
            _insert_nodes = staticmethod(
                lambda list_id, index, nodes: captured.append((list_id, index, nodes)) or True
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

    def test_local_file_batch_rejects_existing_and_repeated_meeting_media(self):
        captured = []
        duplicate_events = []

        class FakeController:
            mediaAlreadyAdded = SimpleNamespace(
                emit=lambda *args: duplicate_events.append(args)
            )
            _media_identity_records = staticmethod(
                lambda: [{"file_path": "existing.mp4"}]
            )
            _manual_media_node = staticmethod(
                lambda path: {
                    "id": path,
                    "type": "media",
                    "media_ref": {"file_path": path},
                    "children": [],
                }
            )
            _insert_nodes = staticmethod(
                lambda list_id, index, nodes: captured.append((list_id, index, nodes))
                or True
            )

        MeetingTreeController.add_files(
            FakeController(),
            ["existing.mp4", "new.mp4", "new.mp4"],
            list_id="section:talk",
            insert_index=2,
        )

        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0][0:2], ("section:talk", 2))
        self.assertEqual(
            [node["media_ref"]["file_path"] for node in captured[0][2]],
            ["new.mp4"],
        )
        self.assertEqual(
            [event[0] for event in duplicate_events],
            ["existing", "new"],
        )

        MeetingTreeController.add_files(
            FakeController(),
            ["existing.mp4"],
            list_id="root",
            insert_index=0,
        )
        self.assertEqual(len(captured), 1)

    def test_local_file_rejects_media_pending_sync_copy(self):
        captured = []
        duplicate_events = []

        class FakeController:
            _nodes = []
            _tree_session = SimpleNamespace(
                pending_nodes=lambda: (
                    {
                        "id": "pending",
                        "type": "media",
                        "media_ref": {"file_path": "pending.mp4"},
                    },
                )
            )
            mediaAlreadyAdded = SimpleNamespace(
                emit=lambda *args: duplicate_events.append(args)
            )
            _media_identity_records = lambda self: (
                MeetingTreeController._media_identity_records(self)
            )
            _manual_media_node = staticmethod(
                lambda path: {
                    "id": path,
                    "type": "media",
                    "media_ref": {"file_path": path},
                    "children": [],
                }
            )
            _insert_nodes = staticmethod(
                lambda *_args: captured.append(True) or True
            )

        MeetingTreeController.add_files(FakeController(), ["pending.mp4"])

        self.assertEqual(captured, [])
        self.assertEqual(len(duplicate_events), 1)
        self.assertEqual(duplicate_events[0][0], "pending")

    def test_sync_commit_rejects_media_converging_to_existing_linked_file(self):
        class ImmediateOperations:
            @staticmethod
            def submit(spec):
                value = spec.runner(
                    lambda _progress: None,
                    SimpleNamespace(is_set=lambda: False),
                )
                spec.commit(value)
                return True

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source" / "clip.mp4"
            source.parent.mkdir()
            source.write_bytes(b"video")
            folder = root / "2026-05-27 MW"
            folder.mkdir()
            linked = folder / source.name
            linked.write_bytes(source.read_bytes())
            existing = {
                "id": "existing",
                "type": "media",
                "media_ref": {"file_path": str(linked)},
                "children": [],
            }
            duplicate_events = []
            pending = {}
            controller = SimpleNamespace(
                _nodes=[existing],
                _tree_key="mwb:2026-05-25:T:20260500",
                _sync_folder=str(folder),
                _sync_service=MeetingLinkedFolderSync(lambda _pub_type: 2),
                _linked_folder_files={str(linked): "existing"},
                _generated_asset_roots=lambda: (),
                _tree_session=SimpleNamespace(
                    owner_id="meeting:test",
                    generation=1,
                    add_pending=lambda operation_id, nodes, **_kwargs: pending.update(
                        {operation_id: tuple(nodes)}
                    ),
                    remove_pending=lambda operation_id: pending.pop(operation_id, None),
                ),
                _media_tree_runtime=SimpleNamespace(operations=ImmediateOperations()),
                _commit_inserted_nodes=lambda _list_id, _index, nodes, **_kwargs: (
                    controller._nodes.extend(nodes) or True
                ),
                mediaAlreadyAdded=SimpleNamespace(
                    emit=lambda *args: duplicate_events.append(args)
                ),
            )
            incoming = {
                "id": "incoming",
                "type": "media",
                "title": "Clip",
                "media_ref": {"file_path": str(source)},
                "children": [],
            }

            submitted = MeetingTreeController._queue_nodes_for_sync(
                controller,
                "root",
                1,
                [incoming],
                signal_name="media",
            )

            self.assertTrue(submitted)
            self.assertEqual([node["id"] for node in controller._nodes], ["existing"])
            self.assertEqual(
                controller._linked_folder_files,
                {str(linked): "existing"},
            )
            self.assertEqual(len(duplicate_events), 1)
            self.assertEqual(duplicate_events[0][0], "Clip")
            self.assertEqual(pending, {})

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
            controller._meeting_folder_scan_generation = 0
            controller._meeting_folder_scan_operation_id = ""
            controller.chromeChanged = self._Signal()
            controller._watched_folder_file_store = WatchedFolderFileStore()
            controller._apply_meeting_folder_scan = lambda folders, monday, pub_type: (
                MeetingTreeController._apply_meeting_folder_scan(
                    controller,
                    folders,
                    monday,
                    pub_type,
                )
            )

            class ImmediateOperations:
                @staticmethod
                def submit(spec):
                    value = spec.runner(
                        lambda _progress: None,
                        SimpleNamespace(is_set=lambda: False),
                    )
                    spec.commit(value)
                    return True

                @staticmethod
                def cancel(_operation_id):
                    return None

            controller._media_tree_runtime = SimpleNamespace(operations=ImmediateOperations())
            captured = {}

            controller.set_sync_root = lambda _path: None
            controller._meeting_folder_target_list_id = lambda _pub: "section:lac"
            controller._adopt_existing_meeting_folder_source = lambda _source_data, _folder_path: []
            controller._remove_previous_meeting_folder_nodes = lambda _record: None
            controller._start_media_requests = lambda: None

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

    def test_remove_official_linked_media_only_tombstones_it(self):
        class FakeController:
            pass

        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "2026-05-27 MW"
            folder.mkdir()
            source = folder / "official.mp4"
            source.write_bytes(b"video")
            controller = FakeController()
            controller._nodes = [
                {
                    "id": "official",
                    "type": "media",
                    "title": "Resolved official title",
                    "auto_title": False,
                    "resolved_url": "https://cdn.example.invalid/official.mp4",
                    "linked_folder_source": str(folder),
                    "children": [],
                    "media_ref": {"file_path": str(source)},
                    "meeting_generated": True,
                    "meeting_source_key": "media:official",
                }
            ]
            controller._resolved_urls = {}
            controller._linked_folder_files = {str(source): "official"}
            controller._meeting_folder_imports = {}
            controller._deleted_source_keys = set()
            self._wire_remove_item_controller(controller)

            MeetingTreeController.removeItem(controller, "official")

            self.assertTrue(source.exists())
            self.assertEqual(controller._nodes, [])
            self.assertEqual(controller._deleted_source_keys, {"media:official"})
            self.assertEqual(
                controller._hidden_canonical_media["media:official"]["title"],
                "Resolved official title",
            )
            self.assertEqual(
                controller._hidden_canonical_media["media:official"]["resolved_url"],
                "https://cdn.example.invalid/official.mp4",
            )
            self.assertTrue(controller.saved)

    def test_remove_materialized_manual_media_does_not_delete_non_import_source(self):
        class FakeController:
            pass

        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "2026-05-27 MW"
            folder.mkdir()
            materialized = folder / "manually-added.mp4"
            materialized.write_bytes(b"video")
            controller = FakeController()
            controller._nodes = [
                {
                    "id": "manual",
                    "type": "media",
                    "linked_folder_source": str(folder),
                    "children": [],
                    "media_ref": {"file_path": str(materialized)},
                    "meeting_generated": False,
                }
            ]
            controller._resolved_urls = {}
            controller._linked_folder_files = {str(materialized): "manual"}
            controller._meeting_folder_imports = {}
            controller._deleted_source_keys = set()
            self._wire_remove_item_controller(controller)

            MeetingTreeController.removeItem(controller, "manual")

            self.assertTrue(materialized.exists())
            self.assertEqual(controller._nodes, [])
            self.assertEqual(controller._linked_folder_files, {})
            self.assertTrue(controller.saved)


if __name__ == "__main__":
    unittest.main()
