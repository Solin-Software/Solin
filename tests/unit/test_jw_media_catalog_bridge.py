from __future__ import annotations

import unittest
import tempfile
from pathlib import Path

from PySide6.QtCore import QCoreApplication

from solin.core.jw.catalog import JWMediaCatalogCachePaths, JWMediaCatalogService
from solin.ui.qml.jw_media_catalog import (
    JWMediaCatalogBridge,
    JWMediaCatalogModel,
    build_jw_media_placement_options,
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


class JWMediaCatalogModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._app = QCoreApplication.instance() or QCoreApplication([])

    def test_set_items_if_changed_skips_identical_page_reset(self):
        model = JWMediaCatalogModel()
        resets: list[None] = []
        model.modelReset.connect(lambda: resets.append(None))

        self.assertTrue(model.set_items_if_changed([item("a"), item("b")]))
        self.assertFalse(model.set_items_if_changed([item("a"), item("b")]))
        self.assertTrue(model.set_items_if_changed([item("b"), item("a")]))

        self.assertEqual(len(resets), 2)


class JWMediaPlacementOptionsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._app = QCoreApplication.instance() or QCoreApplication([])

    def test_many_items_include_single_section_when_prompting(self):
        options = build_jw_media_placement_options(
            playlist_with(
                14,
                [{"id": "section-1", "name": "Opening", "color_hue": 170}],
            ),
        )

        self.assertEqual(option_ids(options), ["top", "bottom", "section:section-1"])

    def test_two_sections_prompt_even_with_short_playlist(self):
        options = build_jw_media_placement_options(
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
        options = build_jw_media_placement_options(
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

    def test_first_progress_applies_immediately(self):
        self.bridge = JWMediaCatalogBridge(
            self._catalog_service,
            self.thumbnail_factory,
        )
        self.bridge._active_catalog_request_id = "request"

        self.bridge._on_videos_progress("request", [item("a")], 1, 5)

        self.assertEqual(self.bridge._load_completed, 1)
        self.assertEqual([entry["id"] for entry in self.bridge._all_items], ["id-a"])

    def test_progress_is_coalesced_when_page_is_already_visible(self):
        self.bridge = JWMediaCatalogBridge(
            self._catalog_service,
            self.thumbnail_factory,
        )
        self.bridge._active_catalog_request_id = "request"
        self.bridge._model.set_items([item("visible")])

        self.bridge._on_videos_progress("request", [item("old")], 1, 5)
        self.bridge._on_videos_progress("request", [item("new")], 2, 5)

        self.assertEqual(self.bridge._all_items, [])

        self.bridge._flush_pending_progress()

        self.assertEqual(self.bridge._load_completed, 2)
        self.assertEqual([entry["id"] for entry in self.bridge._all_items], ["id-new"])

    def test_thumbnail_work_is_delegated_to_injected_session(self):
        self.bridge = JWMediaCatalogBridge(
            self._catalog_service,
            self.thumbnail_factory,
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

    def test_reset_and_cleanup_delegate_thumbnail_lifecycle(self):
        self.bridge = JWMediaCatalogBridge(
            self._catalog_service,
            self.thumbnail_factory,
        )

        self.bridge.reset()
        self.bridge.cleanup()

        self.assertEqual(self.thumbnail_factory.session.reset_count, 1)
        self.assertTrue(self.thumbnail_factory.session.closed)


if __name__ == "__main__":
    unittest.main()
