"""App-side content publisher for the libobs engine (cross-platform).

Implements the :class:`~solin.controllers.content_frame_ingress_controller._FramePublisher`
protocol on top of the cross-platform :class:`SharedFrameChannelWriter`, so
``ContentFrameIngressController`` can push the app's rendered content (timers,
browser, framed images) to the libobs sidecar as a ``SHARED_MEMORY_BGRA``
channel. This is the default publisher for ``ContentFrameIngressController``.

Media files are decoded by libobs (``ffmpeg_source``), not pushed through here —
so this channel only ever carries BGRA content frames.
"""

from __future__ import annotations

from collections.abc import Buffer

from solin.core.scenes.content_frame_channel import SharedFrameChannelWriter
from solin.core.scenes.engine import (
    FrameChannelDescriptor,
    FrameChannelTransport,
    FrameProducerKind,
)
from solin.core.scenes.model import (
    VideoColorRange,
    VideoColorSpace,
    VideoPixelFormat,
)

_CHANNEL_ID = "solin-content"


class SharedMemoryContentPublisher:
    """Publishes BGRA content frames into a cross-platform shared-memory channel."""

    def __init__(self, width: int, height: int, *, generation: int = 0) -> None:
        self._width = int(width)
        self._height = int(height)
        self._writer = SharedFrameChannelWriter(self._width, self._height)
        self._sequence = 0
        self._descriptor = FrameChannelDescriptor(
            channel_id=_CHANNEL_ID,
            generation=int(generation),
            producer_kind=FrameProducerKind.SOLIN_OFFSCREEN,
            transport=FrameChannelTransport.SHARED_MEMORY_BGRA,
            handle_token=self._writer.name,
            width=self._width,
            height=self._height,
            pixel_format=VideoPixelFormat.BGRA,
            color_space=VideoColorSpace.SRGB,
            color_range=VideoColorRange.FULL,
        )

    @property
    def descriptor(self) -> FrameChannelDescriptor:
        return self._descriptor

    def publish(
        self,
        pixels: Buffer,
        *,
        frame_width: int,
        frame_height: int,
        media_epoch: int = 0,
    ) -> int:
        self._writer.write(
            bytes(pixels), stride=frame_width * 4, width=frame_width,
            height=frame_height, media_epoch=media_epoch,
        )
        self._sequence += 1
        return self._sequence

    def close(self) -> None:
        try:
            self._writer.close()
        finally:
            self._writer.unlink()
