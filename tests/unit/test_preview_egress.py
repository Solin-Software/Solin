"""Cross-platform preview egress: channel both-directions + app-side reader."""
from __future__ import annotations

import time

from PySide6.QtCore import QCoreApplication

from solin.controllers.shared_memory_preview_egress import (
    SharedMemoryPreviewEgressController,
)
from solin.core.scenes.content_frame_channel import (
    SharedFrameChannelReader,
    SharedFrameChannelWriter,
)


def _app():
    return QCoreApplication.instance() or QCoreApplication([])


def test_channel_reader_creates_and_writer_attaches_roundtrip():
    reader = SharedFrameChannelReader(None, 4, 2, create=True)  # app owns the block
    try:
        writer = SharedFrameChannelWriter(4, 2, name=reader.name, create=False)  # sidecar attaches
        try:
            assert reader.read_latest() is None  # nothing written yet
            writer.write(bytes([9, 8, 7, 6] * 8), stride=16)
            frame = reader.read_latest()
            assert frame is not None
            assert frame.width == 4 and frame.height == 2 and frame.stride == 16
            assert bytes(frame.data[:4]) == bytes([9, 8, 7, 6])
            assert reader.read_latest() is None  # already delivered
        finally:
            writer.close()
    finally:
        reader.close()
        reader.unlink()


def test_attaching_writer_requires_a_name():
    import pytest

    with pytest.raises(ValueError):
        SharedFrameChannelWriter(4, 2, create=False)


def test_attaching_process_does_not_unlink_the_creators_block():
    # An attaching process (the sidecar) must NOT shm_unlink the creator's block
    # when it exits — otherwise the channel breaks on every sidecar restart.
    import subprocess
    import sys
    import time

    reader = SharedFrameChannelReader(None, 4, 2, create=True)
    name = reader.name
    try:
        child = subprocess.run(
            [sys.executable, "-c",
             "from solin.core.scenes.content_frame_channel import SharedFrameChannelWriter;"
             f"w=SharedFrameChannelWriter(4,2,name={name!r},create=False);"
             "w.write(bytes([1,2,3,4]*8), stride=16); w.close()"],
            capture_output=True, text=True, timeout=30,
        )
        assert child.returncode == 0, child.stderr
        time.sleep(0.8)  # let the child's resource_tracker act (if it wrongly would)
        # the block survived: it still holds the child's frame and re-attaches
        frame = reader.read_latest()
        assert frame is not None and bytes(frame.data[:4]) == bytes([1, 2, 3, 4])
        again = SharedFrameChannelWriter(4, 2, name=name, create=False)
        again.close()
    finally:
        reader.close()
        reader.unlink()


def test_preview_egress_controller_emits_written_frames():
    _app()
    controller = SharedMemoryPreviewEgressController(4, 2)
    try:
        descriptor = controller.descriptor
        assert descriptor is not None
        assert descriptor.transport.value == "shared_memory_bgra"
        assert descriptor.width == 4 and descriptor.height == 2

        frames: list = []
        controller.frame_ready.connect(frames.append)

        # The sidecar attaches as a writer and pushes a green BGRA frame.
        app = _app()
        writer = SharedFrameChannelWriter(4, 2, name=descriptor.handle_token, create=False)
        try:
            writer.write(bytes([0, 255, 0, 255] * 8), stride=16)  # BGRA green
            deadline = time.monotonic() + 2.0
            while not frames and time.monotonic() < deadline:
                app.processEvents()  # deliver the queued cross-thread emit
                time.sleep(0.01)
        finally:
            writer.close()

        assert frames, "no preview frame was emitted"
        image = frames[-1]
        assert image.width() == 4 and image.height() == 2
    finally:
        controller.close()


def test_egress_controller_honours_channel_id():
    _app()
    controller = SharedMemoryPreviewEgressController(4, 2, channel_id="solin-program")
    try:
        assert controller.descriptor.channel_id == "solin-program"
    finally:
        controller.close()


def test_preview_egress_controller_reconfigure_changes_descriptor():
    _app()
    controller = SharedMemoryPreviewEgressController(4, 2)
    try:
        first = controller.descriptor.handle_token
        controller.reconfigure(8, 4)
        assert controller.descriptor.width == 8 and controller.descriptor.height == 4
        assert controller.descriptor.handle_token != first  # a fresh block
    finally:
        controller.close()
