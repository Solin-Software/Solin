from __future__ import annotations

from collections import deque
import threading
import time

import pytest

from solin.core.scenes import content_frame_channel, content_frame_consumer
from solin.core.scenes.content_frame_channel import ContentFrame


class _Reader:
    def __init__(self, events: list[str]) -> None:
        self.frames: deque[ContentFrame] = deque()
        self.events = events
        self.second_read = threading.Event()
        self.read_count = 0

    def read_latest(self) -> ContentFrame | None:
        self.read_count += 1
        if self.read_count == 2:
            self.second_read.set()
        self.events.append("read")
        return self.frames.popleft() if self.frames else None

    def close(self) -> None:
        self.events.append("reader closed")


class _Source:
    source = "content-source"

    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.uploads: list[tuple[int, bool]] = []
        self.block = False
        self.entered = threading.Event()
        self.resume = threading.Event()

    def push_bgra(self, data, width, height, stride, *, reset=False) -> bool:
        assert (width, height, stride) == (2, 2, 8)
        self.uploads.append((data[0], reset))
        self.events.append("upload entered")
        if self.block:
            self.entered.set()
            self.resume.wait()
        self.events.append("upload completed")
        return data[0] != 99

    def release(self) -> None:
        self.events.append("source released")


@pytest.fixture
def consumer(monkeypatch):
    events: list[str] = []
    reader = _Reader(events)
    source = _Source(events)
    # Park the automatic pump; each scenario owns its explicit pumping threads.
    monkeypatch.setattr(content_frame_consumer, "_POLL_SECONDS", 60)
    monkeypatch.setattr(content_frame_channel, "SharedFrameChannelReader", lambda *_: reader)
    instance = content_frame_consumer.ContentFrameConsumer(
        None,
        {"handle_token": "unit-content", "width": 2, "height": 2},
        frame_source_factory=lambda *_: source,
    )
    threads: list[threading.Thread] = []
    errors: list[Exception] = []

    def run(call):
        def capture():
            try:
                call()
            except Exception as error:  # noqa: BLE001 - report worker assertions to pytest
                errors.append(error)

        thread = threading.Thread(target=capture)
        threads.append(thread)
        thread.start()
        return thread

    try:
        assert instance.start()
        yield instance, reader, source, events, run
    finally:
        source.resume.set()
        instance.stop()
        for thread in threads:
            thread.join(1)
            assert not thread.is_alive(), "Consumer test left a coordination thread alive"
        assert errors == []


def _frame(value: int, epoch: int) -> ContentFrame:
    return ContentFrame(bytes([value]) * 16, 2, 2, 8, epoch)


def test_epoch_deadline_expires_while_native_upload_is_blocked(consumer):
    instance, reader, source, _events, run = consumer
    reader.frames.append(_frame(1, 1))
    source.block = True
    run(instance.pump_once)
    assert source.entered.wait(1)
    result = []
    returned = threading.Event()

    def wait():
        result.append(instance.wait_for_epoch(1, deadline=time.monotonic() + 0.03))
        returned.set()

    run(wait)
    assert returned.wait(1), "Epoch deadline waited behind an unfinished native upload"
    assert result == [False]
    assert not source.resume.is_set()


def test_stop_wakes_epoch_waiter_and_drains_upload_before_disposal(consumer):
    instance, reader, source, events, run = consumer
    reader.frames.append(_frame(1, 1))
    source.block = True
    pump_result = []
    run(lambda: pump_result.append(instance.pump_once()))
    assert source.entered.wait(1)
    waiter_started = threading.Event()
    waiter_returned = threading.Event()
    result = []

    def wait():
        waiter_started.set()
        result.append(instance.wait_for_epoch(1, deadline=time.monotonic() + 10))
        waiter_returned.set()

    run(wait)
    assert waiter_started.wait(1)
    stopped = threading.Event()

    def stop():
        instance.stop()
        stopped.set()

    run(stop)
    assert waiter_returned.wait(1), "Stop notification waited behind the native upload"
    assert result == [False]
    assert not stopped.is_set()
    assert "source released" not in events
    assert "reader closed" not in events
    source.resume.set()
    assert stopped.wait(1)
    assert pump_result == [False]
    assert events[-3:] == ["upload completed", "source released", "reader closed"]
    assert not instance.pump_once()
    assert not instance.start()
    assert not instance.wait_for_epoch(1, deadline=time.monotonic() + 1)
    assert reader.read_count == 1
    instance.stop()
    assert events.count("source released") == events.count("reader closed") == 1


def test_concurrent_pumps_serialize_reader_and_upload_in_epoch_order(consumer):
    instance, reader, source, _events, run = consumer
    reader.frames.extend((_frame(1, 1), _frame(2, 2)))
    source.block = True
    results = []
    first = run(lambda: results.append(instance.pump_once()))
    assert source.entered.wait(1)
    second_started = threading.Event()

    def pump():
        second_started.set()
        results.append(instance.pump_once())

    second = run(pump)
    assert second_started.wait(1)
    assert not reader.second_read.wait(0.05), "Another reader advanced during the native upload"
    source.resume.set()
    first.join(1)
    second.join(1)
    assert not first.is_alive() and not second.is_alive()
    assert results == [True, True]
    assert source.uploads == [(1, True), (2, True)]
    assert instance.wait_for_epoch(2, deadline=time.monotonic() - 1)
    assert not instance.wait_for_epoch(1, deadline=time.monotonic() + 1)


def test_failed_upload_and_stale_frames_never_publish_an_epoch(consumer):
    instance, reader, source, _events, _run = consumer
    assert not instance.wait_for_epoch(1, deadline=time.monotonic() - 1)
    for value, epoch, expected in (
        (1, 1, True), (2, 1, True), (3, 0, False), (99, 2, False), (4, 2, True),
    ):
        reader.frames.append(_frame(value, epoch))
        assert instance.pump_once() is expected
        if value == 99:
            assert not instance.wait_for_epoch(2, deadline=time.monotonic() - 1)
            assert instance.wait_for_epoch(1, deadline=time.monotonic() - 1)
    assert source.uploads == [(1, True), (2, False), (99, True), (4, True)]
    assert instance.wait_for_epoch(2, deadline=time.monotonic() - 1)
    assert not instance.wait_for_epoch(1, deadline=time.monotonic() + 1)
