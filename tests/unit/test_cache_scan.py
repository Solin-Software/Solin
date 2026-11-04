from __future__ import annotations

from solin.core.media import cache_scan


def test_cache_scan_worker_delegates_directory_and_cancellation(monkeypatch):
    calls = []
    results = []

    def scan(media_cache_dir, *, is_cancelled, on_batch):
        calls.append((media_cache_dir, is_cancelled()))
        on_batch(["cached-item"])
        return ["cached-item"]

    monkeypatch.setattr(cache_scan, "scan_cached_media_items", scan)
    worker = cache_scan._CacheScanWorker("cache/media")
    worker.results_ready.connect(results.append)
    batches = []
    worker.batch_ready.connect(batches.append)

    worker.run()
    worker.cancel()
    worker.run()

    assert calls == [
        ("cache/media", False),
        ("cache/media", True),
    ]
    assert results == [["cached-item"], ["cached-item"]]
    assert batches == [["cached-item"], ["cached-item"]]


def test_cache_scan_factory_creates_owned_session():
    parent = cache_scan.QObject()

    session = cache_scan.CacheScanSessionFactory().create(
        "cache/media",
        parent=parent,
    )

    assert isinstance(session, cache_scan.CacheScanSession)
    assert session.parent() is parent
    session.cancel()


def test_cache_scan_combines_local_publication_assets_without_http_markers(tmp_path):
    from solin.core.media.cache_listing import CachedMediaItem

    asset = CachedMediaItem(str(tmp_path / "image.jpg"), "image.jpg", "Study image", 8, "image")
    results = []
    worker = cache_scan._CacheScanWorker(tmp_path / "empty", lambda: [asset])
    worker.results_ready.connect(results.append)

    worker.run()

    assert results == [[asset]]


def test_cancelled_cache_scan_does_not_read_additional_sources(tmp_path):
    calls = []
    worker = cache_scan._CacheScanWorker(tmp_path, lambda: calls.append("read") or [])
    worker.cancel()

    worker.run()

    assert not calls
