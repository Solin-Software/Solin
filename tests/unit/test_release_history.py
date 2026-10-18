"""Release history contracts and shared update/history transport behavior."""

from __future__ import annotations

import base64
import gc
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from threading import Event
from types import SimpleNamespace

import pytest

from solin.core.network.http import HttpError, HttpStatusError
from solin.core.releases.channel import UpdateChannel
from solin.core.remote import github_updates
from solin.core.remote.github_updates import GitHubReleases
from solin.core.remote.release_history import ReleaseHistoryCursorError
from tests.unit.test_github_updates import Response, payload, published


class Repository:
    def __init__(self):
        self.manifests = {}
        self.releases = []
        self.requests = []
        self.errors = {}

    def add(self, value):
        manifest = payload(value)
        self.manifests[manifest["tag"]] = manifest
        self.releases.append(published(manifest))
        return manifest

    def get(self, url, **kwargs):
        self.requests.append((url, kwargs))
        is_list = "api.github" in url
        key = "list" if is_list else url.split("/")[-2]
        error = self.errors.get(key)
        if error is not None:
            raise error
        if is_list and kwargs["headers"].get("If-None-Match") == "catalog":
            return Response(status=304)
        data = self.releases if is_list else self.manifests[key]
        return Response(json.dumps(data).encode(), headers={"ETag": "catalog" if is_list else key})

    @property
    def manifest_requests(self):
        return [url for url, _kwargs in self.requests if "api.github" not in url]


@pytest.fixture
def repository(monkeypatch):
    source = Repository()
    monkeypatch.setattr(github_updates, "stream_get", source.get)
    return source


def versions(page):
    return [item.version.canonical for item in page.items]


@pytest.mark.parametrize("count", [0, 1, 5, 6, 13])
def test_history_pages_numeric_order_without_refetch_or_eager_manifests(repository, tmp_path, count):
    # GitHub's publication order is deliberately unrelated to version order.
    for patch in list(range(0, count, 2)) + list(range(1, count, 2)):
        repository.add(f"26.32.{patch}")
    client = GitHubReleases(tmp_path)
    page = client.history()
    assert versions(page) == [f"26.32.{patch}" for patch in reversed(range(max(0, count - 5), count))]
    assert len(repository.manifest_requests) == min(count, 6)
    items = list(page.items)
    while page.next_cursor is not None:
        # Continuation also works from a newly constructed client.
        page = GitHubReleases(tmp_path).history(cursor=page.next_cursor)
        assert 1 <= len(page.items) <= 5
        items.extend(page.items)
    assert [item.version.canonical for item in items] == [f"26.32.{patch}" for patch in reversed(range(count))]
    assert len(repository.manifest_requests) == count
    assert len(repository.requests) == count + 1


def test_history_filters_before_pagination_and_beta_order_is_numeric(repository, tmp_path):
    for value in ("26.32.0b2", "26.31.0", "26.32.0b10", "26.32.0", "26.33.0b1", "26.30.0"):
        repository.add(value)
    client = GitHubReleases(tmp_path)
    stable = client.history(page_size=2)
    assert versions(stable) == ["26.32.0", "26.31.0"]
    assert versions(client.history(page_size=2, cursor=stable.next_cursor)) == ["26.30.0"]
    beta = client.history(include_beta=True)
    assert versions(beta) == ["26.33.0b1", "26.32.0", "26.32.0b10", "26.32.0b2", "26.31.0"]
    assert versions(client.history(include_beta=True, cursor=beta.next_cursor)) == ["26.30.0"]


def test_history_sorts_across_github_pages_before_fetching_manifests(repository, tmp_path, monkeypatch):
    for patch in range(7):
        repository.add(f"26.32.{patch}")
    original = repository.get
    list_requests = []

    def get(url, **kwargs):
        if "api.github" not in url:
            return original(url, **kwargs)
        list_requests.append(url)
        # A newer semantic version may have been published much earlier.
        data = repository.releases[:3] + [{"draft": True}] * 97 if url.endswith("page=1") else repository.releases[3:]
        return Response(json.dumps(data).encode())

    monkeypatch.setattr(github_updates, "stream_get", get)
    page = GitHubReleases(tmp_path).history()
    assert versions(page) == ["26.32.6", "26.32.5", "26.32.4", "26.32.3", "26.32.2"]
    assert len(repository.manifest_requests) == 6
    assert len(list_requests) == 2


def test_github_list_pagination_is_bounded(monkeypatch, tmp_path):
    requests = []

    def get(url, **kwargs):
        requests.append(url)
        return Response(json.dumps([{"draft": True}] * 100).encode())

    monkeypatch.setattr(github_updates, "stream_get", get)
    page = GitHubReleases(tmp_path).history()
    assert not page.items and page.next_cursor is None
    assert len(requests) == 10


def test_history_uses_only_own_notes_and_locale_fallback(repository, tmp_path):
    older = repository.add("26.31.0")
    latest = repository.add("26.32.0")
    latest["notes"].insert(0, older["notes"][0])
    latest["notes"][-1]["translations"]["pt_BR"] = "Localized own changes"
    page = GitHubReleases(tmp_path).history(language="pt_BR")
    assert versions(page) == ["26.32.0", "26.31.0"]
    assert page.items[0].markdown == latest["notes"][-1]["translations"]["pt_BR"]
    assert page.items[0].language == "pt_BR"
    assert page.items[1].markdown == older["notes"][0]["translations"]["en"]
    assert page.items[1].language == "en"
    with pytest.raises(FrozenInstanceError):
        page.next_cursor = "modified"
    with pytest.raises(FrozenInstanceError):
        page.items[0].markdown = "modified"


def test_history_ignores_current_version_and_platform_eligibility(repository, tmp_path):
    repository.add("26.31.0")
    client = GitHubReleases(tmp_path)
    assert client.discover("26.40.0", UpdateChannel.STABLE) == []
    assert not repository.manifest_requests
    assert versions(client.history()) == ["26.31.0"]


@pytest.mark.parametrize("first", ["history", "discover"])
def test_update_and_history_share_persisted_validated_manifests(repository, tmp_path, first):
    repository.add("26.32.0")
    if first == "history":
        assert GitHubReleases(tmp_path).history().items
        assert GitHubReleases(tmp_path).discover("26.31.0", UpdateChannel.STABLE)
    else:
        assert GitHubReleases(tmp_path).discover("26.31.0", UpdateChannel.STABLE)
        assert GitHubReleases(tmp_path).history().items
    assert len(repository.manifest_requests) == 1
    list_requests = [kwargs for url, kwargs in repository.requests if "api.github" in url]
    assert len(list_requests) == 2
    assert "If-None-Match" not in list_requests[0]["headers"]
    assert list_requests[1]["headers"]["If-None-Match"] == "catalog"
    cached = json.loads((tmp_path / "github.json").read_text(encoding="utf-8"))
    assert "history_snapshots" in cached
    assert not list(tmp_path.glob("*.tmp"))


def test_continuation_keeps_snapshot_when_another_query_refreshes_catalog(repository, tmp_path):
    for patch in range(7):
        repository.add(f"26.32.{patch}")
    client = GitHubReleases(tmp_path)
    first = client.history()
    repository.add("26.33.0")
    # Fake repository uses ETags; invalidate the list ETag to model new content.
    for entry in client._cache.data.values():
        if isinstance(entry, dict) and entry.get("etag") == "catalog":
            entry["etag"] = "old"
    assert versions(client.history())[0] == "26.33.0"
    assert versions(client.history(cursor=first.next_cursor)) == ["26.32.1", "26.32.0"]


@pytest.mark.parametrize("status", [403, 429])
@pytest.mark.parametrize("location", ["list", "26.32.0"])
def test_rate_limits_are_visible_and_shared_with_preexisting_clients(
    repository, tmp_path, status, location,
):
    repository.add("26.32.0")
    history_client, update_client = GitHubReleases(tmp_path), GitHubReleases(tmp_path)
    repository.errors[location] = HttpStatusError("https://github.com", status, "limited")
    with pytest.raises(HttpStatusError):
        history_client.history()
    count = len(repository.requests)
    assert update_client.discover("26.31.0", UpdateChannel.STABLE) == []
    with pytest.raises(HttpError, match="rate limited"):
        GitHubReleases(tmp_path).history()
    assert len(repository.requests) == count
    assert json.loads((tmp_path / "github.json").read_text())["retry_after"] > 0


def test_next_page_transient_failure_can_retry_same_cursor(repository, tmp_path):
    for patch in range(8):
        repository.add(f"26.32.{patch}")
    client = GitHubReleases(tmp_path)
    first = client.history()
    repository.errors["26.32.1"] = HttpError("offline")
    with pytest.raises(HttpError, match="offline"):
        client.history(cursor=first.next_cursor)
    del repository.errors["26.32.1"]
    retry = GitHubReleases(tmp_path).history(cursor=first.next_cursor)
    assert versions(first) == ["26.32.7", "26.32.6", "26.32.5", "26.32.4", "26.32.3"]
    assert versions(retry) == ["26.32.2", "26.32.1", "26.32.0"]
    assert retry.next_cursor is None
    assert len(repository.manifest_requests) == 9  # Only the failed request is repeated.


def test_transient_lookahead_failure_preserves_complete_page(repository, tmp_path):
    for patch in range(6):
        repository.add(f"26.32.{patch}")
    repository.errors["26.32.0"] = HttpError("offline")
    first = GitHubReleases(tmp_path).history()
    assert versions(first) == ["26.32.5", "26.32.4", "26.32.3", "26.32.2", "26.32.1"]
    assert first.next_cursor is not None
    with pytest.raises(HttpError, match="offline"):
        GitHubReleases(tmp_path).history(cursor=first.next_cursor)
    repository.errors.clear()
    assert versions(GitHubReleases(tmp_path).history(cursor=first.next_cursor)) == ["26.32.0"]


def test_lookahead_rate_limit_is_visible_even_after_complete_page(repository, tmp_path):
    for patch in range(6):
        repository.add(f"26.32.{patch}")
    repository.errors["26.32.0"] = HttpStatusError("https://github.com", 429, "limited")
    with pytest.raises(HttpStatusError):
        GitHubReleases(tmp_path).history()


def test_lookahead_cancellation_is_not_masked_by_complete_page(repository, tmp_path, monkeypatch):
    for patch in range(6):
        repository.add(f"26.32.{patch}")
    original = repository.get
    cancelled = Event()

    def get(url, **kwargs):
        if "/26.32.0/" in url:
            cancelled.set()
            raise HttpError("cancelled")
        return original(url, **kwargs)

    monkeypatch.setattr(github_updates, "stream_get", get)
    with pytest.raises(HttpError, match="cancelled"):
        GitHubReleases(tmp_path, cancelled).history()


@pytest.mark.parametrize("failure", ["invalid_manifest", "wrong_identity", "missing", "inventory"])
def test_invalid_releases_are_skipped_before_page_is_filled(repository, tmp_path, failure):
    for patch in range(7):
        repository.add(f"26.32.{patch}")
    if failure == "invalid_manifest":
        repository.manifests["26.32.6"]["schema_version"] = 99
    elif failure == "wrong_identity":
        repository.manifests["26.32.6"] = payload("26.33.0")
    elif failure == "missing":
        repository.errors["26.32.6"] = HttpStatusError("https://github.com", 404, "removed")
    else:
        repository.releases[-1]["assets"][0]["size"] += 1
    page = GitHubReleases(tmp_path).history()
    assert versions(page) == [f"26.32.{patch}" for patch in (5, 4, 3, 2, 1)]
    assert versions(GitHubReleases(tmp_path).history(cursor=page.next_cursor)) == ["26.32.0"]


@pytest.mark.parametrize("cursor", ["", "invalid", "W10=", "//=="])
def test_invalid_cursors_fail_locally_with_typed_error(repository, tmp_path, cursor):
    with pytest.raises(ReleaseHistoryCursorError):
        GitHubReleases(tmp_path).history(cursor=cursor)
    assert not repository.requests


def test_cursor_rejects_filter_locale_and_snapshot_changes(repository, tmp_path):
    for patch in range(6):
        repository.add(f"26.32.{patch}")
    client = GitHubReleases(tmp_path)
    first = client.history()
    for kwargs in ({"include_beta": True}, {"language": "pt_BR"}):
        with pytest.raises(ReleaseHistoryCursorError):
            client.history(cursor=first.next_cursor, **kwargs)
    # Simulate eviction/corruption of a persistent snapshot.
    client._cache.data["history_snapshots"].clear()
    with pytest.raises(ReleaseHistoryCursorError, match="expired"):
        client.history(cursor=first.next_cursor)
    assert client.history().items  # A fresh query recovers.


def test_cursor_expiry_is_bounded_and_recoverable(repository, tmp_path):
    for patch in range(6):
        repository.add(f"26.32.{patch}")
    client = GitHubReleases(tmp_path)
    first = client.history()
    for patch in range(6, 14):
        repository.add(f"26.32.{patch}")
        for entry in client._cache.data.values():
            if isinstance(entry, dict) and entry.get("etag") == "catalog":
                entry["etag"] = "old"
        client.history()
    with pytest.raises(ReleaseHistoryCursorError, match="expired"):
        client.history(cursor=first.next_cursor)
    cached = json.loads((tmp_path / "github.json").read_text())
    assert len(cached["history_snapshots"]) == 8


@pytest.mark.parametrize("offset", [-1, True, 100])
def test_cursor_rejects_invalid_offsets(repository, tmp_path, offset):
    for patch in range(6):
        repository.add(f"26.32.{patch}")
    client = GitHubReleases(tmp_path)
    first = client.history()
    token = json.loads(base64.urlsafe_b64decode(first.next_cursor))
    token[2] = offset
    cursor = base64.urlsafe_b64encode(json.dumps(token).encode()).decode()
    with pytest.raises(ReleaseHistoryCursorError):
        client.history(cursor=cursor)


@pytest.mark.parametrize("size", [0, -1, 101, True, 1.5])
def test_invalid_page_sizes_fail_without_network(repository, tmp_path, size):
    with pytest.raises(ValueError, match="page size"):
        GitHubReleases(tmp_path).history(page_size=size)
    assert not repository.requests


def test_cancelled_queries_do_not_use_network(repository, tmp_path):
    cancelled = Event()
    cancelled.set()
    with pytest.raises(HttpError, match="cancelled"):
        GitHubReleases(tmp_path, cancelled).history()
    assert not repository.requests


def test_query_deadline_and_per_request_timeout_are_bounded(monkeypatch, tmp_path):
    now = [0.0]
    requests = []
    monkeypatch.setattr(
        github_updates, "time", SimpleNamespace(monotonic=lambda: now[0], time=lambda: 0),
    )

    def get(url, **kwargs):
        requests.append(kwargs["timeout"])
        now[0] += 6
        return Response(json.dumps([{"draft": True}] * 100).encode())

    monkeypatch.setattr(github_updates, "stream_get", get)
    with pytest.raises(HttpError, match="timed out"):
        GitHubReleases(tmp_path).history()
    assert requests == [5.0, 5.0, 5.0, 2.0]


def test_missing_persistent_cache_resets_shared_backoff(repository, tmp_path):
    client = GitHubReleases(tmp_path)
    repository.errors["list"] = HttpStatusError("https://github.com", 429, "limited")
    with pytest.raises(HttpStatusError):
        client.history()
    (tmp_path / "github.json").unlink()
    repository.errors.clear()
    assert not client.history().items
    assert len(repository.requests) == 2


def test_atomic_writer_is_used_and_bad_persistent_cache_is_recoverable(repository, tmp_path, monkeypatch):
    (tmp_path / "github.json").write_text("invalid JSON", encoding="utf-8")
    repository.add("26.32.0")
    writes = []
    original = github_updates.write_json_atomic

    def write(path, data):
        writes.append(path)
        original(path, data)

    monkeypatch.setattr(github_updates, "write_json_atomic", write)
    assert GitHubReleases(tmp_path).history().items
    assert writes == [tmp_path / "github.json"]


def test_shared_cache_serializes_queries_without_losing_manifests(repository, tmp_path, monkeypatch):
    repository.add("26.32.0")
    first_request, release_request, second_started = Event(), Event(), Event()
    original = repository.get

    def get(url, **kwargs):
        if not first_request.is_set():
            first_request.set()
            assert release_request.wait(5)
        return original(url, **kwargs)

    monkeypatch.setattr(github_updates, "stream_get", get)
    history_client, update_client = GitHubReleases(tmp_path), GitHubReleases(tmp_path)

    def discover():
        second_started.set()
        return update_client.discover("26.31.0", UpdateChannel.STABLE)

    with ThreadPoolExecutor(max_workers=2) as executor:
        history = executor.submit(history_client.history)
        try:
            assert first_request.wait(5)
            update = executor.submit(discover)
            assert second_started.wait(5)
        finally:
            release_request.set()
        assert history.result(timeout=5).items
        assert update.result(timeout=5)
    assert len(repository.manifest_requests) == 1
    cache = json.loads((tmp_path / "github.json").read_text())
    assert "history_snapshots" in cache
    assert sum(isinstance(value, dict) and value.get("validated") is True for value in cache.values()) == 1


def test_waiting_for_cache_lock_is_cancellable(repository, tmp_path, monkeypatch):
    entered, release, cancelled = Event(), Event(), Event()

    def blocked_get(url, **kwargs):
        entered.set()
        assert release.wait(5)
        return Response(b"[]")

    monkeypatch.setattr(github_updates, "stream_get", blocked_get)
    first, waiting = GitHubReleases(tmp_path), GitHubReleases(tmp_path, cancelled)
    with ThreadPoolExecutor(max_workers=2) as executor:
        initial = executor.submit(first.history)
        try:
            assert entered.wait(5)
            continuation = executor.submit(waiting.history)
            cancelled.set()
            with pytest.raises(HttpError, match="cancelled"):
                continuation.result(timeout=2)
        finally:
            release.set()
        assert not initial.result(timeout=5).items


def test_read_only_persistent_cache_does_not_prevent_queries(repository, tmp_path, monkeypatch):
    repository.add("26.32.0")

    def denied(*args, **kwargs):
        raise PermissionError("read only")

    monkeypatch.setattr(github_updates, "write_json_atomic", denied)
    client = GitHubReleases(tmp_path)
    assert client.history().items
    assert client.discover("26.31.0", UpdateChannel.STABLE)
    assert len(repository.manifest_requests) == 1


def test_read_only_cache_keeps_pagination_across_collected_clients(repository, tmp_path, monkeypatch):
    for patch in range(7):
        repository.add(f"26.32.{patch}")

    def denied(*args, **kwargs):
        raise PermissionError("read only")

    monkeypatch.setattr(github_updates, "write_json_atomic", denied)
    first = GitHubReleases(tmp_path).history()
    gc.collect()
    second = GitHubReleases(tmp_path).history(cursor=first.next_cursor)
    assert versions(second) == ["26.32.1", "26.32.0"]
    assert len(repository.manifest_requests) == 7
