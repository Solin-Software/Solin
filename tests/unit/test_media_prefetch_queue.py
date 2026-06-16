from __future__ import annotations

from solin.core.media.application import MediaPrefetchQueue, PrefetchPlan
from tests._paths import REPO_ROOT


class _Lookup:
    def __init__(self) -> None:
        self.cached: set[str] = set()

    def is_remote(self, url: str) -> bool:
        return url.startswith("http")

    def is_cached(self, url: str) -> bool:
        return url in self.cached


def _urls(count: int) -> list[str]:
    return [f"https://cdn.example/media-{index}.mp4" for index in range(count)]


def _action_urls(plan: PrefetchPlan, kind: str) -> list[str]:
    return [action.url for action in plan.actions if action.kind == kind]


def _action_messages(plan: PrefetchPlan, kind: str) -> list[str]:
    return [action.message for action in plan.actions if action.kind == kind]


def test_prefetch_many_deduplicates_limits_concurrency_and_reports_counts() -> None:
    queue = MediaPrefetchQueue(_Lookup(), max_concurrent=2)
    urls = _urls(4)

    plan = queue.prefetch_many([urls[0], urls[1], urls[2], urls[0]], "batch")

    assert plan.added == 3
    assert _action_urls(plan, "queued") == urls[:3]
    assert _action_urls(plan, "start") == urls[:2]
    assert queue.batch_counts("batch") == (1, 2, 0, 0)
    assert queue.is_prefetching(urls[0])
    assert queue.is_queued(urls[2])


def test_priority_prefetch_promotes_a_queued_url_to_the_next_slot() -> None:
    queue = MediaPrefetchQueue(_Lookup(), max_concurrent=1)
    urls = _urls(3)
    queue.prefetch_many(urls, "batch")

    queue.prefetch(urls[2], priority=True)
    plan = queue.complete(urls[0])

    assert _action_urls(plan, "start") == [urls[2]]
    assert queue.batch_counts("batch") == (1, 1, 1, 0)


def test_temporary_failure_retries_once_before_reporting_error() -> None:
    queue = MediaPrefetchQueue(_Lookup(), max_concurrent=1)
    url = _urls(1)[0]
    queue.prefetch_many([url], "batch")

    first_failure = queue.fail(url, "temporary")
    second_failure = queue.fail(url, "temporary")

    assert _action_urls(first_failure, "error") == []
    assert _action_urls(first_failure, "start") == [url]
    assert _action_urls(second_failure, "error") == [url]
    assert queue.batch_counts("batch") == (0, 0, 0, 1)


def test_fatal_cache_error_aborts_batch_without_retrying_or_starting_more() -> None:
    queue = MediaPrefetchQueue(_Lookup(), max_concurrent=2)
    urls = _urls(4)
    queue.prefetch_many(urls, "batch")

    plan = queue.fail(urls[0], "No space left on device")

    assert _action_urls(plan, "start") == []
    assert _action_urls(plan, "cancel_active") == [urls[1]]
    assert _action_urls(plan, "error") == [urls[0]]
    assert _action_messages(plan, "batch_error") == ["No space left on device"]
    assert queue.batch_counts("batch") == (0, 0, 0, 1)


def test_progress_is_throttled_to_visible_percent_changes() -> None:
    now = 0.0

    def clock() -> float:
        return now

    queue = MediaPrefetchQueue(_Lookup(), max_concurrent=1, clock=clock)
    url = _urls(1)[0]
    queue.prefetch(url)

    first = queue.start_progress(url, 1, 1000)
    repeated_percent = queue.start_progress(url, 2, 1000)
    next_percent = queue.start_progress(url, 10, 1000)

    assert [(a.downloaded, a.total) for a in first.actions] == [(1, 1000)]
    assert repeated_percent.actions == []
    assert [(a.downloaded, a.total) for a in next_percent.actions] == [(10, 1000)]


def test_cached_remote_emits_cache_change_without_queueing_or_starting() -> None:
    lookup = _Lookup()
    url = _urls(1)[0]
    lookup.cached.add(url)
    queue = MediaPrefetchQueue(lookup)

    plan = queue.prefetch(url)

    assert _action_urls(plan, "cache_changed") == [url]
    assert _action_urls(plan, "queued") == []
    assert _action_urls(plan, "start") == []


def test_media_prefetch_queue_application_service_has_no_qt_or_downloader_dependency() -> None:
    source = (
        REPO_ROOT
        / "src"
        / "solin"
        / "core"
        / "media"
        / "application.py"
    ).read_text(encoding="utf-8")

    assert "PySide6" not in source
    assert "QObject" not in source
    assert "Signal" not in source
    assert "SongDownloader" not in source
