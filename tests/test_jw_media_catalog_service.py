from __future__ import annotations

import time
import unittest
from unittest.mock import patch

from solin.core.jw import catalog as svc
from solin.core.jw.catalog import JWMediaItem


def item(
    key: str,
    *,
    published: str = "2026-01-01T00:00:00Z",
    guid: str | None = None,
    url: str | None = None,
) -> JWMediaItem:
    return JWMediaItem(
        id=f"id-{key}",
        title=f"Video {key}",
        language="E",
        media_type="video",
        source="category:LatestVideos",
        download_url=url or f"https://cdn.example/{key}.mp4",
        guid=guid if guid is not None else f"guid-{key}",
        natural_key=f"natural-{key}",
        first_published=published,
    )


class JWMediaCatalogLatestDeltaTests(unittest.TestCase):
    def stale_snapshot(self, items: list[JWMediaItem]) -> dict:
        return {
            "_fetched_at": time.time() - svc._CATALOG_REFRESH_INTERVAL_S - 60,
            "completed_categories": ["LatestVideos", "CatA"],
            "all_categories": ["LatestVideos", "CatA"],
            "items": [media_item.to_dict() for media_item in items],
        }

    def test_latest_delta_adds_new_items_when_latest_contains_known_item(self):
        old = item("old", published="2026-01-01T00:00:00Z")
        latest_items = [
            *[
                item(f"new-{idx}", published=f"2026-01-{idx + 2:02d}T00:00:00Z")
                for idx in range(11)
            ],
            old,
        ]
        fetched_categories: list[str] = []

        def fetch_category_media(_language, category, **_kwargs):
            fetched_categories.append(category)
            return {}, time.time(), False

        with (
            patch.object(svc, "_load_catalog_snapshot", return_value=self.stale_snapshot([old])),
            patch.object(svc, "_discover_video_categories", return_value=["LatestVideos", "CatA"]),
            patch.object(svc, "_fetch_category_media", side_effect=fetch_category_media),
            patch.object(svc, "_parse_category_media", return_value=latest_items),
            patch.object(svc, "_save_catalog_snapshot") as save_snapshot,
        ):
            items, _fetched_at, from_cache = svc.fetch_jw_video_catalog("E")

        self.assertTrue(from_cache)
        self.assertEqual(fetched_categories, ["LatestVideos"])
        self.assertEqual(len(items), 12)
        self.assertIn("guid-old", {media_item.guid for media_item in items})
        save_snapshot.assert_called_once()

    def test_latest_delta_with_no_new_items_only_marks_snapshot_checked(self):
        old_1 = item("old-1", published="2026-01-02T00:00:00Z")
        old_2 = item("old-2", published="2026-01-01T00:00:00Z")
        fetched_categories: list[str] = []

        def fetch_category_media(_language, category, **_kwargs):
            fetched_categories.append(category)
            return {}, time.time(), False

        with (
            patch.object(
                svc,
                "_load_catalog_snapshot",
                return_value=self.stale_snapshot([old_1, old_2]),
            ),
            patch.object(svc, "_discover_video_categories", return_value=["LatestVideos", "CatA"]),
            patch.object(svc, "_fetch_category_media", side_effect=fetch_category_media),
            patch.object(svc, "_parse_category_media", return_value=[old_1, old_2]),
            patch.object(svc, "_save_catalog_snapshot") as save_snapshot,
        ):
            items, _fetched_at, from_cache = svc.fetch_jw_video_catalog("E")

        self.assertTrue(from_cache)
        self.assertEqual(fetched_categories, ["LatestVideos"])
        self.assertEqual({media_item.guid for media_item in items}, {"guid-old-1", "guid-old-2"})
        save_snapshot.assert_called_once()

    def test_latest_delta_with_all_returned_items_new_falls_back_to_full_scan(self):
        old = item("old", published="2026-01-01T00:00:00Z")
        latest_items = [
            item(f"new-{idx}", published=f"2026-01-{idx + 2:02d}T00:00:00Z")
            for idx in range(7)
        ]
        cat_item = item("cat", published="2026-01-15T00:00:00Z")
        fetched_categories: list[str] = []
        discover_force_values: list[bool] = []

        def discover_categories(_language, *, force, **_kwargs):
            discover_force_values.append(force)
            return ["LatestVideos", "CatA"]

        def fetch_category_media(_language, category, **_kwargs):
            fetched_categories.append(category)
            return {}, time.time(), False

        def parse_category_media(_raw, query):
            if query.category == "LatestVideos":
                return latest_items
            if query.category == "CatA":
                return [cat_item]
            return []

        with (
            patch.object(svc, "_load_catalog_snapshot", return_value=self.stale_snapshot([old])),
            patch.object(svc, "_discover_video_categories", side_effect=discover_categories),
            patch.object(svc, "_fetch_category_media", side_effect=fetch_category_media),
            patch.object(svc, "_parse_category_media", side_effect=parse_category_media),
            patch.object(svc, "_save_catalog_snapshot"),
        ):
            items, _fetched_at, _from_cache = svc.fetch_jw_video_catalog("E")

        self.assertEqual(discover_force_values, [False, True])
        self.assertEqual(fetched_categories, ["LatestVideos", "LatestVideos", "CatA"])
        self.assertIn("guid-cat", {media_item.guid for media_item in items})

    def test_latest_delta_flag_off_uses_full_refresh_path(self):
        old = item("old", published="2026-01-01T00:00:00Z")
        latest_item = item("latest", published="2026-01-02T00:00:00Z")
        cat_item = item("cat", published="2026-01-03T00:00:00Z")
        fetched_categories: list[str] = []
        discover_force_values: list[bool] = []

        def discover_categories(_language, *, force, **_kwargs):
            discover_force_values.append(force)
            return ["LatestVideos", "CatA"]

        def fetch_category_media(_language, category, **_kwargs):
            fetched_categories.append(category)
            return {}, time.time(), False

        def parse_category_media(_raw, query):
            if query.category == "LatestVideos":
                return [latest_item]
            if query.category == "CatA":
                return [cat_item]
            return []

        with (
            patch.object(svc, "USE_LATEST_DELTA_CATALOG_REFRESH", False),
            patch.object(svc, "_load_catalog_snapshot", return_value=self.stale_snapshot([old])),
            patch.object(svc, "_discover_video_categories", side_effect=discover_categories),
            patch.object(svc, "_fetch_category_media", side_effect=fetch_category_media),
            patch.object(svc, "_parse_category_media", side_effect=parse_category_media),
            patch.object(svc, "_save_catalog_snapshot"),
        ):
            items, _fetched_at, _from_cache = svc.fetch_jw_video_catalog("E")

        self.assertEqual(discover_force_values, [False, True])
        self.assertEqual(fetched_categories, ["LatestVideos", "CatA"])
        self.assertEqual(
            {media_item.guid for media_item in items},
            {"guid-old", "guid-latest", "guid-cat"},
        )


if __name__ == "__main__":
    unittest.main()
