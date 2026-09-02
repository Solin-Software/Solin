"""Sidecar-side content ingress: shared-memory frames → a libobs source.

Given a ``content_ingress`` :class:`FrameChannelDescriptor`, this opens the
shared-memory channel (:mod:`content_frame_channel`), creates a
``solin_frame_source`` in the libobs runtime, and pumps BGRA frames from the
channel into that source on a background thread so the app's rendered content
(yeartext, timers, browser, framed images) composites like any other source.

The frame source is exposed via :attr:`source` so the scene builder can place it
for the content layer; the consumer owns its lifetime.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable

log = logging.getLogger(__name__)

# Only this transport is supported cross-platform (see content_frame_channel).
SHARED_MEMORY_BGRA = "shared_memory_bgra"
_POLL_SECONDS = 1.0 / 60.0

FrameSourceFactory = Callable[[Any, str], Any]


def _default_frame_source_factory(runtime: Any, name: str) -> Any:
    from solin.core.media.obs_frame_source import create_frame_source

    return create_frame_source(runtime, name)


class ContentFrameConsumer:
    """Pumps BGRA frames from a shared channel into a libobs frame source."""

    def __init__(
        self,
        runtime: Any,
        descriptor: dict,
        *,
        frame_source_factory: FrameSourceFactory | None = None,
    ) -> None:
        self._runtime = runtime
        self._descriptor = descriptor
        self._frame_source_factory = frame_source_factory or _default_frame_source_factory
        self._reader: Any | None = None
        self._frame_source: Any | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    @property
    def handle_token(self) -> str | None:
        token = self._descriptor.get("handle_token")
        return str(token) if token else None

    @property
    def source(self) -> Any | None:
        return self._frame_source.source if self._frame_source is not None else None

    def start(self) -> bool:
        """Open the channel + frame source and start pumping. False if unusable."""
        from solin.core.scenes.content_frame_channel import SharedFrameChannelReader

        token = self.handle_token
        width = int(self._descriptor.get("width") or 0)
        height = int(self._descriptor.get("height") or 0)
        if not token or width <= 0 or height <= 0:
            return False
        try:
            self._reader = SharedFrameChannelReader(token, width, height)
        except Exception:  # noqa: BLE001 - the app may not have created it yet
            log.warning("Could not open content frame channel %r", token, exc_info=True)
            return False
        self._frame_source = self._frame_source_factory(self._runtime, "solin-content")
        if self._frame_source is None:
            self._reader.close()
            self._reader = None
            return False
        self._thread = threading.Thread(
            target=self._run, name="solin-content-pump", daemon=True
        )
        self._thread.start()
        return True

    def pump_once(self) -> bool:
        """Deliver the newest frame to libobs. True if a frame was pushed."""
        reader = self._reader
        frame_source = self._frame_source
        if reader is None or frame_source is None:
            return False
        frame = reader.read_latest()
        if frame is None:
            return False
        frame_source.push_bgra(frame.data, frame.width, frame.height, frame.stride)
        return True

    def _run(self) -> None:
        while not self._stop.wait(_POLL_SECONDS):
            try:
                self.pump_once()
            except Exception:  # noqa: BLE001 - the pump must never die on one bad frame
                log.debug("content frame pump errored", exc_info=True)

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=1.0)
        frame_source, self._frame_source = self._frame_source, None
        if frame_source is not None:
            try:
                frame_source.release()
            except Exception:  # noqa: BLE001 - libobs boundary
                log.debug("content frame source release errored", exc_info=True)
        reader, self._reader = self._reader, None
        if reader is not None:
            try:
                reader.close()
            except Exception:  # noqa: BLE001 - shm boundary
                log.debug("content frame channel close errored", exc_info=True)
