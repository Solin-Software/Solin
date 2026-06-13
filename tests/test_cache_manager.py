from __future__ import annotations

import tempfile
import unittest

from PySide6.QtCore import QCoreApplication, QObject, Signal

from solin.core.media.cache import MediaCacheManager
from solin.core.i18n.strings import tr_offline_queued
from solin.widgets.media_library_widget import MediaLibraryModel


class FakeDownloader(QObject):
    progress = Signal(int, int)
    finished = Signal(str)
    error = Signal(str)

    created: list["FakeDownloader"] = []

    def __init__(self, parent=None):
        super().__init__(parent)
        self.url = ""
        self.canceled = False
        FakeDownloader.created.append(self)

    def start(self, url: str, persist: bool = True) -> None:
        self.url = url

    def cancel(self) -> None:
        self.canceled = True


class MediaCacheManagerQueueTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._app = QCoreApplication.instance() or QCoreApplication([])

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        FakeDownloader.created.clear()
        self.mgr = MediaCacheManager(self._tmp.name)
        self.mgr._downloader_factory = lambda parent: FakeDownloader(parent)

    def tearDown(self):
        self.mgr.cancel_all()
        self._tmp.cleanup()

    def urls(self, count: int) -> list[str]:
        return [f"https://cdn.example/media-{i}.mp4" for i in range(count)]

    def downloader_for(self, url: str) -> FakeDownloader:
        for downloader in reversed(FakeDownloader.created):
            if downloader.url == url and not downloader.canceled:
                return downloader
        raise AssertionError(f"No active fake downloader for {url}")

    def test_prefetch_many_deduplicates_and_limits_concurrency(self):
        urls = self.urls(5)

        added = self.mgr.prefetch_many(urls + [urls[0]], "batch")

        self.assertEqual(added, 5)
        self.assertEqual([dl.url for dl in FakeDownloader.created], urls[:3])
        self.assertEqual(self.mgr.batch_counts("batch"), (2, 3, 0, 0))
        self.assertTrue(self.mgr.is_queued(urls[3]))
        self.assertFalse(self.mgr.is_queued(urls[0]))

        self.downloader_for(urls[0]).finished.emit("cached.mp4")

        self.assertEqual([dl.url for dl in FakeDownloader.created], urls[:4])
        self.assertEqual(self.mgr.batch_counts("batch"), (1, 3, 1, 0))

    def test_prefetch_many_coalesces_batch_changed_signal(self):
        self.mgr.max_concurrent_prefetches = 0
        events: list[tuple[int, int, int, int]] = []
        self.mgr.prefetch_batch_changed.connect(
            lambda _batch_id, queued, active, done, failed: events.append(
                (queued, active, done, failed)
            )
        )

        self.mgr.prefetch_many(self.urls(5), "batch")

        self.assertEqual(events, [(5, 0, 0, 0)])

    def test_priority_prefetch_moves_queued_url_to_front(self):
        self.mgr.max_concurrent_prefetches = 1
        urls = self.urls(3)
        self.mgr.prefetch_many(urls, "batch")

        self.mgr.prefetch(urls[2], priority=True)
        self.downloader_for(urls[0]).finished.emit("cached.mp4")

        self.assertEqual([dl.url for dl in FakeDownloader.created], [urls[0], urls[2]])

    def test_cancel_batch_removes_queued_and_active_items(self):
        urls = self.urls(4)
        self.mgr.max_concurrent_prefetches = 2
        self.mgr.prefetch_many(urls, "batch")

        self.mgr.cancel_batch("batch")

        self.assertEqual(self.mgr.batch_counts("batch"), (0, 0, 0, 0))
        self.assertTrue(all(dl.canceled for dl in FakeDownloader.created[:2]))
        self.assertFalse(any(self.mgr.is_queued(url) for url in urls))
        self.assertFalse(any(self.mgr.is_prefetching(url) for url in urls))

    def test_prefetch_retries_once_before_error(self):
        urls = self.urls(1)
        errors: list[tuple[str, str]] = []
        self.mgr.prefetch_error.connect(lambda url, msg: errors.append((url, msg)))
        self.mgr.prefetch_many(urls, "batch")

        self.downloader_for(urls[0]).error.emit("temporary")
        self.assertEqual([dl.url for dl in FakeDownloader.created], [urls[0], urls[0]])
        self.assertEqual(errors, [])

        self.downloader_for(urls[0]).error.emit("temporary")
        self.assertEqual(errors, [(urls[0], "temporary")])
        self.assertEqual(self.mgr.batch_counts("batch"), (0, 0, 0, 1))

    def test_progress_signal_is_throttled_to_visible_percent_changes(self):
        url = self.urls(1)[0]
        progress: list[tuple[int, int]] = []
        self.mgr.prefetch_progress.connect(
            lambda _url, downloaded, total: progress.append((downloaded, total))
        )
        self.mgr.prefetch(url)

        self.downloader_for(url).progress.emit(1, 1000)
        self.downloader_for(url).progress.emit(2, 1000)
        self.downloader_for(url).progress.emit(10, 1000)

        self.assertEqual(progress, [(1, 1000), (10, 1000)])

    def test_batch_aborts_after_three_preprogress_failures(self):
        self.mgr.max_concurrent_prefetches = 1
        urls = self.urls(5)
        batch_errors: list[tuple[str, str]] = []
        self.mgr.prefetch_batch_error.connect(
            lambda batch_id, msg: batch_errors.append((batch_id, msg))
        )
        self.mgr.prefetch_many(urls, "batch")

        for expected_url in urls[:3]:
            self.downloader_for(expected_url).error.emit("network")
            self.downloader_for(expected_url).error.emit("network")

        self.assertEqual(len(batch_errors), 1)
        self.assertEqual(batch_errors[0][0], "batch")
        self.assertEqual(self.mgr.batch_counts("batch"), (0, 0, 0, 3))
        self.assertNotIn(urls[3], [dl.url for dl in FakeDownloader.created])

    def test_fatal_cache_error_aborts_without_retry(self):
        self.mgr.max_concurrent_prefetches = 1
        urls = self.urls(3)
        batch_errors: list[str] = []
        self.mgr.prefetch_batch_error.connect(lambda _batch_id, msg: batch_errors.append(msg))
        self.mgr.prefetch_many(urls, "batch")

        self.downloader_for(urls[0]).error.emit("No space left on device")

        self.assertEqual([dl.url for dl in FakeDownloader.created], [urls[0]])
        self.assertEqual(len(batch_errors), 1)
        self.assertEqual(self.mgr.batch_counts("batch"), (0, 0, 0, 1))


class MediaLibraryModelQueueTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._app = QCoreApplication.instance() or QCoreApplication([])

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        FakeDownloader.created.clear()
        self.mgr = MediaCacheManager(self._tmp.name)
        self.mgr.max_concurrent_prefetches = 1
        self.mgr._downloader_factory = lambda parent: FakeDownloader(parent)

    def tearDown(self):
        self.mgr.cancel_all()
        self._tmp.cleanup()

    def test_model_exposes_queued_state_and_tooltip(self):
        active_url = "https://cdn.example/active.mp4"
        queued_url = "https://cdn.example/queued.mp4"
        model = MediaLibraryModel(self.mgr)
        try:
            model.set_items([
                {"url": queued_url, "title": "Queued", "duration": 1},
            ], audio_mode=False)

            self.mgr.prefetch(active_url)
            self.mgr.prefetch(queued_url)
            index = model.index(0, 0)

            self.assertTrue(model.data(index, MediaLibraryModel.CloudQueuedRole))
            self.assertEqual(
                model.data(index, MediaLibraryModel.CloudTooltipRole),
                tr_offline_queued(),
            )
        finally:
            model.cleanup()


if __name__ == "__main__":
    unittest.main()
