"""App-side content publisher for the libobs engine (cross-platform).

Implements the :class:`~solin.controllers.content_frame_ingress_controller._FramePublisher`
protocol on top of the cross-platform :class:`SharedFrameChannelWriter`, so
``ContentFrameIngressController`` can push the app's rendered content (yeartext,
timers, browser, framed images) to the libobs sidecar as a ``SHARED_MEMORY_BGRA``
channel. Injected at bootstrap when the libobs engine is selected, in place of
main's Windows-only shared-memory / D3D11 publishers.

Media files are decoded by libobs (``ffmpeg_source``), not pushed through here —
so this channel only ever carries BGRA content frames.
"""

from __future__ import annotations

import logging
from typing import Any

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

log = logging.getLogger(__name__)

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
        pixels: Any,
        *,
        frame_width: int,
        frame_height: int,
        presentation_timestamp_ns: int = 0,
        duration_ns: int = 0,
        media_epoch: int = 0,
    ) -> int:
        return self._write(bytes(pixels), stride=frame_width * 4)

    def publish_planes(
        self,
        planes: tuple[Any, ...],
        *,
        plane_strides: tuple[int, ...],
        frame_width: int,
        frame_height: int,
        pixel_format: VideoPixelFormat,
        presentation_timestamp_ns: int = 0,
        duration_ns: int = 0,
        media_epoch: int = 0,
    ) -> int:
        if pixel_format is not VideoPixelFormat.BGRA:
            raise ValueError("the libobs content channel accepts only BGRA frames")
        return self._write(bytes(planes[0]), stride=plane_strides[0])

    def _write(self, data: bytes, *, stride: int) -> int:
        self._writer.write(data, stride=stride)
        self._sequence += 1
        return self._sequence

    def set_media_epoch(self, media_epoch: int) -> None:
        # The media epoch orders content swaps in main's Windows publisher; the
        # libobs content source shows the latest frame, so nothing to track here.
        pass

    def set_image_transform(
        self,
        transform: Any,
        *,
        media_epoch: int,
        canvas_width: int,
        canvas_height: int,
        animate: bool,
        duration_ms: int,
    ) -> None:
        # Image framing (Ken Burns / letterbox) is applied to the rendered frame
        # before it reaches this channel, so this is a no-op for now.
        pass

    def close(self) -> None:
        try:
            self._writer.close()
        finally:
            self._writer.unlink()
