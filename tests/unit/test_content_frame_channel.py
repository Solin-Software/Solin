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


def test_a_frame_smaller_than_the_channel_reports_its_own_size():
    """The header must describe the frame, not the channel it travels through.

    Stamping the channel's dimensions told the sidecar a small picture was a
    full-canvas frame, so the scene item scaled the *canvas* to fit and the
    picture rendered at native size in the corner.
    """
    writer = SharedFrameChannelWriter(4, 4)  # channel capacity: 4x4
    try:
        reader = SharedFrameChannelReader(writer.name, 4, 4)
        try:
            picture = bytes(range(2 * 2 * 4))  # a 2x2 BGRA picture
            writer.write(picture, stride=2 * 4, width=2, height=2)

            frame = reader.read_latest()
            assert frame is not None
            assert (frame.width, frame.height, frame.stride) == (2, 2, 8)
            assert frame.data[: len(picture)] == picture
        finally:
            reader.close()
    finally:
        writer.close()
        writer.unlink()


def test_a_frame_taller_than_the_block_is_never_over_promised():
    """The reader trusts the header, so never claim rows the block cannot hold."""
    writer = SharedFrameChannelWriter(2, 2)  # room for 2 rows of 8 bytes
    try:
        reader = SharedFrameChannelReader(writer.name, 2, 2)
        try:
            writer.write(bytes(64), stride=8, width=2, height=8)  # claims 8 rows

            frame = reader.read_latest()
            assert frame is not None
            assert frame.height == 2  # clamped to what actually fits
            assert frame.height * frame.stride <= len(frame.data)
        finally:
            reader.close()
    finally:
        writer.close()
        writer.unlink()


def test_full_frame_writers_still_describe_the_whole_channel():
    """The egress writers send canvas-sized frames and pass no explicit size."""
    writer = SharedFrameChannelWriter(4, 2)
    try:
        reader = SharedFrameChannelReader(writer.name, 4, 2)
        try:
            writer.write(bytes(32))
            frame = reader.read_latest()
            assert frame is not None
            assert (frame.width, frame.height, frame.stride) == (4, 2, 16)
        finally:
            reader.close()
    finally:
        writer.close()
        writer.unlink()


def test_content_publisher_forwards_the_pictures_own_dimensions():
    from solin.core.scenes.content_frame_publisher import SharedMemoryContentPublisher

    publisher = SharedMemoryContentPublisher(4, 4)
    try:
        reader = SharedFrameChannelReader(publisher.descriptor.handle_token, 4, 4)
        try:
            publisher.publish(bytes(2 * 1 * 4), frame_width=2, frame_height=1)
            frame = reader.read_latest()
            assert frame is not None
            assert (frame.width, frame.height) == (2, 1)
        finally:
            reader.close()
    finally:
        publisher.close()


@pytest.mark.parametrize("planes", [False, True])
def test_content_publisher_preserves_the_presentation_identity(planes: bool):
    from solin.core.scenes.content_frame_publisher import SharedMemoryContentPublisher
    from solin.core.scenes.model import VideoPixelFormat

    publisher = SharedMemoryContentPublisher(2, 2)
    reader = SharedFrameChannelReader(publisher.descriptor.handle_token, 2, 2)
    try:
        for epoch in (1, 2, 2**64 - 1):
            pixels = bytes([epoch % 256]) * 16
            if planes:
                publisher.publish_planes(
                    (pixels,), plane_strides=(8,), frame_width=2, frame_height=2,
                    pixel_format=VideoPixelFormat.BGRA, media_epoch=epoch,
                )
            else:
                publisher.publish(pixels, frame_width=2, frame_height=2, media_epoch=epoch)
            frame = reader.read_latest()
            assert frame is not None
            assert frame.media_epoch == epoch
            assert frame.data == pixels
    finally:
        reader.close()
        publisher.close()
