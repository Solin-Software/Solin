from __future__ import annotations

import multiprocessing
import struct

import pytest

from solin.core.conference.media_ring import MediaRingReader, MediaRingWriter


@pytest.fixture
def ring():
    reader = MediaRingReader(None, slot_count=4, payload_size=16, create=True)
    writer = MediaRingWriter(reader.name)
    yield reader, writer
    writer.close()
    reader.close()
    reader.unlink()


def test_items_arrive_in_order_with_metadata(ring):
    reader, writer = ring
    assert reader.read_next() is None
    for i in range(3):
        writer.write(bytes([i]) * 8, timestamp_ns=1000 + i, meta=i * 10)
    items = [reader.read_next() for _ in range(3)]
    assert [item.number for item in items] == [0, 1, 2]
    assert [item.payload for item in items] == [bytes([i]) * 8 for i in range(3)]
    assert [item.timestamp_ns for item in items] == [1000, 1001, 1002]
    assert [item.meta for item in items] == [0, 10, 20]
    assert reader.read_next() is None
    assert reader.dropped == 0


def test_slow_reader_skips_ahead_and_counts_drops(ring):
    reader, writer = ring
    for i in range(10):
        writer.write(bytes([i]))
    numbers = []
    while (item := reader.read_next()) is not None:
        numbers.append(item.number)
    assert numbers == [7, 8, 9]  # the last slot_count - 1 items survive
    assert reader.dropped == 7


def test_late_joiner_starts_at_the_newest_item(ring):
    reader, writer = ring
    writer.write(b"old")
    late = MediaRingReader(reader.name)
    assert late.read_next() is None
    writer.write(b"new")
    assert late.read_next().payload == b"new"
    late.close()


def test_oversized_payload_is_rejected(ring):
    _, writer = ring
    with pytest.raises(ValueError):
        writer.write(b"x" * 17)


def test_torn_slot_is_not_delivered(ring):
    reader, writer = ring
    writer.write(b"ok")
    # Simulate a writer caught mid-write: make slot 0's sequence odd.
    buf = reader._shm.buf
    (seq,) = struct.unpack_from("<Q", buf, 64)
    struct.pack_into("<Q", buf, 64, seq + 1)
    assert reader.read_next() is None
    struct.pack_into("<Q", buf, 64, seq)
    assert reader.read_next().payload == b"ok"


def test_attaching_to_something_else_fails():
    from multiprocessing import shared_memory

    block = shared_memory.SharedMemory(create=True, size=256)
    try:
        with pytest.raises(ValueError):
            MediaRingReader(block.name)
    finally:
        block.close()
        block.unlink()


def _produce(name: str, count: int) -> None:
    writer = MediaRingWriter(name)
    for i in range(count):
        writer.write(i.to_bytes(4, "little"), timestamp_ns=i)
    writer.close()


def test_cross_process_writer():
    reader = MediaRingReader(None, slot_count=256, payload_size=4, create=True)
    try:
        process = multiprocessing.get_context("spawn").Process(target=_produce, args=(reader.name, 200))
        process.start()
        process.join(30)
        assert process.exitcode == 0
        values = []
        while (item := reader.read_next()) is not None:
            values.append(int.from_bytes(item.payload, "little"))
        assert values == list(range(200))
    finally:
        reader.close()
        reader.unlink()
