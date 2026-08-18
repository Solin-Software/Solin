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
