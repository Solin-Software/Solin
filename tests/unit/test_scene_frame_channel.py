from __future__ import annotations

import os
import struct
import sys
from multiprocessing import shared_memory

import pytest

from solin.core.scenes.engine import FrameChannelTransport, FrameProducerKind
from solin.core.scenes.frame_channel import (
    FRAME_CHANNEL_HEADER_SIZE,
    FRAME_CHANNEL_MAGIC,
    FRAME_CHANNEL_SLOT_COUNT,
    FRAME_CHANNEL_SLOT_HEADER_SIZE,
    FRAME_CHANNEL_VERSION,
    FrameChannelUnavailableError,
    SharedMemoryBgraFramePublisher,
    SharedMemoryBgraFrameSubscriber,
)


def test_shared_memory_channel_is_an_explicit_platform_backend() -> None:
    if sys.platform == "win32":
        with SharedMemoryBgraFramePublisher(2, 2) as publisher:
            assert publisher.descriptor.transport is FrameChannelTransport.SHARED_MEMORY_BGRA
        return

    with pytest.raises(FrameChannelUnavailableError):
        SharedMemoryBgraFramePublisher(2, 2)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows shared-memory backend")
def test_shared_memory_channel_publishes_a_versioned_latest_bgra_frame() -> None:
    pixels = bytes(range(16))
    with SharedMemoryBgraFramePublisher(2, 2, generation=7) as publisher:
        sequence = publisher.publish(
            pixels,
            presentation_timestamp_ns=123,
            duration_ns=456,
        )
        attached = shared_memory.SharedMemory(
            name=publisher.descriptor.handle_token,
            create=False,
        )
        try:
            header = struct.unpack_from("<8sHHIIIIIQQQ", attached.buf, 0)
            assert header == (
                FRAME_CHANNEL_MAGIC,
                FRAME_CHANNEL_VERSION,
                FRAME_CHANNEL_HEADER_SIZE,
                FRAME_CHANNEL_SLOT_COUNT,
                2,
                2,
                8,
                1,
                7,
                FRAME_CHANNEL_SLOT_HEADER_SIZE + len(pixels),
                FRAME_CHANNEL_HEADER_SIZE
                + FRAME_CHANNEL_SLOT_COUNT
                * (FRAME_CHANNEL_SLOT_HEADER_SIZE + len(pixels)),
            )
            assert struct.unpack_from("<Q", attached.buf, 56)[0] == sequence
            slot = FRAME_CHANNEL_HEADER_SIZE
            assert struct.unpack_from("<QQQQQ", attached.buf, slot)[:4] == (
                sequence * 2,
                sequence,
                123,
                456,
            )
            assert struct.unpack_from("<Q", attached.buf, slot + 40)[0] == len(pixels)
            assert struct.unpack_from("<IIII", attached.buf, slot + 48) == (2, 2, 8, 1)
            assert bytes(
                attached.buf[
                    slot + FRAME_CHANNEL_SLOT_HEADER_SIZE :
                    slot + FRAME_CHANNEL_SLOT_HEADER_SIZE + len(pixels)
                ]
            ) == pixels
        finally:
            attached.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows shared-memory backend")
def test_shared_memory_channel_rejects_mismatched_or_late_frames() -> None:
    publisher = SharedMemoryBgraFramePublisher(2, 2)
    try:
        with pytest.raises(ValueError, match="frame size"):
            publisher.publish(b"too short")
    finally:
        publisher.close()

    with pytest.raises(RuntimeError, match="closed"):
        publisher.publish(bytes(16))


@pytest.mark.skipif(sys.platform != "win32", reason="Windows shared-memory backend")
def test_shared_memory_publisher_skips_leased_slots_and_drops_when_full() -> None:
    pixels = bytes(range(16))
    with SharedMemoryBgraFramePublisher(2, 2) as publisher:
        attached = shared_memory.SharedMemory(
            name=publisher.descriptor.handle_token,
            create=False,
        )
        try:
            slot_size = FRAME_CHANNEL_SLOT_HEADER_SIZE + len(pixels)
            first_slot = FRAME_CHANNEL_HEADER_SIZE
            struct.pack_into("<II", attached.buf, first_slot + 64, 1, os.getpid())

            sequence = publisher.publish(pixels)

            second_slot = FRAME_CHANNEL_HEADER_SIZE + slot_size
            assert sequence == 1
            assert struct.unpack_from("<QQ", attached.buf, second_slot) == (2, 1)

            for slot_index in range(FRAME_CHANNEL_SLOT_COUNT):
                slot = FRAME_CHANNEL_HEADER_SIZE + slot_index * slot_size
                struct.pack_into("<II", attached.buf, slot + 64, 1, os.getpid())
            with pytest.raises(TimeoutError, match="slots are leased"):
                publisher.publish(pixels)

            struct.pack_into("<IIQ", attached.buf, first_slot + 64, 1, 0, 0)
            assert publisher.publish(pixels) == 2
        finally:
            attached.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows shared-memory backend")
def test_shared_memory_channel_accepts_frames_smaller_than_its_capacity() -> None:
    with SharedMemoryBgraFramePublisher(4, 4) as publisher:
        sequence = publisher.publish(
            bytes(4 * 2 * 4),
            frame_width=4,
            frame_height=2,
        )
        attached = shared_memory.SharedMemory(
            name=publisher.descriptor.handle_token,
            create=False,
        )
        try:
            slot = FRAME_CHANNEL_HEADER_SIZE + (
                (sequence - 1) % FRAME_CHANNEL_SLOT_COUNT
            ) * (FRAME_CHANNEL_SLOT_HEADER_SIZE + 4 * 4 * 4)
            assert struct.unpack_from("<QIIII", attached.buf, slot + 40) == (
                4 * 2 * 4,
                4,
                2,
                16,
                1,
            )
        finally:
            attached.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows shared-memory backend")
def test_shared_memory_subscriber_reads_native_compositor_frames() -> None:
    subscriber = SharedMemoryBgraFrameSubscriber(2, 2, generation=11)
    attached = shared_memory.SharedMemory(
        name=subscriber.descriptor.handle_token,
        create=False,
    )
    try:
        assert not subscriber.wait_for_frame(0)
        subscriber.wake()
        assert subscriber.wait_for_frame(10)
        pixels = bytes(reversed(range(16)))
        sequence = 1
        slot = FRAME_CHANNEL_HEADER_SIZE
        struct.pack_into("<QQQQQ", attached.buf, slot, 1, sequence, 123, 456, 789)
        struct.pack_into("<Q", attached.buf, slot + 40, len(pixels))
        struct.pack_into("<IIII", attached.buf, slot + 48, 2, 2, 8, 1)
        attached.buf[
            slot + FRAME_CHANNEL_SLOT_HEADER_SIZE :
            slot + FRAME_CHANNEL_SLOT_HEADER_SIZE + len(pixels)
        ] = pixels
        struct.pack_into("<Q", attached.buf, slot, sequence * 2)
        struct.pack_into("<Q", attached.buf, 56, sequence)

        frame = subscriber.read_latest()

        assert subscriber.descriptor.producer_kind is FrameProducerKind.NATIVE_COMPOSITOR
        assert frame is not None
        assert frame.sequence == sequence
        assert frame.produced_monotonic_ns == 789
        assert frame.pixels == pixels
        assert subscriber.read_latest() is None
    finally:
        attached.close()
        subscriber.close()
