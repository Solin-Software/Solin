from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from PySide6.QtCore import QCoreApplication

from solin.core.jw.catalog import JWMediaCatalogCachePaths
from solin.core.jw.catalog_service import JWMediaCatalogService
from solin.core.media.insertion import MediaInsertResult
from solin.core.media.placement import build_media_placement_options
from solin.ui.qml.jw_media_catalog import (
    JWMediaCatalogBridge,
    JWMediaCatalogModel,
)


class _SignalStub:
    def __init__(self):
        self.callbacks = []

    def connect(self, callback):
        self.callbacks.append(callback)

    def emit(self, *args):
        for callback in self.callbacks:
            callback(*args)


class _ThumbnailSessionStub:
    def __init__(self):
        self.ready = _SignalStub()
        self.enqueued = []
        self.reset_count = 0
        self.closed = False

    def enqueue(self, item_id, thumbnail_url):
        self.enqueued.append((item_id, thumbnail_url))
        return True

    def reset(self):
        self.reset_count += 1

    def close(self):
        self.closed = True


class _ThumbnailSessionFactoryStub:
    def __init__(self):
        self.session = _ThumbnailSessionStub()
        self.parents = []

    def create(self, *, parent=None):
        self.parents.append(parent)
        return self.session


def item(key: str) -> dict:
    return {
        "id": f"id-{key}",
        "title": f"Video {key}",
        "download_url": f"https://cdn.example/{key}.mp4",
        "thumbnail_url": f"https://cdn.example/{key}.jpg",
        "thumbnail_path": "",
        "duration_seconds": 10,
        "duration_ticks": 100_000_000,
        "primary_category": "",
    }


def playlist_with(item_count: int, sections: list[dict] | None = None) -> dict:
    return {
        "items": [{"id": f"item-{index}"} for index in range(item_count)],
        "sections": sections or [],
    }


def option_ids(options: list[dict]) -> list[str]:
    return [option["id"] for option in options]


def placement_options(playlist: dict) -> list[dict]:
    return build_media_placement_options(playlist, translate=lambda text: text)


class JWMediaCatalogModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._app = QCoreApplication.instance() or QCoreApplication([])

    def test_reconcile_items_preserves_stable_rows_without_model_reset(self):
        model = JWMediaCatalogModel()
        resets: list[None] = []
        insertions: list[tuple[int, int]] = []
        removals: list[tuple[int, int]] = []
        moves: list[tuple[int, int]] = []
        updates: list[tuple[int, tuple[int, ...]]] = []
        model.modelReset.connect(lambda: resets.append(None))
        model.rowsInserted.connect(
            lambda _parent, first, last: insertions.append((first, last))
        )
        model.rowsRemoved.connect(
            lambda _parent, first, last: removals.append((first, last))
        )
        model.rowsMoved.connect(
            lambda _source_parent, source, _source_last, _dest_parent, dest: (
                moves.append((source, dest))
            )
        )
        model.dataChanged.connect(
            lambda top, _bottom, roles: updates.append(
                (top.row(), tuple(int(role) for role in roles))
            )
        )

        self.assertTrue(
            model.reconcile_items([item("a"), item("b"), item("c")])
        )
        self.assertFalse(
            model.reconcile_items([item("a"), item("b"), item("c")])
        )
        changed_title = item("a")
        changed_title["title"] = "Updated title"
        self.assertTrue(
            model.reconcile_items([item("c"), changed_title, item("d")])
        )

        self.assertEqual(resets, [])
        self.assertEqual(insertions[0], (0, 2))
        self.assertIn((1, 1), removals)
        self.assertIn((1, 0), moves)
        self.assertTrue(updates)
        self.assertEqual(
            [model.item_at(index)["id"] for index in range(model.rowCount())],
            ["id-c", "id-a", "id-d"],
        )

    def test_reconcile_items_uses_remove_rows_when_cleared(self):
        model = JWMediaCatalogModel()
        resets: list[None] = []
        removals: list[tuple[int, int]] = []
        model.modelReset.connect(lambda: resets.append(None))
        model.rowsRemoved.connect(
            lambda _parent, first, last: removals.append((first, last))
        )
        model.reconcile_items([item("a"), item("b")])

        self.assertTrue(model.reconcile_items([]))

        self.assertEqual(resets, [])
        self.assertEqual(removals, [(0, 1)])
        self.assertEqual(model.rowCount(), 0)


class JWMediaPlacementOptionsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._app = QCoreApplication.instance() or QCoreApplication([])

    def test_many_items_include_single_section_when_prompting(self):
        options = placement_options(
            playlist_with(
                14,
                [{"id": "section-1", "name": "Opening", "color_hue": 170}],
            ),
        )

        self.assertEqual(option_ids(options), ["top", "bottom", "section:section-1"])

    def test_two_sections_prompt_even_with_short_playlist(self):
        options = placement_options(
            playlist_with(
                1,
                [
                    {"id": "section-1", "name": "Opening", "color_hue": 170},
                    {"id": "section-2", "name": "Main", "color_hue": 240},
                ],
            ),
        )

        self.assertEqual(
            option_ids(options),
            ["top", "bottom", "section:section-1", "section:section-2"],
        )

    def test_single_section_short_playlist_does_not_prompt(self):
        options = placement_options(
            playlist_with(
                1,
                [{"id": "section-1", "name": "Opening", "color_hue": 170}],
            ),
        )

        self.assertEqual(options, [])


class JWMediaCatalogBridgeProgressTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._app = QCoreApplication.instance() or QCoreApplication([])

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.cache_paths = JWMediaCatalogCachePaths(
            root / "cache",
            root / "thumbs",
        )
        self.thumbnail_factory = _ThumbnailSessionFactoryStub()

    def tearDown(self):
        if hasattr(self, "bridge"):
            self.bridge.cleanup()
        self._tmp.cleanup()

    def _catalog_service(self, parent):
        return JWMediaCatalogService(self.cache_paths, parent)

    @staticmethod
    def _insert(item, _list_id, _index):
        return MediaInsertResult(added_items=(item,))

    def test_first_progress_applies_immediately(self):
        self.bridge = JWMediaCatalogBridge(
            self._catalog_service,
            self.thumbnail_factory,
            insertion_handler=self._insert,
        )
        self.bridge._active_catalog_request_id = "request"

        self.bridge._on_videos_progress("request", [item("a")], 1, 5)

        self.assertEqual(self.bridge._load_completed, 1)
        self.assertEqual([entry["id"] for entry in self.bridge._all_items], ["id-a"])

    def test_progress_reconciles_immediately_when_page_is_already_visible(self):
        self.bridge = JWMediaCatalogBridge(
            self._catalog_service,
            self.thumbnail_factory,
            insertion_handler=self._insert,
        )
        self.bridge._active_catalog_request_id = "request"
        self.bridge._model.reconcile_items([item("visible")])

        self.bridge._on_videos_progress("request", [item("old")], 1, 5)
        self.bridge._on_videos_progress("request", [item("new")], 2, 5)

        self.assertEqual(self.bridge._load_completed, 2)
        self.assertEqual([entry["id"] for entry in self.bridge._all_items], ["id-new"])

    def test_thumbnail_work_is_delegated_to_injected_session(self):
        self.bridge = JWMediaCatalogBridge(
            self._catalog_service,
            self.thumbnail_factory,
            insertion_handler=self._insert,
        )

        self.bridge._queue_thumbnails([item("a"), item("b")])

        self.assertEqual(
            self.thumbnail_factory.session.enqueued,
            [
                ("id-a", "https://cdn.example/a.jpg"),
                ("id-b", "https://cdn.example/b.jpg"),
            ],
        )
        self.assertEqual(self.thumbnail_factory.parents, [self.bridge])

    def test_resolved_thumbnail_does_not_regress_on_later_metadata_snapshot(self):
        self.bridge = JWMediaCatalogBridge(
            self._catalog_service,
            self.thumbnail_factory,
            insertion_handler=self._insert,
        )
        self.bridge._active_catalog_request_id = "request"
        media = item("stable")
        resets: list[None] = []
        self.bridge._model.modelReset.connect(lambda: resets.append(None))

        self.bridge._on_videos_progress("request", [media], 1, 5)
        self.bridge._on_thumb_ready(
            media["id"],
            media["thumbnail_url"],
            "cached/stable.jpg",
        )
        self.bridge._on_videos_progress("request", [item("stable")], 2, 5)

        self.assertEqual(
            self.bridge._all_items[0]["thumbnail_path"],
            "cached/stable.jpg",
        )
        self.assertEqual(
            self.bridge._model.item_at(0)["thumbnail_path"],
            "cached/stable.jpg",
        )
        self.assertEqual(resets, [])

    def test_stale_thumbnail_result_is_ignored_after_url_changes(self):
        self.bridge = JWMediaCatalogBridge(
            self._catalog_service,
            self.thumbnail_factory,
            insertion_handler=self._insert,
        )
        self.bridge._active_catalog_request_id = "request"
        old = item("changing")
        self.bridge._on_videos_progress("request", [old], 1, 5)

        updated = item("changing")
        updated["thumbnail_url"] = "https://cdn.example/changing-v2.jpg"
        self.bridge._on_videos_progress("request", [updated], 2, 5)
        self.bridge._on_thumb_ready(
            old["id"],
            old["thumbnail_url"],
            "cached/stale.jpg",
        )

        self.assertEqual(self.bridge._all_items[0]["thumbnail_path"], "")
        self.assertEqual(self.bridge._model.item_at(0)["thumbnail_path"], "")

        self.bridge._on_thumb_ready(
            updated["id"],
            updated["thumbnail_url"],
            "cached/current.jpg",
        )

        self.assertEqual(
            self.bridge._model.item_at(0)["thumbnail_path"],
            "cached/current.jpg",
        )

    def test_large_progress_sequence_never_resets_visible_page(self):
        self.bridge = JWMediaCatalogBridge(
            self._catalog_service,
            self.thumbnail_factory,
            insertion_handler=self._insert,
        )
        self.bridge._active_catalog_request_id = "request"
        catalog = [item(f"catalog-{index}") for index in range(3_938)]
        accumulated = list(catalog[:3_763])
        resets: list[None] = []
        self.bridge._model.modelReset.connect(lambda: resets.append(None))

        for completed, new_item in enumerate(catalog[3_763:], start=1):
            accumulated.insert(0, new_item)
            self.bridge._on_videos_progress(
                "request",
                accumulated,
                completed,
                175,
            )

        self.assertEqual(resets, [])
        self.assertEqual(self.bridge.resultCount, 3_938)
        self.assertEqual(self.bridge._model.rowCount(), 24)
        self.assertEqual(
            self.bridge._model.item_at(0)["id"],
            catalog[-1]["id"],
        )

    def test_reset_and_cleanup_delegate_thumbnail_lifecycle(self):
        self.bridge = JWMediaCatalogBridge(
            self._catalog_service,
            self.thumbnail_factory,
            insertion_handler=self._insert,
        )

        self.bridge.reset()
        self.bridge.cleanup()

        self.assertEqual(self.thumbnail_factory.session.reset_count, 1)
        self.assertTrue(self.thumbnail_factory.session.closed)

    def test_close_preserves_catalog_and_clears_only_transient_state(self):
        self.bridge = JWMediaCatalogBridge(
            self._catalog_service,
            self.thumbnail_factory,
            insertion_handler=self._insert,
        )
        catalog_items = [item("a"), item("b")]
        self.bridge._all_items = catalog_items
        self.bridge._filtered_items = list(catalog_items)
        self.bridge._model.reconcile_items(catalog_items)
        self.bridge._search_query = "video"
        self.bridge._current_page = 2
        self.bridge._include_audio_description = True
        self.bridge._pending_item = item("pending")
        self.bridge._show_placement = True
        self.bridge._placement_options = [{"id": "bottom"}]

        self.bridge.closeModal()

        self.assertEqual(self.bridge._all_items, catalog_items)
        self.assertEqual(self.bridge.searchQuery, "")
        self.assertEqual(self.bridge.currentPage, 1)
        self.assertFalse(self.bridge.includeAudioDescription)
        self.assertIsNone(self.bridge._pending_item)
        self.assertFalse(self.bridge.showPlacement)
        self.assertEqual(self.bridge.placementOptions, [])
        self.assertEqual(self.thumbnail_factory.session.reset_count, 0)

    def test_reopen_uses_preserved_catalog_without_starting_another_fetch(self):
        self.bridge = JWMediaCatalogBridge(
            self._catalog_service,
            self.thumbnail_factory,
            insertion_handler=self._insert,
        )
        self.bridge._all_items = [item("cached")]
        self.bridge._catalog_fetched_at = time.time()

        fetch_calls: list[str] = []
        self.bridge._catalog_service.fetch_all_videos = (
            lambda language: fetch_calls.append(language) or "request"
        )

        self.bridge.openModal()

        self.assertEqual(fetch_calls, [])

    def test_reopen_revalidates_expired_catalog_without_clearing_visible_items(self):
        self.bridge = JWMediaCatalogBridge(
            self._catalog_service,
            self.thumbnail_factory,
            insertion_handler=self._insert,
        )
        cached = item("cached")
        self.bridge._all_items = [cached]
        self.bridge._filtered_items = [cached]
        self.bridge._model.reconcile_items([cached])
        self.bridge._catalog_fetched_at = 1.0

        fetch_calls: list[str] = []
        self.bridge._catalog_service.fetch_all_videos = (
            lambda language: fetch_calls.append(language) or "refresh"
        )

        self.bridge.openModal()

        self.assertEqual(fetch_calls, ["E"])
        self.assertEqual(self.bridge._all_items, [cached])
        self.assertEqual(self.bridge.model.rowCount(), 1)
        self.assertTrue(self.bridge.isLoading)

    def test_failed_refresh_keeps_cached_catalog_visible(self):
        self.bridge = JWMediaCatalogBridge(
            self._catalog_service,
            self.thumbnail_factory,
            insertion_handler=self._insert,
        )
        cached = item("cached")
        self.bridge._all_items = [cached]
        self.bridge._filtered_items = [cached]
        self.bridge._model.reconcile_items([cached])
        self.bridge._is_loading = True
        self.bridge._active_catalog_request_id = "refresh"

        self.bridge._on_fetch_failed("refresh", "network unavailable")

        self.assertEqual(self.bridge._all_items, [cached])
        self.assertEqual(self.bridge.errorMessage, "")
        self.assertFalse(self.bridge.isLoading)

    def test_duplicate_selection_is_rejected_before_placement(self):
        insertions = []
        self.bridge = JWMediaCatalogBridge(
            self._catalog_service,
            self.thumbnail_factory,
            insertion_handler=lambda *args: insertions.append(args),
        )
        candidate = item("duplicate")
        self.bridge._model.reconcile_items([candidate])
        self.bridge.set_playlist_ref(
            {"items": [{"url": candidate["download_url"]}], "sections": []}
        )
        rejected = []
        self.bridge.mediaAlreadyAdded.connect(lambda *args: rejected.append(args))

        self.bridge.selectItem(0)

        self.assertEqual(insertions, [])
        self.assertEqual(rejected[0][0], "Video duplicate")
        self.assertIsNone(self.bridge._pending_item)
        self.assertFalse(self.bridge.showPlacement)

    def test_authoritative_duplicate_does_not_close_modal(self):
        candidate = item("race")
        self.bridge = JWMediaCatalogBridge(
            self._catalog_service,
            self.thumbnail_factory,
            insertion_handler=lambda *_args: MediaInsertResult(
                duplicate_items=(candidate,)
            ),
        )
        self.bridge._pending_item = candidate
        closed = []
        rejected = []
        self.bridge.modalShouldClose.connect(lambda: closed.append(True))
        self.bridge.mediaAlreadyAdded.connect(lambda *args: rejected.append(args))

        self.bridge.confirmPlacement("bottom")

        self.assertEqual(closed, [])
        self.assertEqual(rejected[0][0], "Video race")
        self.assertIsNone(self.bridge._pending_item)


if __name__ == "__main__":
    unittest.main()
