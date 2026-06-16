from __future__ import annotations

from pathlib import Path

import pytest

from solin.core.media.info_queue import MediaInfoScheduler


def test_scheduler_enforces_positive_concurrency():
    with pytest.raises(ValueError, match="at least 1"):
        MediaInfoScheduler(max_concurrent=0)


def test_scheduler_limits_concurrency_and_deduplicates_indices():
    scheduler = MediaInfoScheduler(max_concurrent=2)
    first = scheduler.enqueue(0, "first.mp4", "video")
    duplicate = scheduler.enqueue(0, "ignored.mp4", "video")
    second = scheduler.enqueue(1, "second.mp4", "video")
    third = scheduler.enqueue(2, "third.mp4", "video")

    assert duplicate is first
    assert scheduler.pump() == [first, second]
    assert scheduler.active == {0: first, 1: second}
    assert scheduler.pending == [third]

    assert scheduler.complete(first, 0)
    assert scheduler.pump() == [third]
    assert scheduler.active == {1: second, 2: third}


def test_clear_rejects_results_from_an_older_generation():
    scheduler = MediaInfoScheduler()
    old_job = scheduler.enqueue(0, "old.mp4", "video")
    scheduler.pump()

    assert scheduler.clear() == [old_job]

    new_job = scheduler.enqueue(0, "new.mp4", "video")
    scheduler.pump()

    assert not scheduler.accepts_result(old_job, 0)
    assert not scheduler.complete(old_job, 0)
    assert scheduler.accepts_result(new_job, 0)


def test_invalidate_rejects_a_fast_path_version_for_the_same_index():
    scheduler = MediaInfoScheduler()
    old_version = scheduler.version_for(0)

    scheduler.invalidate(0)

    assert not scheduler.is_current(old_version)
    assert scheduler.is_current(scheduler.version_for(0))


def test_invalidate_retires_active_and_pending_jobs_for_the_index():
    scheduler = MediaInfoScheduler(max_concurrent=1)
    active = scheduler.enqueue(0, "active.mp4", "video")
    scheduler.pump()
    scheduler.enqueue(1, "pending.mp4", "video")

    assert scheduler.invalidate(1) == []
    assert not scheduler.is_scheduled(1)

    assert scheduler.invalidate(0) == [active]
    replacement = scheduler.enqueue(0, "replacement.mp4", "video")

    assert not scheduler.accepts_result(active, 0)
    assert replacement.revision == 1
    assert scheduler.pump() == [replacement]
    assert scheduler.is_scheduled(0)


def test_scheduler_module_has_no_qt_dependency():
    from solin.core.media import info_queue

    source = Path(info_queue.__file__).read_text(encoding="utf-8")

    assert "PySide6" not in source
    assert "QObject" not in source
    assert "Signal" not in source
