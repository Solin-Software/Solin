from pathlib import Path

from PySide6.QtCore import QCoreApplication

from solin.core.jw import thumbnail_fetch
from solin.core.jw.catalog import JWMediaCatalogCachePaths

_APP = QCoreApplication.instance() or QCoreApplication([])


class _ThreadPoolStub:
    instances = []

    def __init__(self, parent=None):
        self.parent = parent
        self.max_thread_count = 0
        self.started = []
        self.clear_count = 0
        self.wait_calls = []
        self.__class__.instances.append(self)

    def setMaxThreadCount(self, count):
        self.max_thread_count = count

    def start(self, worker):
        self.started.append(worker)

    def clear(self):
        self.clear_count += 1

    def waitForDone(self, *args):
        self.wait_calls.append(args)
        return True


def _session(monkeypatch, *, max_concurrency=2):
    _ThreadPoolStub.instances.clear()
    monkeypatch.setattr(thumbnail_fetch, "QThreadPool", _ThreadPoolStub)
    cache_paths = JWMediaCatalogCachePaths(
        Path("cache"),
        Path("thumbs"),
    )
    session = thumbnail_fetch.JWCatalogThumbnailSession(
        cache_paths,
        max_concurrency=max_concurrency,
    )
    return session, _ThreadPoolStub.instances[-1], cache_paths


def test_thumbnail_session_bounds_concurrency_and_deduplicates(monkeypatch):
    downloaded = []
    monkeypatch.setattr(
        thumbnail_fetch,
        "ensure_thumbnail_cached",
        lambda url, *, cache_paths: downloaded.append((url, cache_paths))
        or f"cached/{Path(url).name}",
    )
    session, pool, cache_paths = _session(monkeypatch, max_concurrency=2)
    ready = []
    session.ready.connect(
        lambda item_id, url, path: ready.append((item_id, url, path))
    )

    assert session.enqueue("a", "https://cdn.example/a.jpg") is True
    assert session.enqueue("b", "https://cdn.example/b.jpg") is True
    assert session.enqueue("c", "https://cdn.example/c.jpg") is True
    assert session.enqueue("c", "https://cdn.example/c.jpg") is False
    assert len(pool.started) == 2

    pool.started[0].run()
    assert len(pool.started) == 3
    pool.started[1].run()
    pool.started[2].run()

    assert ready == [
        ("a", "https://cdn.example/a.jpg", "cached/a.jpg"),
        ("b", "https://cdn.example/b.jpg", "cached/b.jpg"),
        ("c", "https://cdn.example/c.jpg", "cached/c.jpg"),
    ]
    assert downloaded == [
        ("https://cdn.example/a.jpg", cache_paths),
        ("https://cdn.example/b.jpg", cache_paths),
        ("https://cdn.example/c.jpg", cache_paths),
    ]


def test_thumbnail_session_fences_stale_results_after_reset(monkeypatch):
    monkeypatch.setattr(
        thumbnail_fetch,
        "ensure_thumbnail_cached",
        lambda url, *, cache_paths: f"cached/{Path(url).name}",
    )
    session, pool, _cache_paths = _session(monkeypatch, max_concurrency=1)
    ready = []
    session.ready.connect(
        lambda item_id, url, path: ready.append((item_id, url, path))
    )

    session.enqueue("old", "https://cdn.example/old.jpg")
    session.reset()
    session.enqueue("new", "https://cdn.example/new.jpg")

    assert pool.clear_count == 0
    assert len(pool.started) == 1
    pool.started[0].run()
    assert ready == []
    assert len(pool.started) == 2

    pool.started[1].run()
    assert ready == [
        ("new", "https://cdn.example/new.jpg", "cached/new.jpg")
    ]


def test_thumbnail_session_allows_new_url_for_same_item(monkeypatch):
    monkeypatch.setattr(
        thumbnail_fetch,
        "ensure_thumbnail_cached",
        lambda url, *, cache_paths: f"cached/{Path(url).name}",
    )
    session, pool, _cache_paths = _session(monkeypatch, max_concurrency=1)
    ready = []
    session.ready.connect(
        lambda item_id, url, path: ready.append((item_id, url, path))
    )

    assert session.enqueue("same", "https://cdn.example/old.jpg") is True
    assert session.enqueue("same", "https://cdn.example/new.jpg") is True

    pool.started[0].run()
    pool.started[1].run()

    assert ready == [
        ("same", "https://cdn.example/old.jpg", "cached/old.jpg"),
        ("same", "https://cdn.example/new.jpg", "cached/new.jpg"),
    ]


def test_thumbnail_session_normalizes_failures_and_closes_once(monkeypatch):
    def _fail(_url, *, cache_paths):
        raise OSError("offline")

    monkeypatch.setattr(thumbnail_fetch, "ensure_thumbnail_cached", _fail)
    session, pool, _cache_paths = _session(monkeypatch, max_concurrency=1)
    ready = []
    session.ready.connect(
        lambda item_id, url, path: ready.append((item_id, url, path))
    )

    session.enqueue("a", "https://cdn.example/a.jpg")
    pool.started[0].run()
    session.close()
    session.close()

    assert ready == [("a", "https://cdn.example/a.jpg", "")]
    assert pool.clear_count == 1
    assert pool.wait_calls == [()]
    assert session.enqueue("b", "https://cdn.example/b.jpg") is False


def test_thumbnail_session_factory_binds_cache_paths(monkeypatch):
    _ThreadPoolStub.instances.clear()
    monkeypatch.setattr(thumbnail_fetch, "QThreadPool", _ThreadPoolStub)
    cache_paths = JWMediaCatalogCachePaths(Path("cache"), Path("thumbs"))
    owner = _APP
    factory = thumbnail_fetch.JWCatalogThumbnailSessionFactory(
        cache_paths,
        max_concurrency=3,
    )

    session = factory.create(parent=owner)

    assert session.parent() is owner
    assert session._cache_paths is cache_paths
    assert _ThreadPoolStub.instances[-1].max_thread_count == 3
