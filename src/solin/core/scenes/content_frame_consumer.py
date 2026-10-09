"""Sidecar-side content ingress: shared-memory frames → a libobs source.

Given a ``content_ingress`` :class:`FrameChannelDescriptor`, this opens the
shared-memory channel (:mod:`content_frame_channel`), creates a
``solin_frame_source`` in the libobs runtime, and pumps BGRA frames from the
channel into that source on a background thread so the app's rendered content
(yeartext, timers, browser, framed images) composites like any other source.

Each presentation epoch has its own frame source. New epochs cannot overwrite
pixels still used by an outgoing scene. The consumer owns the sources; the
control thread collects them after scene and native showing references retire.
"""

from __future__ import annotations

import logging
import threading
import time
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
        self._sources: list[Any] = []
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        # Native uploads can wait for the graphics context. Keep their resource
        # lifetime separate from the condition used for IPC deadlines.
        self._pump_lock = threading.Lock()
        self._condition = threading.Condition()
        self._media_epoch: int | None = None

    @property
    def handle_token(self) -> str | None:
        token = self._descriptor.get("handle_token")
        return str(token) if token else None

    @property
    def source(self) -> Any | None:
        with self._condition:
            return self._frame_source.source if self._frame_source is not None else None

    def source_for_epoch(self, media_epoch: int) -> Any | None:
        """Borrow only an uploaded source matching the preparation's identity."""
        with self._condition:
            if self._stop.is_set() or self._media_epoch != media_epoch:
                return None
            return self._frame_source.source if self._frame_source is not None else None

    def collect_unused_sources(self, referenced_sources: tuple[Any, ...]) -> None:
        """Release old epochs on the control thread, after all renderers retire.

        Scene references also pin hidden sources: pylibobs release marks its
        source removed, so native reference counting alone cannot preserve them.
        Never release under the condition used for preparation deadlines.
        """
        with self._condition:
            retired = [
                wrapper for wrapper in self._sources
                if wrapper is not self._frame_source
                and not any(wrapper.source is source for source in referenced_sources)
                and not wrapper.source.showing
            ]
            for wrapper in retired:
                self._sources.remove(wrapper)
        for wrapper in retired:
            try:
                wrapper.release()
            except Exception:  # noqa: BLE001 - cleanup must not break supervision
                log.warning("Could not release a retired content source", exc_info=True)

    def start(self) -> bool:
        """Open the channel and start pumping. False if unusable or stopped."""
        with self._pump_lock:
            if self._stop.is_set():
                return False
            if self._thread is not None:
                return True
            return self._start()

    def _start(self) -> bool:
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
        self._sources.append(self._frame_source)
        self._thread = threading.Thread(
            target=self._run, name="solin-content-pump", daemon=True
        )
        self._thread.start()
        return True

    def pump_once(self) -> bool:
        """Deliver the newest frame to libobs. True if a frame was pushed."""
        with self._pump_lock:
            reader = self._reader
            frame_source = self._frame_source
            if self._stop.is_set() or reader is None or frame_source is None:
                return False
            frame = reader.read_latest()
            if frame is None:
                return False
            with self._condition:
                previous_epoch = self._media_epoch
            if previous_epoch is not None and frame.media_epoch < previous_epoch:
                return False
            new_epoch = previous_epoch is not None and frame.media_epoch != previous_epoch
            if new_epoch:
                frame_source = self._frame_source_factory(
                    self._runtime, f"solin-content-{frame.media_epoch}",
                )
                if frame_source is None:
                    return False
            try:
                uploaded = frame_source.push_bgra(
                    frame.data, frame.width, frame.height, frame.stride,
                    reset=frame.media_epoch != previous_epoch,
                )
            except Exception:  # noqa: BLE001 - release new epoch source before re-raising
                if new_epoch:
                    frame_source.release()
                raise
            if not uploaded:
                if new_epoch:
                    frame_source.release()
                return False
            with self._condition:
                if new_epoch:
                    self._sources.append(frame_source)
                if self._stop.is_set():
                    return False
                self._frame_source = frame_source
                self._media_epoch = frame.media_epoch
                self._condition.notify_all()
                return True

    def wait_for_epoch(self, media_epoch: int, *, deadline: float) -> bool:
        """Wait for the requested presentation's first GPU upload, within the IPC deadline."""
        with self._condition:
            while not self._stop.is_set():
                if self._media_epoch is not None and self._media_epoch >= media_epoch:
                    return self._media_epoch == media_epoch
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return False

    def _run(self) -> None:
        while not self._stop.wait(_POLL_SECONDS):
            try:
                self.pump_once()
            except Exception:  # noqa: BLE001 - the pump must never die on one bad frame
                log.debug("content frame pump errored", exc_info=True)

    def stop(self) -> None:
        for frame_source in self.detach_sources():
            try:
                frame_source.release()
            except Exception:  # noqa: BLE001 - libobs boundary
                log.debug("content frame source release errored", exc_info=True)

    def detach_sources(self) -> tuple[Any, ...]:
        """Stop ingress and transfer its frozen native sources to the caller.

        Channel replacement must not release presentations still borrowed by
        scenes or outputs. The new owner retires them after those references end.
        """
        self._stop.set()
        with self._condition:
            self._condition.notify_all()
        thread = self._thread
        if thread is not None:
            thread.join()
        # Also drain explicit pump_once callers before freeing borrowed native
        # resources. A timeout cannot make an unfinished native upload safe.
        with self._pump_lock:
            self._thread = None
            with self._condition:
                sources, self._sources = self._sources, []
                self._frame_source = None
            reader, self._reader = self._reader, None
            if reader is not None:
                try:
                    reader.close()
                except Exception:  # noqa: BLE001 - shm boundary
                    log.debug("content frame channel close errored", exc_info=True)
            return tuple(sources)
