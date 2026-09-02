"""Unit tests for the cross-platform shared-memory content frame channel."""

from __future__ import annotations

import struct
from multiprocessing import shared_memory

import pytest

from solin.core.scenes.content_frame_channel import (
    SharedFrameChannelReader,
    SharedFrameChannelWriter,
)


def test_write_then_read_roundtrip_and_dedup():
    writer = SharedFrameChannelWriter(4, 2)  # 4x2 BGRA = 32 bytes
    try:
        reader = SharedFrameChannelReader(writer.name, 4, 2)
        try:
            assert reader.read_latest() is None  # nothing written yet

            payload = bytes(range(32))
            writer.write(payload)
            frame = reader.read_latest()
            assert frame is not None
            assert frame.data == payload
            assert (frame.width, frame.height, frame.stride) == (4, 2, 16)

            # Re-reading the same frame returns None (only new frames are delivered).
            assert reader.read_latest() is None

            payload2 = bytes(range(100, 132))
            writer.write(payload2)
            frame2 = reader.read_latest()
            assert frame2 is not None and frame2.data == payload2
        finally:
            reader.close()
    finally:
        writer.close()
        writer.unlink()


def test_reader_skips_a_frame_while_the_writer_is_mid_write():
    writer = SharedFrameChannelWriter(2, 2)
    try:
        reader = SharedFrameChannelReader(writer.name, 2, 2)
        try:
            writer.write(bytes(16))
            assert reader.read_latest() is not None
            # Simulate the writer being mid-frame: an odd sequence number.
            struct.pack_into("<Q", writer._buf, 8, 99)
            assert reader.read_latest() is None
        finally:
            reader.close()
    finally:
        writer.close()
        writer.unlink()


def test_custom_stride_is_reported():
    writer = SharedFrameChannelWriter(2, 2)
    try:
        reader = SharedFrameChannelReader(writer.name, 2, 2)
        try:
            writer.write(bytes(16), stride=8)
            frame = reader.read_latest()
            assert frame is not None and frame.stride == 8
        finally:
            reader.close()
    finally:
        writer.close()
        writer.unlink()


def test_reader_rejects_a_foreign_block():
    block = shared_memory.SharedMemory(create=True, size=128)
    try:
        with pytest.raises(ValueError):
            SharedFrameChannelReader(block.name, 2, 2)
    finally:
        block.close()
        block.unlink()


# ── app-side content publisher (produces frames + the descriptor) ─────────────


def test_content_publisher_roundtrips_a_frame_to_a_reader():
    from solin.core.scenes.content_frame_publisher import SharedMemoryContentPublisher
    from solin.core.scenes.engine import FrameChannelTransport
    from solin.core.scenes.model import VideoPixelFormat

    publisher = SharedMemoryContentPublisher(2, 2)
    try:
        descriptor = publisher.descriptor
        assert descriptor.transport is FrameChannelTransport.SHARED_MEMORY_BGRA
        assert descriptor.pixel_format is VideoPixelFormat.BGRA
        assert (descriptor.width, descriptor.height) == (2, 2)

        reader = SharedFrameChannelReader(descriptor.handle_token, 2, 2)
        try:
            red = b"\x00\x00\xff\xff" * 4  # BGRA red, 2x2
            seq = publisher.publish(red, frame_width=2, frame_height=2)
            assert seq == 1
            frame = reader.read_latest()
            assert frame is not None and frame.data == red
        finally:
            reader.close()
    finally:
        publisher.close()


def test_content_publisher_rejects_non_bgra_planes():
    from solin.core.scenes.content_frame_publisher import SharedMemoryContentPublisher
    from solin.core.scenes.model import VideoPixelFormat

    publisher = SharedMemoryContentPublisher(2, 2)
    try:
        with pytest.raises(ValueError):
            publisher.publish_planes(
                (b"\x00" * 4, b"\x00" * 4),
                plane_strides=(2, 2),
                frame_width=2,
                frame_height=2,
                pixel_format=VideoPixelFormat.NV12,
            )
    finally:
        publisher.close()
